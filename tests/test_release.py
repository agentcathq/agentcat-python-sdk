"""Tests of .github/scripts/release.py: the prepare, status and notes steps of
.github/workflows/release.yml, as in the Java, Rust and Ruby SDKs.

The sdist packs tests/ but not .github/, so this module skips itself when the script
is absent.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "release.py"

if not SCRIPT.is_file():
    pytest.skip("the release script is not in the sdist", allow_module_level=True)


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agentcat_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load()
REPOSITORY: str = release.REPOSITORY
Version = tuple[int, int, int]

CHANGELOG = f"""# Changelog

Intro.

## [Unreleased]

### Added

- Thing two.

## [1.0.0] - 2026-01-02

### Added

- Thing one.

[Unreleased]: {REPOSITORY}/compare/v1.0.0...HEAD
[1.0.0]: {REPOSITORY}/releases/tag/v1.0.0
"""
DATED = f"""# Changelog

Intro.

## [Unreleased]

## [1.1.0] - 2026-11-03

### Added

- Thing two.

## [1.0.0] - 2026-01-02

### Added

- Thing one.

[Unreleased]: {REPOSITORY}/compare/v1.1.0...HEAD
[1.1.0]: {REPOSITORY}/compare/v1.0.0...v1.1.0
[1.0.0]: {REPOSITORY}/releases/tag/v1.0.0
"""
FIRST = f"""# Changelog

## [Unreleased]

First release.

### Added

- Thing one.

[Unreleased]: {REPOSITORY}/commits/main
"""
PYPROJECT = """[project]
name = "agentcat"
version = "1.0.0"
dependencies = [
    "agentcat-api==1.0.2",
]

[tool.hatch.version]
version = "9.9.9"

[dependency-groups]
dev = ["freezegun>=1.5.2"]
"""


def v(text: str) -> Version:
    version: Version | None = release.parse_version(text)
    assert version is not None, text
    return version


# ── Versions ─────────────────────────────────────────────────────────────────


def test_versions_parse_only_as_major_minor_patch() -> None:
    assert release.show(v("1.20.3")) == "1.20.3"
    assert release.show(v("0.0.0")) == "0.0.0"
    for bad in (
        "",
        "1",
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-beta.1",
        "1.2.3b1",
        "1.2.3rc1",
        "1.2.3.post1",
        "1.2.3+build",
        "v1.2.3",
        " 1.2.3",
        "1.2.3\n",
        "1.2.x",
        "1..3",
        "-1.2.3",
    ):
        assert release.parse_version(bad) is None, bad


def test_the_newest_tag_ignores_tags_that_are_not_versions() -> None:
    tags = ["v1.2.0", "v1.10.0", "v1.9.9", "v2.0.0b1", "v2.0", "1.11.0", "attempt-1"]
    assert release.newest_tag(tags) == v("1.10.0")
    assert release.newest_tag(["v2.0.0b1", "attempt-1"]) is None


def test_words_count_from_the_newest_tag() -> None:
    current, newest = v("1.2.3"), v("1.2.3")
    assert release.next_version("patch", current, newest) == v("1.2.4")
    assert release.next_version("minor", current, newest) == v("1.3.0")
    assert release.next_version("major", current, newest) == v("2.0.0")
    assert release.next_version("1.5.0", current, newest) == v("1.5.0")


def test_an_exact_version_must_be_newer_than_the_newest_tag() -> None:
    with pytest.raises(
        release.ReleaseError,
        match=r"^1\.2\.3 is not newer than the newest tag, v1\.2\.3$",
    ):
        release.next_version("1.2.3", v("1.2.3"), v("1.2.3"))
    with pytest.raises(release.ReleaseError):
        release.next_version("1.0.9", v("1.2.3"), v("1.2.3"))
    with pytest.raises(
        release.ReleaseError,
        match=r'^"1\.3" is not patch, minor, major or a version X\.Y\.Z$',
    ):
        release.next_version("1.3", v("1.2.3"), v("1.2.3"))


def test_the_first_release_is_the_version_of_pyproject_toml() -> None:
    for word in ("patch", "minor", "major", "1.0.0"):
        assert release.next_version(word, v("1.0.0"), None) == v("1.0.0"), word
    with pytest.raises(
        release.ReleaseError,
        match=r"^the first release is the version of pyproject\.toml, 1\.0\.0; "
        r"set it there to release 1\.1\.0$",
    ):
        release.next_version("1.1.0", v("1.0.0"), None)
    with pytest.raises(release.ReleaseError):
        release.next_version("next", v("1.0.0"), None)


# ── pyproject.toml ───────────────────────────────────────────────────────────


def test_the_version_is_the_one_of_the_project_table() -> None:
    assert release.project_version(PYPROJECT) == v("1.0.0")


def test_a_bad_or_missing_project_version_is_not_a_release_version() -> None:
    with pytest.raises(release.ReleaseError, match='is not a version X.Y.Z: "1.0"'):
        release.project_version(PYPROJECT.replace('"1.0.0"', '"1.0"'))
    with pytest.raises(release.ReleaseError, match='is not a version X.Y.Z: "1.1.0b1"'):
        release.project_version(PYPROJECT.replace('"1.0.0"', '"1.1.0b1"'))
    with pytest.raises(release.ReleaseError, match="has 0 version lines, not one"):
        release.project_version(PYPROJECT.replace('version = "1.0.0"\n', ""))
    with pytest.raises(release.ReleaseError, match=r"has no \[project\] table"):
        release.project_version('[tool.hatch]\nversion = "1.0.0"\n')


def test_setting_the_version_changes_only_the_project_version_line() -> None:
    assert release.set_project_version(PYPROJECT, v("1.0.1")) == PYPROJECT.replace(
        'version = "1.0.0"', 'version = "1.0.1"'
    )
    with pytest.raises(release.ReleaseError, match="has 0 version lines, not one"):
        release.set_project_version('[project]\nname = "x"\n', v("1.0.1"))


# ── CHANGELOG.md ─────────────────────────────────────────────────────────────


def test_dating_moves_the_entries_under_the_version_and_links_both_sections() -> None:
    assert release.date_changelog(CHANGELOG, v("1.1.0"), "2026-11-03", v("1.0.0")) == (
        DATED
    )
    assert release.dated_section(DATED, v("1.1.0")) == (
        "2026-11-03",
        "### Added\n\n- Thing two.",
    )
    assert release.dated_section(DATED, v("1.0.0")) == (
        "2026-01-02",
        "### Added\n\n- Thing one.",
    )


def test_the_first_release_links_its_tag() -> None:
    dated = release.date_changelog(FIRST, v("1.0.0"), "2026-11-03", None)
    assert (
        dated
        == f"""# Changelog

## [Unreleased]

## [1.0.0] - 2026-11-03

First release.

### Added

- Thing one.

[Unreleased]: {REPOSITORY}/compare/v1.0.0...HEAD
[1.0.0]: {REPOSITORY}/releases/tag/v1.0.0
"""
    )
    assert release.dated_section(dated, v("1.0.0")) == (
        "2026-11-03",
        "First release.\n\n### Added\n\n- Thing one.",
    )


def test_dating_refuses_an_empty_section_a_bad_date_and_a_missing_section() -> None:
    with pytest.raises(
        release.ReleaseError,
        match=r"^the ## \[Unreleased\] section of CHANGELOG\.md has no entry; "
        r"add the changes of the release first$",
    ):
        release.date_changelog(DATED, v("1.2.0"), "2026-11-03", v("1.1.0"))
    for date in ("2026-11-3", "2026-13-01", "2026-02-30", "03-11-2026"):
        with pytest.raises(release.ReleaseError, match="is not a date YYYY-MM-DD"):
            release.date_changelog(CHANGELOG, v("1.1.0"), date, v("1.0.0"))
    with pytest.raises(release.ReleaseError, match=r"has no ## \[Unreleased\] section"):
        release.date_changelog("# Changelog\n", v("1.1.0"), "2026-11-03", None)
    with pytest.raises(release.ReleaseError, match=r"has no \[Unreleased\] link"):
        release.date_changelog(
            FIRST.replace("[Unreleased]: ", "Unreleased: "),
            v("1.0.0"),
            "2026-11-03",
            None,
        )


def test_a_section_without_a_valid_date_is_not_dated() -> None:
    assert (
        release.dated_section(DATED.replace("2026-11-03", "YYYY-MM-DD"), v("1.1.0"))
        is None
    )
    assert (
        release.dated_section(
            DATED.replace("## [1.1.0] - 2026-11-03", "## [1.1.0]"), v("1.1.0")
        )
        is None
    )
    assert release.dated_section(DATED, v("9.9.9")) is None
    assert (
        release.dated_section(DATED.replace("[1.1.0]", "[1.1.0b1]"), v("1.1.0")) is None
    )


# ── The steps, as release.yml runs them ──────────────────────────────────────


def environment(**values: str) -> dict[str, str]:
    """The environment without the GIT_* variables of a git hook, such as GIT_DIR,
    which point git at the outer repository."""
    kept = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    return {**kept, **values}


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        env=environment(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"),
    )


def step(root: Path, *arguments: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *arguments],
        capture_output=True,
        text=True,
        env=environment(),
    )
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "start")
    yield tmp_path


def test_prepare_then_status_then_notes(repository: Path) -> None:
    git(repository, "tag", "v1.0.0")
    assert step(repository, "status") == (0, "1.0.0 skip\n", "")

    assert step(repository, "prepare", "minor", "2026-11-03") == (0, "1.1.0\n", "")

    pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
    assert pyproject == PYPROJECT.replace('version = "1.0.0"', 'version = "1.1.0"')
    assert (repository / "CHANGELOG.md").read_text(encoding="utf-8") == DATED
    assert step(repository, "status") == (0, "1.1.0 publish\n", "")
    assert step(repository, "notes", "1.1.0") == (0, "### Added\n\n- Thing two.\n", "")

    git(repository, "tag", "v1.1.0")
    assert step(repository, "status") == (0, "1.1.0 skip\n", "")


def test_the_first_release_is_the_pyproject_version_and_links_its_tag(
    repository: Path,
) -> None:
    (repository / "CHANGELOG.md").write_text(FIRST, encoding="utf-8")
    git(repository, "tag", "v1.0.0b1")
    assert step(repository, "status") == (0, "1.0.0 skip\n", "")

    assert step(repository, "prepare", "patch", "2026-11-03") == (0, "1.0.0\n", "")

    assert (repository / "pyproject.toml").read_text(encoding="utf-8") == PYPROJECT
    changelog = (repository / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[1.0.0]: {REPOSITORY}/releases/tag/v1.0.0\n" in changelog
    assert step(repository, "status") == (0, "1.0.0 publish\n", "")


def test_prepare_stops_without_an_entry_and_changes_nothing(repository: Path) -> None:
    (repository / "CHANGELOG.md").write_text(DATED, encoding="utf-8")
    git(repository, "tag", "v1.1.0")

    assert step(repository, "prepare", "patch", "2026-11-04") == (
        1,
        "",
        "release: the ## [Unreleased] section of CHANGELOG.md has no entry; "
        "add the changes of the release first\n",
    )

    assert (repository / "CHANGELOG.md").read_text(encoding="utf-8") == DATED
    assert (repository / "pyproject.toml").read_text(encoding="utf-8") == PYPROJECT


def test_notes_need_a_dated_section(repository: Path) -> None:
    assert step(repository, "notes", "1.0.0") == (0, "### Added\n\n- Thing one.\n", "")
    assert step(repository, "notes", "1.1.0") == (
        1,
        "",
        "release: CHANGELOG.md has no dated section for 1.1.0\n",
    )
    assert step(repository, "notes", "1.1") == (
        1,
        "",
        'release: "1.1" is not a version X.Y.Z\n',
    )


def test_unknown_arguments_get_the_usage(repository: Path) -> None:
    for arguments in ((), ("publish",), ("prepare", "patch"), ("status", "now")):
        assert step(repository, *arguments) == (2, "", release.USAGE + "\n")


# ── This repository ──────────────────────────────────────────────────────────


def test_this_repository_can_release_the_version_of_pyproject_toml(
    tmp_path: Path,
) -> None:
    """The CHANGELOG of this repository can always release the version of
    pyproject.toml. Before the release pull request, prepare dates the [Unreleased]
    entries; after it, the version has its dated section, so status publishes it
    while it has no tag."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.count(release.UNRELEASED + "\n") == 1
    assert sum(line.startswith("[Unreleased]: ") for line in changelog.split("\n")) == 1
    version = release.project_version(pyproject)
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "start")
    shown = release.show(version)

    if release.dated_section(changelog, version) is None:
        assert step(tmp_path, "prepare", "patch", "2026-11-17") == (0, shown + "\n", "")
        assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == pyproject
    assert step(tmp_path, "status") == (0, f"{shown} publish\n", "")

    code, out, _ = step(tmp_path, "notes", shown)
    assert code == 0
    assert "### " in out
    assert "## [" not in out
    dated = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{shown}]: {REPOSITORY}/" in dated
