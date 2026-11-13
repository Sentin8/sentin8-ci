#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>
# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>
# SPDX-License-Identifier: Apache-2.0
"""release-tag.py - cut release tags and drive the pin-bump cascade (REL-1).

Two phases:

1. `--tag <repo-name>`: push the release tag for one repo in the tree
   (SDK repos use their derived `{prefix}-v1.0.0`, everything else
   `v1.0.0`). Tags are cut ONE AT A TIME (the documented >3-tags-at-
   once repository_dispatch loss rule, CICD §2.2) and only when the
   tagged commit is exactly the release commit (clean tree, HEAD ==
   origin/main).

2. `--cascade <sdk-name> <tag>`: the MANUAL pin-bump cascade (the
   automated cascade stays unarmed - no GitHub App secrets). For every
   consumer of an SDK repo: rewrite every Cargo.toml pin of that SDK to
   the new tag, `cargo update` the lock, rebuild metadata, commit
   `chore: bump <sdk> to <tag>`, push, then (optionally, --tag-consumers)
   retag the consumer when IT is itself an SDK (the mini-facade ring
   closes one link at a time - CICD §2.2).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TARGET = "1.0.0"
SKIP_DIRS = {".git", "target", "node_modules", ".tmp", "vendor", "build", ".dart_tool"}


def git(*args: str, cwd: Path, check: bool = True) -> str:
    r = subprocess.run(("git",)+args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
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


def sdk_prefixes(tree: Path) -> dict[str, str]:
    """slug -> tag prefix for SDK repos (derived like sync-ci.py)."""
    spec_path = tree / "sentin8" / "sentin8-ci" / "scripts" / "sync-ci.py"
    if not spec_path.is_file():
        return {}
    import importlib.util
    spec = importlib.util.spec_from_file_location("sync_ci", spec_path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["sync_ci"] = m
    spec.loader.exec_module(m)
    repos = m.discover(tree)
    m.derive_sdk_graph(repos)
    return {r.slug: r.sdk_tag_prefix for r in repos if r.sdk_tag_prefix}, {r.slug: r for r in repos}


def consumers_map(tree: Path) -> dict[str, list[str]]:
    p = tree / "sentin8" / "sentin8-ci" / "consumers.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def remote_slug(repo: Path) -> str | None:
    out = git("remote", "get-url", "origin", cwd=repo, check=False)
    m = re.search(r"github\.com[/:]([^/]+)/([^/\s]+?)(?:\.git)?$", out)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def do_tag(tree: Path, name: str, tag: str | None) -> int:
    prefixes, _ = sdk_prefixes(tree)
    for repo in discover(tree):
        slug = remote_slug(repo)
        if not slug or not slug.endswith(f"/{name}"):
            continue
        pfx = prefixes.get(slug)
        t = tag or (f"{pfx}{TARGET}" if pfx else f"v{TARGET}")
        head = git("rev-parse", "HEAD", cwd=repo)
        pushed = git("rev-parse", "origin/main", cwd=repo, check=False)
        if head != pushed:
            print(f"!! {slug}: HEAD != origin/main - not tagging")
            return 1
        if git("status", "--porcelain", cwd=repo, check=False).strip():
            print(f"!! {slug}: dirty tree - not tagging")
            return 1
        git("tag", t, cwd=repo)
        git("push", "origin", t, cwd=repo)
        print(f"[tag] {slug} -> {t} (HEAD {head[:12]})")
        return 0
    print(f"!! repo {name} not found in tree")
    return 1


def do_cascade(tree: Path, sdk_name: str, tag: str, tag_consumers: bool) -> int:
    cmap = consumers_map(tree)
    slug = None
    for s in cmap:
        if s.endswith(f"/{sdk_name}"):
            slug = s
            break
    if not slug:
        print(f"!! {sdk_name} is not a cascade source in consumers.json")
        return 1
    name2path = {}
    for repo in discover(tree):
        s = remote_slug(repo)
        if s:
            name2path[s] = repo
    consumers = cmap[slug]
    print(f"cascade {slug} @{tag} -> {len(consumers)} consumers")
    n = 0
    for cslug in consumers:
        repo = name2path.get(cslug)
        if not repo:
            print(f"  -- {cslug}: not on disk, skipped")
            continue
        # rewrite every pin of this SDK repo across Cargo.tomls
        changed = []
        for m in sorted(repo.rglob("Cargo.toml")):
            if any(part in SKIP_DIRS for part in m.relative_to(repo).parts):
                continue
            txt = m.read_text()
            pat = re.compile(r'(git = "https://github\.com/' + re.escape(slug) +
                             r'"[^}]*?tag = ")[^"]+(")')
            new = pat.sub(rf"\g<1>{tag}\g<2>", txt)
            if new != txt:
                m.write_text(new)
                changed.append(str(m.relative_to(repo)))
        if not changed:
            print(f"  -- {cslug}: no pins of {slug} (consumer of another sdk)")
            continue
        # refresh the lock (pins are git tags - cargo update re-resolves)
        env = {"SQLX_OFFLINE": "true", **__import__("os").environ}
        r = subprocess.run(["cargo", "update", "--workspace"], cwd=repo,
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            print(f"  !! {cslug}: cargo update failed: {r.stderr.strip()[:160]}")
            continue
        git("add", "-A", cwd=repo)
        git("commit", "-m",
            f"chore: bump {slug} to {tag} (REL-1 cascade)\n\n"
            f"pins rewritten: {', '.join(changed)}", cwd=repo)
        git("push", "origin", "HEAD:main", cwd=repo)
        n += 1
        print(f"  [bump] {cslug} -> {tag}")
        if tag_consumers:
            prefixes2, _ = sdk_prefixes(tree)
            pfx = prefixes2.get(cslug)
            if pfx:
                t = f"{pfx}{TARGET}"
                git("tag", t, cwd=repo)
                git("push", "origin", t, cwd=repo)
                print(f"  [tag] {cslug} -> {t}")
    print(f"\ncascade done: {n} consumers bumped to {tag}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", help="cut the release tag for this repo name")
    ap.add_argument("--tag-name", help="override the tag name (default derived)")
    ap.add_argument("--cascade", help="manual pin-bump cascade for this sdk repo name")
    ap.add_argument("--cascade-tag", default=f"*-v{TARGET}", help="tag to cascade (literal)")
    ap.add_argument("--tag-consumers", action="store_true",
                    help="also cut the consumer's own sdk tag when it IS an sdk")
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    tree = Path(args.root).resolve()
    if args.tag:
        return do_tag(tree, args.tag, args.tag_name)
    if args.cascade:
        return do_cascade(tree, args.cascade, args.cascade_tag, args.tag_consumers)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
