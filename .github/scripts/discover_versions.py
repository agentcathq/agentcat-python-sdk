#!/usr/bin/env python3
"""Pick the upstream versions a compatibility matrix should test.

Replaces the `pip index versions | grep | sort -V` pipelines the two
compatibility workflows used to carry. Three reasons it is Python:

- `sort -V` is not PEP 440. Measured: it sorts `4.0.0b1` AFTER `4.0.0`, where
  PEP 440 puts a prerelease BEFORE the release it leads to. So "latest" off
  `sort -V | tail -1` could name a superseded prerelease as the newest version.
- `pip index versions` is an experimental command that prints a deprecation
  banner to stderr and has no stable output contract.
- A range like `>=1.2,<3` is one specifier here instead of hand-rolled
  major/minor arithmetic that has to be re-derived every time a bound moves.

Usage:
    discover_versions.py <package> <specifier>          # stable releases
    discover_versions.py --pre <package> <specifier>    # prereleases only

Specifier gotcha, and why the prerelease callers pass `a0`: PEP 440 orders
`4.0.0b1 < 4.0.0`, so `>=4.0` EXCLUDES every 4.0.0 prerelease and a `--pre`
run against it silently finds nothing. `>=4.0.0a0` is the lowest bound that
admits the whole 4.0.0 prerelease series.

Stable mode emits the newest patch of every minor in range — one matrix leg per
minor, which is the granularity upstream breaks things at.

Prerelease mode emits at most one version: the newest prerelease in range that
no stable release has superseded. A prerelease older than the latest stable is
not an early warning about anything, it is history, so `mcp 2.0.0rc1` drops out
the day `mcp 2.0.0` ships and that job goes back to skipping itself. This is
what keeps the prerelease workflow pointed at the NEXT generation without a
hardcoded version number to bump.

Writes GitHub Actions step outputs (`versions=[...]`, `has-versions=...`) to
stdout, for appending to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

PYPI_JSON = "https://pypi.org/pypi/{package}/json"
TIMEOUT_SECONDS = 30


def fetch_releases(package: str) -> dict[str, list[dict[str, object]]]:
    """Every release of `package` from PyPI's JSON API, mapped to its files."""
    url = PYPI_JSON.format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"::error::Could not read {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    releases = payload.get("releases")
    if not isinstance(releases, dict):
        print(f"::error::{url} returned no releases map", file=sys.stderr)
        raise SystemExit(1)
    return releases


def installable(files: object) -> bool:
    """True when a release still has at least one file nobody has yanked.

    PyPI keeps an entry for deleted and fully-yanked releases, and pinning one
    fails the install rather than reporting an incompatibility — so a matrix
    leg for it would be noise that looks exactly like a real failure.
    """
    if not isinstance(files, list) or not files:
        return False
    return any(not entry.get("yanked", False) for entry in files)


def parse_versions(releases: dict[str, list[dict[str, object]]]) -> list[Version]:
    parsed = []
    for raw, files in releases.items():
        if not installable(files):
            continue
        try:
            parsed.append(Version(raw))
        except InvalidVersion:
            continue
    return parsed


def latest_per_minor(versions: list[Version]) -> list[Version]:
    newest: dict[tuple[int, int], Version] = {}
    for version in versions:
        key = (version.major, version.minor)
        if key not in newest or version > newest[key]:
            newest[key] = version
    return [newest[key] for key in sorted(newest)]


def select(package: str, specifier: str, prerelease: bool) -> list[Version]:
    all_versions = parse_versions(fetch_releases(package))
    # `prereleases=True` on both sides: the default would silently drop every
    # prerelease from an in-range check, which is exactly what we filter on.
    in_range = [v for v in all_versions if SpecifierSet(specifier).contains(v, True)]

    if not prerelease:
        return latest_per_minor([v for v in in_range if not v.is_prerelease])

    candidates = [v for v in in_range if v.is_prerelease]
    stable = [v for v in all_versions if not v.is_prerelease]
    if stable:
        newest_stable = max(stable)
        candidates = [v for v in candidates if v > newest_stable]
    return [max(candidates)] if candidates else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package")
    parser.add_argument("specifier", help="PEP 440 range, e.g. '>=1.2,<3'")
    parser.add_argument(
        "--pre",
        action="store_true",
        dest="prerelease",
        help="emit the newest un-superseded prerelease instead of stables",
    )
    args = parser.parse_args()

    selected = select(args.package, args.specifier, args.prerelease)
    rendered = json.dumps([str(v) for v in selected], separators=(",", ":"))

    channel = "prerelease" if args.prerelease else "stable"
    print(
        f"Selected {len(selected)} {args.package} {channel} version(s) "
        f"for {args.specifier}: {rendered}",
        file=sys.stderr,
    )
    print(f"versions={rendered}")
    print(f"has-versions={'true' if selected else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
