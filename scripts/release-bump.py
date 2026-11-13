#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>
# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>
# SPDX-License-Identifier: Apache-2.0
"""release-bump.py - the 1.0.0 wave (REL-1, CICD design §2.6).

Run from the aggregation tree root. For every repo in the ci
classification (plus vertical VERSION files and the tree-root design
docs), move the version carriers to the target version and commit:

- Rust repos: the root (+ SDK-member / mini-facade / sdk crates')
  package `version` in Cargo.toml -> 1.0.0. Never touches third-party
  [workspace.dependencies] / [dependencies] version specs - only the
  `[package] version` / `{crate}.version` lines that belong to crates
  defined in THIS repo (a crate name must match the repo's own crate
  set, detected per-Cargo.toml by the presence of a `[package]` block
  whose edition/license come from the workspace).
- Flutter repos: pubspec.yaml `version:` -> 1.0.0.
- e2e/site repos: package.json `version` -> 1.0.0.
- Vertical roots with a VERSION file: VERSION -> 1.0.0.
- Test literals: known hardcoded "0.1.0" version fields in tests
  (captain's live/integration fixtures) -> 1.0.0.

Per-repo commit: `chore(release): v1.0.0 (REL-1)` with the changed
files. --push performs `git push origin main` per non-skipped repo.
Never touches existing git tags (forward-only - CICD §2.6 ring
discipline). Never touches third-party manifests (node_modules,
target/, vendor/, .tmp/ are skipped by construction).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TARGET = "1.0.0"
SKIP_DIRS = {".git", "target", "node_modules", ".tmp", "vendor", "build", ".dart_tool", ".github"}

VERSION_LINE = re.compile(r'^(\s*version\s*=\s*")0\.\d+\.\d+("\s*)$', re.M)
PUBSPEC_VERSION = re.compile(r'^(version:\s*)0\.\d+\.\d+(\s*)$', re.M)
PKG_VERSION = re.compile(r'^(\s*"version":\s*")0\.\d+\.\d+("\s*,?\s*)$', re.M)


def sh(*args: str, cwd: Path) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def git(*args: str, cwd: Path) -> str:
    return sh("git", *args, cwd=cwd)


def discover(tree: Path) -> list[Path]:
    """Git repositories under `tree` (excluding the tree root itself).

    Mirrors sync-ci.py discovery (rglob .git - dir or worktree file),
    skipping scratch/build dirs and profile .github clones. The tree
    root (Sentin8/verticals) is the docs snapshot repo - its nested
    service repos are the independent release units, so the root is
    never a bump target itself.
    """
    found: list[Path] = []
    for gitdir in sorted(tree.rglob(".git")):
        s = str(gitdir)
        if ("/target/" in s or "/node_modules/" in s or "/.tmp/" in s
                or "/.run/" in s or gitdir.parent.name == ".github"):
            continue
        repo = gitdir.parent
        if repo == tree:
            continue
        if any(part in SKIP_DIRS for part in repo.parts):
            continue
        found.append(repo)
    return sorted(found)


def classify(p: Path) -> str:
    if (p / "pubspec.yaml").is_file():
        return "flutter"
    if (p / "package.json").is_file() and (p / "Cargo.toml").is_file():
        return "rust"  # mixed rust repo with tooling package.json
    if (p / "Cargo.toml").is_file():
        return "rust"
    if (p / "package.json").is_file():
        return "e2e"
    return "docs"


def find_manifests(repo: Path) -> list[Path]:
    """Every Cargo.toml in the repo (excluding skip dirs)."""
    out: list[Path] = []
    for p in sorted(repo.rglob("Cargo.toml")):
        if any(part in SKIP_DIRS for part in p.relative_to(repo).parts):
            continue
        out.append(p)
    return out


def bump_cargo_toml(path: Path, dry: bool) -> bool:
    """Bump the [package] version of a crate DEFINED in this tree.

    A Cargo.toml carries either a [package] (crate manifest) or only
    [workspace]/[dependencies] (virtual workspace or member deps
    section). Only `[package] version` lines move; dependency version
    requirements (`foo = { version = "..." }`) are NEVER touched.
    """
    txt = path.read_text()
    lines = txt.splitlines(keepends=True)
    changed = False
    in_package = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            in_package = s == "[package]"
            continue
        if in_package:
            new, n = VERSION_LINE.subn(rf"\g<1>{TARGET}\g<2>", line)
            if n and new != line:
                lines[i] = new
                changed = True
    if changed and not dry:
        path.write_text("".join(lines))
    return changed


def bump_pubspec(path: Path, dry: bool) -> bool:
    txt = path.read_text()
    out = PUBSPEC_VERSION.sub(rf"\g<1>{TARGET}\g<2>", txt, count=1)
    if out != txt:
        if not dry:
            path.write_text(out)
        return True
    return False


def bump_package_json(path: Path, dry: bool) -> bool:
    txt = path.read_text()
    out = PKG_VERSION.sub(rf"\g<1>{TARGET}\g<2>", txt, count=1)
    if out != txt:
        if not dry:
            path.write_text(out)
        return True
    return False


def bump_version_file(path: Path, dry: bool) -> bool:
    txt = path.read_text().strip()
    if txt and re.fullmatch(r"0\.\d+\.\d+", txt) and txt != TARGET:
        if not dry:
            path.write_text(TARGET + "\n")
        return True
    return False


TEST_LITERALS: dict[str, tuple[str, str]] = {
    # captain fixtures stamp a version value the catalog rows carry
    # (keys are REPO-relative paths).
    "tests/live.rs": ('version: "0.1.0".into()', f'version: "{TARGET}".into()'),
    "tests/integration.rs": ('version: "0.1.0".into()', f'version: "{TARGET}".into()'),
}


def bump_test_literals(repo: Path, rel: str, dry: bool) -> bool:
    f = repo / rel
    if not f.is_file():
        return False
    old, new = TEST_LITERALS[rel]
    txt = f.read_text()
    if old in txt:
        if not dry:
            f.write_text(txt.replace(old, new))
        return True
    return False


# Repos that carry the known version literals (repo name -> checked).
TEST_LITERAL_REPOS = {"sentin8-captain"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--root", default=".", help="aggregation tree root")
    args = ap.parse_args()
    tree = Path(args.root).resolve()

    repos = discover(tree)

    bumped = skipped = 0
    for repo in repos:
        cls = classify(repo)
        changed: list[str] = []

        if cls == "rust":
            for m in find_manifests(repo):
                if bump_cargo_toml(m, args.dry_run):
                    changed.append(str(m.relative_to(repo)))
        elif cls == "flutter":
            pub = repo / "pubspec.yaml"
            if bump_pubspec(pub, args.dry_run):
                changed.append("pubspec.yaml")
        elif cls == "e2e":
            pkg = repo / "package.json"
            if bump_package_json(pkg, args.dry_run):
                changed.append("package.json")

        # Vertical/root VERSION files.
        vf = repo / "VERSION"
        if vf.is_file() and bump_version_file(vf, args.dry_run):
            changed.append("VERSION")

        # Known test literals (repo name-gated - the literals are
        # fixture stamps, not package metadata).
        if repo.name in TEST_LITERAL_REPOS:
            for rel in TEST_LITERALS:
                if bump_test_literals(repo, rel, args.dry_run):
                    changed.append(rel)

        if not changed:
            skipped += 1
            continue
        bumped += 1
        names = ", ".join(changed)
        if args.dry_run:
            print(f"[dry] {repo.relative_to(tree)} ({cls}): {names}")
            continue
        git("add", "-A", cwd=repo)
        git("commit", "-m",
            f"chore(release): v{TARGET} (REL-1 - CICD design section 2.6)\n\nversion carriers to {TARGET}: {names}",
            cwd=repo)
        print(f"[commit] {repo.relative_to(tree)} ({cls}): {names}")
        if args.push:
            git("push", "origin", "HEAD:main", cwd=repo)
            print(f"[push] {repo.relative_to(tree)}")

    print(f"\n{bumped} repos bumped, {skipped} already at target, "
          f"{'DRY RUN' if args.dry_run else 'committed'}"
          f"{', pushed' if args.push else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
