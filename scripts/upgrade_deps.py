#!/usr/bin/env python3
"""Bump every dependency in pyproject.toml to its latest compatible release.

Goal: clear the recurring "outdated package" / Dependabot findings in one shot.
We're a typical user of these libs, so latest-is-fine — the SAFETY GATE is the
manual test pass AFTER running this, not the bump itself:

    uv run python scripts/upgrade_deps.py
    # run the site + the test suite, click around:
    #   uv run --extra dev pytest
    #   uv run poiesis start --app-only   (then poke the UI)
    git commit -am "chore: bump all deps to latest"   # only if it all works

How it works (no PyPI API, no TOML editing — uv does the strip, resolve, and
repin):

  PHASE 1 — `uv remove` every declared dependency, across the main list AND
            every dependency section. This clears ALL version constraints
            first. Order matters: uv resolves every section together, so a
            leftover ceiling in one (e.g. gunicorn<24) would otherwise leak
            into the shared resolution and hold packages back below latest.

  PHASE 2 — `uv add` them all back (bare, but keeping extras like
            [standard]/[django]). With nothing pinning anything, uv resolves
            the latest *mutually-compatible* set and rewrites each entry to
            `>=<latest>`.

Then `uv sync`.

Dependency sections handled (each stripped/re-added with the right uv flag):
  * [project.dependencies]                      -> no flag
  * [project.optional-dependencies].<extra>     -> --optional <extra>
  * [dependency-groups].<group>                 -> --group <group>

Notes:
  * A bare `uv add <name>` on a still-constrained dep is a no-op — the remove
    is what frees it. (And `pkg@latest` isn't a uv thing; that's npm.)
  * Resolving the whole set at once lets uv find a consistent solution rather
    than forcing per-package absolute-latests that might conflict.
  * It deliberately does NOT git-commit. You test first.

Usage:
    uv run python scripts/upgrade_deps.py             # apply
    uv run python scripts/upgrade_deps.py --dry-run   # print uv commands only
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
# package name + optional [extras]; stops before the version specifier
NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?")


def split_dep(spec: str):
    """'sentry-sdk[django]>=2.19.2' -> ('sentry-sdk', 'sentry-sdk[django]').

    Returns (base_name_for_remove, name_with_extras_for_add).
    """
    m = NAME_RE.match(spec)
    if not m:
        return None, None
    base = m.group(1)
    return base, base + (m.group(2) or "")


def collect_sections() -> list[tuple[str, list[str], list[str]]]:
    """Every dependency section as (label, uv_flag, deps).

    uv_flag is the extra argv uv needs to target that section:
      main -> [], optional extra -> ['--optional', name], group -> ['--group', name].
    """
    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    sections: list[tuple[str, list[str], list[str]]] = []

    main = list(data.get("project", {}).get("dependencies", []))
    if main:
        sections.append(("(main)", [], main))
    for extra, deps in data.get("project", {}).get("optional-dependencies", {}).items():
        if deps:
            sections.append((f"optional:{extra}", ["--optional", extra], list(deps)))
    for group, deps in data.get("dependency-groups", {}).items():
        if deps:
            sections.append((f"group:{group}", ["--group", group], list(deps)))
    return sections


def run(cmd, dry: bool) -> bool:
    print(">>>", " ".join(cmd))
    return True if dry else subprocess.run(cmd, cwd=ROOT).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Bump all deps to latest via uv.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the uv commands without running them")
    args = ap.parse_args()

    # Parse everything up front so a malformed entry aborts before any mutation.
    parsed = []  # (label, flag, bases, full_with_extras)
    for label, flag, deps in collect_sections():
        bases, full = [], []
        for dep in deps:
            base, name_extras = split_dep(dep)
            if not base:
                print(f"  ? unparseable, skipping: {dep!r}", file=sys.stderr)
                continue
            bases.append(base)
            full.append(name_extras)
        if bases:
            parsed.append((label, flag, bases, full))

    if not parsed:
        print("No dependencies found in pyproject.toml — nothing to do.")
        return 0

    print("PHASE 1 — strip every version constraint (uv remove)\n")
    for label, flag, bases, _ in parsed:
        run(["uv", "remove", *flag, *bases], args.dry_run)

    print("\nPHASE 2 — re-add bare; uv resolves + repins to latest (uv add)\n")
    ok = True
    for label, flag, _, full in parsed:
        if not run(["uv", "add", *flag, *full], args.dry_run):
            ok = False
            print(f"  !! uv add failed for {label} — see error above")

    if args.dry_run:
        print("\n--dry-run: nothing changed.")
        return 0

    print("\n=== uv sync ===")
    subprocess.run(["uv", "sync"], cwd=ROOT)
    print("\nDONE. Review the diff, then TEST before committing:")
    print("    git diff pyproject.toml uv.lock")
    print("    uv run --extra dev pytest        # + boot the app and click around")
    print("    git commit -am 'chore: bump all deps to latest'   # only if green")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
