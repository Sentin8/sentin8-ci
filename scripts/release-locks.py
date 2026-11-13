#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>
# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>
# SPDX-License-Identifier: Apache-2.0
"""release-locks.py - refresh lockfiles after the v1.0.0 manifest bump.

The release lanes build `cargo build --locked` (rust) and `npm ci`
(e2e/site). A manifest version bump desyncs both lock formats:

- Cargo.lock: workspace-member package versions
- package-lock.json: the root "version" + packages[""].version

This script refreshes each affected lock (cargo metadata for Rust,
surgical version-field patch for npm) and commits the refresh as a
second REL-1 commit per repo. Run AFTER release-bump.py + push.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TARGET = "1.0.0"
SKIP_DIRS = {".git", "target", "node_modules", ".tmp", "vendor", "build", ".dart_tool"}


def git(*args: str, cwd: Path) -> str:
    r = subprocess.run(("git",)+args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr.strip()}")
    return r.stdout.strip()


def discover(tree: Path) -> list[Path]:
    found: list[Path] = []
    for gitdir in sorted(tree.rglob(".git")):
        s = str(gitdir)
        if ("/target/" in s or "/node_modules/" in s or "/.tmp/" in s
                or "/.run/" in s or gitdir.parent.name == ".github"):
            continue
        repo = gitdir.parent
        if repo == tree or any(part in SKIP_DIRS for part in repo.parts):
            continue
        found.append(repo)
    return sorted(found)


def refresh_cargo_lock(repo: Path, dry: bool) -> bool:
    """cargo metadata re-resolves local member versions into Cargo.lock."""
    if not (repo / "Cargo.toml").is_file() or not (repo / "Cargo.lock").is_file():
        return False
    if dry:
        # dry: report desync without resolving
        desync = False
        txt = (repo / "Cargo.lock").read_text()
        for m in (repo / "Cargo.toml").read_text().splitlines():
            pass
        # cheap check: any member version 0.x in lock while manifest says 1.0.0
        if "version = \"1.0.0\"" not in txt and 'name = "' in txt:
            pass
        return "version = \"1.0.0\"" not in txt
    before = (repo / "Cargo.lock").read_bytes()
    r = subprocess.run(["cargo", "metadata", "--format-version", "1", "--manifest-path",
                        str(repo / "Cargo.toml")],
                       cwd=repo, capture_output=True, text=True,
                       env={**dict(__import__("os").environ), "SQLX_OFFLINE": "true"})
    if r.returncode != 0:
        print(f"  !! cargo metadata failed {repo.name}: {r.stderr.strip()[:200]}")
        return False
    after = (repo / "Cargo.lock").read_bytes()
    return before != after


def refresh_npm_lock(repo: Path, dry: bool) -> bool:
    """Patch the root version fields in package-lock.json."""
    lock = repo / "package-lock.json"
    if not (repo / "package.json").is_file() or not lock.is_file():
        return False
    data = json.loads(lock.read_text())
    changed = False
    if data.get("version") == TARGET:
        pass
    else:
        data["version"] = TARGET
        changed = True
    root = data.get("packages", {}).get("")
    if root and root.get("version") != TARGET:
        root["version"] = TARGET
        changed = True
    if changed and not dry:
        lock.write_text(json.dumps(data, indent=2) + "\n")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    tree = Path(args.root).resolve()

    n = 0
    for repo in discover(tree):
        changed: list[str] = []
        if refresh_cargo_lock(repo, args.dry_run):
            changed.append("Cargo.lock")
        if refresh_npm_lock(repo, args.dry_run):
            changed.append("package-lock.json")
        if not changed:
            continue
        n += 1
        rel = repo.relative_to(tree)
        if args.dry_run:
            print(f"[dry] {rel}: {', '.join(changed)}")
            continue
        git("add", "-A", cwd=repo)
        git("commit", "-m",
            f"chore(release): v{TARGET} lock refresh (REL-1 - CICD design section 2.6)\n\n"
            f"lockfiles re-resolved for the manifest bump: {', '.join(changed)}",
            cwd=repo)
        git("push", "origin", "HEAD:main", cwd=repo)
        print(f"[push] {rel}: {', '.join(changed)}")
    print(f"\n{n} repos {'need' if args.dry_run else 'got'} lock refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
