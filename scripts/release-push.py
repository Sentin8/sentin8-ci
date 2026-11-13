#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>
# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>
# SPDX-License-Identifier: Apache-2.0
"""release-push.py - push the v1.0.0 release commits in DAG order (REL-1).

Derives the dependency direction from each repo's Cargo.toml git-tag
pins: a repo that PINS another repo depends on it, so it must be pushed
AFTER (once the pin target's new tag exists, the pin resolves). The
SDK repos are pushed first (they carry the tags consumers resolve),
then everything else.

Usage: release-push.py [--push] [--dry-run] [--root PATH]
The script pushes `HEAD:main` per repo with a pending release commit.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PIN = re.compile(r'git = "https://github\.com/([^/]+)/([^"\s]+)"', re.M)
SKIP_DIRS = {".git", "target", "node_modules", ".tmp", "vendor", "build", ".dart_tool"}


def sh(*args: str, cwd: Path) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def git(*args: str, cwd: Path) -> str:
    return sh("git", *args, cwd=cwd)


def pins(repo: Path) -> set[str]:
    """Repo slugs this repo pins via Cargo git tags (topological key)."""
    out: set[str] = set()
    for m in sorted(repo.rglob("Cargo.toml")):
        if any(part in SKIP_DIRS for part in m.relative_to(repo).parts):
            continue
        txt = m.read_text(errors="ignore")
        for owner, name in PIN.findall(txt):
            slug = f"{owner}/{name}"
            # A repo never pins itself (git source pins inside SDK
            # workspaces all target the SAME repo).
            if name != repo.name:
                out.add(slug)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    tree = Path(args.root).resolve()

    repos: list[Path] = []
    for gitdir in sorted(tree.rglob(".git")):
        s = str(gitdir)
        if ("/target/" in s or "/node_modules/" in s or "/.tmp/" in s
                or "/.run/" in s or gitdir.parent.name == ".github"):
            continue
        repo = gitdir.parent
        if repo == tree or any(part in SKIP_DIRS for part in repo.parts):
            continue
        repos.append(repo)

    name2path = {r.name: r for r in repos}

    # Pending release commit?
    pending = {}
    for r in repos:
        ahead = git("rev-list", "--count", "origin/main..HEAD", cwd=r) \
            if _has_origin(r) else "0"
        dirty = git("status", "--porcelain", cwd=r)
        if int(ahead) > 0 or dirty:
            pending[r] = (int(ahead), dirty)

    # Topological order: repos whose pin targets are NOT pending go
    # first ( Kahn: a repo is ready when none of its pinned slugs are
    # still pending).
    order: list[Path] = []
    remaining = set(pending)
    while remaining:
        progressed = False
        for r in sorted(remaining):
            deps = {d for d in pins(r)
                    if d.rsplit("/", 1)[-1] in {x.name for x in remaining}}
            if not deps:
                order.append(r)
                remaining.discard(r)
                progressed = True
        if not progressed:
            # cycle - push the rest alphabetically (cargo pins resolve
            # by tag at CONSUMER-BUILD time, not push time, so push
            # order only matters for the cascade; a cycle here is fine).
            order.extend(sorted(remaining))
            break

    n = 0
    for r in order:
        ahead, dirty = pending[r]
        rel = r.relative_to(tree)
        action = "pushed" if args.push else "would push"
        n += 1
        print(f"[{n:3}] {rel} (ahead {ahead}, dirty {'yes' if dirty else 'no'}) - {action}")
        if not args.push:
            continue
        git("push", "origin", "HEAD:main", cwd=r)
    print(f"\n{n} repos {'pushed' if args.push else 'in push order'}")
    return 0


def _has_origin(r: Path) -> bool:
    try:
        git("remote", "get-url", "origin", cwd=r)
        return True
    except RuntimeError:
        return False


if __name__ == "__main__":
    sys.exit(main())
