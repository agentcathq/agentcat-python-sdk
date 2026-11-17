#!/usr/bin/env python3
"""The release steps of .github/workflows/release.yml, on pyproject.toml, CHANGELOG.md
and the v* tags of the repository. The Java, Rust and Ruby SDKs have the same three
steps. It needs only the standard library of Python 3.10 or later.

Usage:
  release.py prepare <patch | minor | major | X.Y.Z> <YYYY-MM-DD>
      picks the next version, writes it into the [project] version of pyproject.toml,
      turns the [Unreleased] section of CHANGELOG.md into the section of that version
      with the date, and prints the version. patch, minor and major count from the
      newest v* tag. With no tag yet, the first release is the version that
      pyproject.toml holds, whatever the input word. An exact version must be newer
      than the newest tag. An [Unreleased] section with no entry stops the step: a
      release needs a change.
  release.py status
      prints '<version> publish' when the version of pyproject.toml has no tag and
      CHANGELOG.md has its dated section, else '<version> skip'.
  release.py notes <X.Y.Z>
      prints the body of the CHANGELOG.md section of that version.

--root <folder>, before the command, names another repository folder (default: this
one). A failed step exits with 1 and the reason, and a usage error with 2.

This file is not in the wheel or the sdist: pyproject.toml packs only src/agentcat,
tests and a few documents. tests/test_release.py tests it.
"""

from __future__ import annotations

import datetime
import re
import subprocess
import sys
from pathlib import Path

USAGE = (
    "usage: release.py [--root <folder>] prepare <patch | minor | major | X.Y.Z> "
    "<YYYY-MM-DD> | status | notes <X.Y.Z>"
)

# The repository URL that the CHANGELOG.md links point to.
REPOSITORY = "https://github.com/agentcathq/agentcat-python-sdk"

PYPROJECT = "pyproject.toml"
CHANGELOG = "CHANGELOG.md"
UNRELEASED = "## [Unreleased]"

# MAJOR.MINOR.PATCH with decimal numbers and no leading zero: no pre-release or build
# part.
VERSION_PART = r"(?:0|[1-9][0-9]*)"
VERSION = re.compile(rf"{VERSION_PART}\.{VERSION_PART}\.{VERSION_PART}")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The `version = "..."` line of a TOML table, and the header of the next table.
VERSION_LINE = re.compile(r'^(version[ \t]*=[ \t]*")([^"\n]*)("[ \t]*)$', re.MULTILINE)
TABLE = re.compile(r"^\[", re.MULTILINE)
LINK = re.compile(r"\[[^\]]+\]: ")

Version = tuple[int, int, int]


class ReleaseError(Exception):
    pass


def parse_version(text: str) -> Version | None:
    """(major, minor, patch) of a version MAJOR.MINOR.PATCH, else None."""
    if not VERSION.fullmatch(text):
        return None
    major, minor, patch = (int(part) for part in text.split("."))
    return major, minor, patch


def show(version: Version) -> str:
    return ".".join(str(part) for part in version)


def newest_tag(tags: list[str]) -> Version | None:
    """The newest of the v<version> tags; other tags, such as v2.0.0b1, do not count."""
    versions = [
        version
        for tag in tags
        if tag.startswith("v") and (version := parse_version(tag[1:])) is not None
    ]
    return max(versions, default=None)


def next_version(word: str, current: Version, newest: Version | None) -> Version:
    """The version of the release that word asks for: see the usage."""
    exact = parse_version(word)
    if exact is None and word not in ("patch", "minor", "major"):
        raise ReleaseError(f'"{word}" is not patch, minor, major or a version X.Y.Z')
    if newest is None:
        if exact is not None and exact != current:
            raise ReleaseError(
                f"the first release is the version of {PYPROJECT}, {show(current)}; "
                f"set it there to release {show(exact)}"
            )
        return current
    major, minor, patch = newest
    following = {
        "patch": (major, minor, patch + 1),
        "minor": (major, minor + 1, 0),
        "major": (major + 1, 0, 0),
    }
    version = exact if exact is not None else following[word]
    if version <= newest:
        raise ReleaseError(
            f"{show(version)} is not newer than the newest tag, v{show(newest)}"
        )
    return version


def project_table(pyproject: str) -> tuple[int, int]:
    """The start and the end of the body of the [project] table of pyproject.toml."""
    header = re.search(r"^\[project\][ \t]*$", pyproject, re.MULTILINE)
    if header is None:
        raise ReleaseError(f"{PYPROJECT} has no [project] table")
    following = TABLE.search(pyproject, header.end())
    return header.end(), following.start() if following else len(pyproject)


def project_version(pyproject: str) -> Version:
    """The version of the [project] table of pyproject.toml."""
    start, end = project_table(pyproject)
    lines = list(VERSION_LINE.finditer(pyproject, start, end))
    if len(lines) != 1:
        raise ReleaseError(
            f"the [project] table of {PYPROJECT} has {len(lines)} version lines, "
            f"not one"
        )
    version = parse_version(lines[0].group(2))
    if version is None:
        raise ReleaseError(
            f"the [project] version of {PYPROJECT} is not a version X.Y.Z: "
            f'"{lines[0].group(2)}"'
        )
    return version


def set_project_version(pyproject: str, version: Version) -> str:
    """The text of pyproject.toml with version as its [project] version, and nothing
    else changed."""
    project_version(pyproject)
    start, end = project_table(pyproject)
    body = VERSION_LINE.sub(
        lambda m: f"{m.group(1)}{show(version)}{m.group(3)}", pyproject[start:end]
    )
    return pyproject[:start] + body + pyproject[end:]


def date_changelog(
    changelog: str, version: Version, date: str, previous: Version | None
) -> str:
    """The text of CHANGELOG.md with the [Unreleased] entries under
    '## [<version>] - <date>', an empty [Unreleased] section above it, and the links
    of both sections."""
    if not is_date(date):
        raise ReleaseError(f'"{date}" is not a date YYYY-MM-DD')
    lines = split_lines(changelog)
    if UNRELEASED not in lines:
        raise ReleaseError(f"{CHANGELOG} has no {UNRELEASED} section")
    start = lines.index(UNRELEASED)
    entries = lines[start + 1 : section_end(lines, start)]
    if not any(line.startswith("- ") for line in entries):
        raise ReleaseError(
            f"the {UNRELEASED} section of {CHANGELOG} has no entry; "
            f"add the changes of the release first"
        )
    link = next(
        (i for i, line in enumerate(lines) if line.startswith("[Unreleased]: ")), None
    )
    if link is None:
        raise ReleaseError(f"{CHANGELOG} has no [Unreleased] link")
    if previous is None:
        target = f"releases/tag/v{show(version)}"
    else:
        target = f"compare/v{show(previous)}...v{show(version)}"
    version_link = f"[{show(version)}]: {REPOSITORY}/{target}"
    out: list[str] = []
    for i, line in enumerate(lines):
        if i == start:
            out += [UNRELEASED, "", f"## [{show(version)}] - {date}"]
        elif i == link:
            out += [
                f"[Unreleased]: {REPOSITORY}/compare/v{show(version)}...HEAD",
                version_link,
            ]
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def dated_section(changelog: str, version: Version) -> tuple[str, str] | None:
    """The (date, body) of the '## [<version>] - <date>' section, the body without its
    blank edges, or None when the section is missing or its date is not valid."""
    lines = split_lines(changelog)
    heading = f"## [{show(version)}] - "
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return None
    date = lines[start][len(heading) :].strip()
    if not is_date(date):
        return None
    return date, "\n".join(lines[start + 1 : section_end(lines, start)]).strip("\n")


def section_end(lines: list[str], start: int) -> int:
    """The index of the line after the section that starts at start: the next '## '
    heading or the first link line."""
    return next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith("## ") or LINK.match(lines[i])
        ),
        len(lines),
    )


def split_lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def is_date(text: str) -> bool:
    if not DATE.fullmatch(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def prepare(root: Path, word: str, date: str) -> str:
    """The prepare step: see the usage. Returns the version."""
    pyproject = read(root / PYPROJECT)
    current = project_version(pyproject)
    newest = newest_tag(tags(root))
    version = next_version(word, current, newest)
    changelog = date_changelog(read(root / CHANGELOG), version, date, newest)
    if version != current:
        write(root / PYPROJECT, set_project_version(pyproject, version))
    write(root / CHANGELOG, changelog)
    return show(version)


def status(root: Path) -> str:
    """The status step: see the usage."""
    version = project_version(read(root / PYPROJECT))
    tagged = f"v{show(version)}" in tags(root)
    dated = dated_section(read(root / CHANGELOG), version) is not None
    return f"{show(version)} {'publish' if dated and not tagged else 'skip'}"


def notes(root: Path, text: str) -> str:
    """The notes step: see the usage. Returns the body of the section."""
    version = parse_version(text)
    if version is None:
        raise ReleaseError(f'"{text}" is not a version X.Y.Z')
    section = dated_section(read(root / CHANGELOG), version)
    if section is None:
        raise ReleaseError(f"{CHANGELOG} has no dated section for {text}")
    return section[1]


def tags(root: Path) -> list[str]:
    """The tags of the repository at root."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as error:
        raise ReleaseError(f"cannot run git: {error}") from error
    if result.returncode != 0:
        raise ReleaseError(f"git tag --list failed: {result.stderr.strip()}")
    return result.stdout.split()


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseError(f"cannot read {path.name}: {error}") from error


def write(path: Path, text: str) -> None:
    try:
        with path.open("w", encoding="utf-8", newline="\n") as file:
            file.write(text)
    except OSError as error:
        raise ReleaseError(f"cannot write {path.name}: {error}") from error


def run(arguments: list[str]) -> tuple[int, str]:
    """Runs the step that arguments name: (exit code, text for stdout or stderr)."""
    root = Path(__file__).resolve().parents[2]
    if arguments[:1] == ["--root"] and len(arguments) >= 2:
        root, arguments = Path(arguments[1]), arguments[2:]
    try:
        match arguments:
            case ["prepare", word, date]:
                return 0, prepare(root, word, date)
            case ["status"]:
                return 0, status(root)
            case ["notes", version]:
                return 0, notes(root, version)
            case _:
                return 2, USAGE
    except ReleaseError as error:
        return 1, f"release: {error}"


def main(arguments: list[str]) -> int:
    code, text = run(arguments)
    print(text, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
