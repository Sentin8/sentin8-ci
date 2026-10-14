#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>
# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>
# SPDX-FileCopyrightText: Copyright Contributors to the Sentin8:CI
# SPDX-License-Identifier: Apache-2.0
"""wipe-history.py — single-commit history wipe across the tree (HIST design).

Reduces every repository to EXACTLY ONE first commit carrying the current
working tree (WIP folded in), message verbatim:

    Sync from SVN and remove client/project-related code.

and keeps the tag graph the Cargo pins resolve through functional:

- every PINNED tag (from consumers' Cargo.toml git-tag pins) is
  re-created on the snapshot (old content converges to the snapshot —
  owner decision; stale pins included),
- unpinned historical tags are dropped with the history,
- every consumer Cargo.lock git-source rev is rewritten to the new tag
  target BEFORE its own snapshot (DAG order: platform SDK → cross-pinned
  mini-facades → vertical SDKs → services → everything else).

Safety: an assertion pass runs over ALL repos BEFORE any mutation; any
failure aborts the whole run. Idempotent per repo: re-running resumes
(a repo whose main already has exactly 1 commit with the right message
is only verified + pushed). The operator keeps a backup tarball of all
.git dirs OUTSIDE the tree (design §4) — this script refuses to run
without one (pass --backup-path).

Usage (from the aggregation tree root):
    python3 sentin8/sentin8-ci/scripts/wipe-history.py --backup-path <tarball>
    … --dry-run        assertions + plan only, no mutation
    … --repo <path>    pilot a single repo (relative path, e.g. sentin8/sentin8-sdk)
    … --no-push        skip the push phase
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

COMMIT_MESSAGE = "Sync from SVN and remove client/project-related code."

DAG_PLATFORM = [
    # Platform plane in TOPOLOGICAL pin order (HIST §2 constraint 4;
    # cross-pin edges verified live: flow→func, func→storage,
    # post→secrets, app→{secrets,stat}, bff→stat,
    # ai→{cron,flow,secrets,stat,storage}):
    #   sdk → storage → {secrets, stat, func} → {cron, flow, post,
    #   app, bff} → ai → starters/services/docs.
    "sentin8/sentin8-sdk",
    "sentin8/sentin8-storage",
    "sentin8/sentin8-secrets", "sentin8/sentin8-stat", "sentin8/sentin8-func",
    "sentin8/sentin8-cron", "sentin8/sentin8-flow",
    "sentin8/sentin8-post", "sentin8/sentin8-app", "sentin8/sentin8-bff",
    "sentin8/sentin8-ai",
    "sentin8/sentin8-bus",
    "sentin8/sentin8-cli", "sentin8/sentin8-captain",
    "sentin8/sentin8-acme", "sentin8/sentin8-auth",
    "sentin8/sentin8-proxy", "sentin8/sentin8-web", "sentin8/sentin8-sys",
    "sentin8/sentin8-flutter", "sentin8/sentin8-auth-ui",
    "sentin8/sentin8-dash", "sentin8/sentin8-e2e", "sentin8/sentin8-packaging",
    "sentin8/sentin8",
]

VERTICALS = ["bank8", "hotel8", "trade8", "bill8", "email8", "exchange8"]

PIN_RE = re.compile(
    r'git = "https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^"]+?)(?:\.git)?", '
    r'tag = "(?P<tag>[^"]+)"'
)

LOCK_SOURCE_RE = re.compile(
    r'source = "git\+(?P<url>https://github\.com/[^?]+)\?tag=(?P<tag>[^#"]+)#(?P<rev>[0-9a-f]{40})"'
)


def sh(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"command failed ({' '.join(args)}): {r.stderr.strip()[:500]}")
    return r.stdout.strip()


@dataclass
class Repo:
    path: Path
    slug: str          # owner/name from the origin remote ('' if none)
    pinned_tags: list[str] = field(default_factory=list)   # tags OTHERS pin on this repo
    pins: list[tuple[str, str]] = field(default_factory=list)  # (sdk slug, tag) this repo pins
    new_sha: str | None = None
    fresh: bool = False   # never-initialized repo (sentin8-ci)

    @property
    def has_git(self) -> bool:
        return (self.path / ".git").exists()


def discover(tree: Path) -> list[Repo]:
    repos: list[Repo] = []
    seen: set[Path] = set()
    for gitdir in sorted(tree.rglob(".git")):
        if "/target/" in str(gitdir) or "/node_modules/" in str(gitdir):
            continue
        repo = gitdir.parent.resolve()
        if repo in seen:
            continue
        seen.add(repo)
        repos.append(Repo(path=repo, slug=remote_slug(repo)))
    # The fresh, never-initialized ci repo joins the sweep.
    ci = tree / "sentin8" / "sentin8-ci"
    if (ci / ".git").exists():
        pass  # already initialized (pilot or re-run) — discovered normally
    elif ci.is_dir():
        repos.append(Repo(path=ci.resolve(), slug="Sentin8/sentin8-ci",
                          fresh=True))
    return repos


def remote_slug(repo: Path) -> str:
    try:
        url = sh("git", "-C", str(repo), "remote", "get-url", "origin", check=False)
    except Exception:
        return ""
    if not url:
        return ""
    m = re.match(r"(?:https://github\.com/|git@github\.com:)([^/]+)/(.+?)(?:\.git)?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def collect_pin_graph(repos: list[Repo]) -> None:
    """Pinned tags per SDK repo + pin list per consumer (from Cargo.tomls)."""
    for r in repos:
        if r.fresh or not r.has_git:
            continue
        for mf in r.path.rglob("Cargo.toml"):
            if "/target/" in str(mf) or "/.git/" in str(mf) or "/node_modules/" in str(mf):
                continue
            for m in PIN_RE.finditer(mf.read_text()):
                slug = f"{m.group('owner')}/{m.group('repo')}"
                r.pins.append((slug, m.group("tag")))
    pinned_by_slug: dict[str, set[str]] = {}
    for r in repos:
        for slug, tag in r.pins:
            pinned_by_slug.setdefault(slug, set()).add(tag)
    for r in repos:
        r.pinned_tags = sorted(pinned_by_slug.get(r.slug, set()))


def dag_order(repos: list[Repo], tree: Path) -> list[Repo]:
    """Wipe order: pinned SDK repos before their consumers.

    Fixed known order first (platform plane, HIST §2), then each
    vertical group (SDK repo before its services), then everything
    remaining (group folders, root, health8).
    """
    by_rel = {str(r.path.relative_to(tree)): r for r in repos}
    order: list[Repo] = []

    def take(rel: str) -> None:
        r = by_rel.pop(rel, None)
        if r is not None:
            order.append(r)

    for rel in DAG_PLATFORM:
        take(rel)
    for v in VERTICALS:
        take(f"{v}/{v}-sdk")
        for rel in sorted(list(by_rel)):
            if rel.startswith(f"{v}/"):
                take(rel)
    # Cross-pinned stragglers + everything else (root '.', groups, health8).
    take("health8")
    for rel in ["."]:
        take(rel)
    order.extend(by_rel.values())
    return order


def assert_preconditions(repos: list[Repo], tree: Path) -> list[str]:
    """Whole-tree assertions BEFORE any mutation. Returns problem list."""
    problems: list[str] = []
    for r in repos:
        rel = str(r.path.relative_to(tree))
        if r.fresh:
            continue
        if sh("git", "-C", str(r.path), "rev-parse", "--abbrev-ref", "HEAD", check=False) != "main":
            problems.append(f"{rel}: HEAD is not on main")
        ls = sh("git", "-C", str(r.path), "ls-files", "-ci", "--exclude-standard", check=False)
        if ls:
            problems.append(f"{rel}: tracked-but-ignored files: {ls.splitlines()[:3]}")
        if sh("git", "-C", str(r.path), "stash", "list", check=False):
            problems.append(f"{rel}: has stashes")
    slugs = {r.slug for r in repos if r.slug}
    for r in repos:
        for slug, _tag in r.pins:
            if slug not in slugs:
                problems.append(f"{str(r.path.relative_to(tree))}: pins unknown repo {slug}")
    return problems


LOCK_SOURCE_RE = re.compile(
    r'source = "git\+(?P<url>https://github\.com/[^?]+)\?tag=(?P<tag>[^#"]+)#(?P<rev>[0-9a-f]{40})"'
)


def rewrite_locks(repo: Repo, new_shas: dict[str, str], dry: bool) -> bool:
    """Rewrite every Cargo.lock git-source rev to the snapshot SHA.

    Returns True when any lock changed. A lock referencing an upstream
    that has no snapshot yet is a hard error (DAG violation).
    """
    changed = False
    for lock in repo.path.rglob("Cargo.lock"):
        if "/target/" in str(lock) or "/.git/" in str(lock) or "/node_modules/" in str(lock):
            continue
        txt = lock.read_text()

        def sub(m: re.Match) -> str:
            slug = m.group("url").removeprefix("https://github.com/")
            if slug not in new_shas:
                raise RuntimeError(
                    f"{lock}: pins {slug} which has no snapshot yet (DAG violation)")
            return f'source = "git+{m.group("url")}?tag={m.group("tag")}#{new_shas[slug]}"'

        new_txt, n = LOCK_SOURCE_RE.subn(sub, txt)
        if n and new_txt != txt:
            changed = True
            print(f"    locks: {n} git revs rewritten in {lock.relative_to(repo.path)}")
            if not dry:
                lock.write_text(new_txt)
    return changed


def is_wiped(repo: Repo) -> bool:
    """Already at target state: single commit, exact message."""
    if repo.fresh:
        return False
    n = sh("git", "-C", str(repo.path), "rev-list", "--count", "main", check=False)
    msg = sh("git", "-C", str(repo.path), "log", "-1", "--format=%s", check=False)
    return n == "1" and msg == COMMIT_MESSAGE


def wipe_repo(repo: Repo, new_shas: dict[str, str], dry: bool, tree: Path) -> None:
    rel = str(repo.path.relative_to(tree))
    if not repo.fresh and is_wiped(repo):
        new_shas[repo.slug] = sh("git", "-C", str(repo.path), "rev-parse", "HEAD")
        print(f"  {rel}: already wiped — verified")
        return
    if repo.pins:
        rewrite_locks(repo, new_shas, dry)
    if dry:
        print(f"  {rel}: WOULD wipe ({len(repo.pins)} pin targets, "
              f"{len(repo.pinned_tags)} pinned tags to re-create)")
        return
    if repo.fresh:
        # Never-initialized repo (sentin8-ci): the snapshot IS the init.
        sh("git", "-C", str(repo.path), "init", "-b", "main")
        sh("git", "-C", str(repo.path), "add", "-A")
        sh("git", "-C", str(repo.path), "commit", "-m", COMMIT_MESSAGE)
        for t in repo.pinned_tags:
            sh("git", "-C", str(repo.path), "tag", t)
        new_shas[repo.slug] = sh("git", "-C", str(repo.path), "rev-parse", "HEAD")
        print(f"  {rel}: initialized + first commit → {new_shas[repo.slug][:8]}")
        return
    # Snapshot: orphan branch carrying exactly the worktree.
    sh("git", "-C", str(repo.path), "checkout", "--orphan", "__wipe__")
    sh("git", "-C", str(repo.path), "rm", "-rf", "--cached", ".")
    sh("git", "-C", str(repo.path), "add", "-A")
    sh("git", "-C", str(repo.path), "commit", "-m", COMMIT_MESSAGE)
    # Replace main with the snapshot; drop ALL history + tags.
    sh("git", "-C", str(repo.path), "branch", "-m", "main", "__old_main__")
    sh("git", "-C", str(repo.path), "branch", "-m", "__wipe__", "main")
    sh("git", "-C", str(repo.path), "branch", "-D", "__old_main__")
    for t in sh("git", "-C", str(repo.path), "tag").splitlines():
        sh("git", "-C", str(repo.path), "tag", "-d", t)
    # Re-create ONLY the pinned tags on the snapshot (HIST §3).
    for t in repo.pinned_tags:
        sh("git", "-C", str(repo.path), "tag", t)
    # Prune unreachable objects locally (remote handled by force-push).
    sh("git", "-C", str(repo.path), "reflog", "expire", "--expire=now", "--all")
    sh("git", "-C", str(repo.path), "gc", "--prune=now", "--quiet")
    new_shas[repo.slug] = sh("git", "-C", str(repo.path), "rev-parse", "HEAD")
    print(f"  {rel}: wiped → {new_shas[repo.slug][:8]} "
          f"({len(repo.pinned_tags)} tags re-created)")


def push_repo(repo: Repo, dry: bool, tree: Path, done: set[str]) -> None:
    rel = str(repo.path.relative_to(tree))
    if repo.slug in done or not repo.slug:
        return
    if dry:
        print(f"  {rel}: WOULD force-push main + {len(repo.pinned_tags)} tags")
        return
    if repo.fresh:
        # First push: create the remote repo branch (operator created the
        # empty GitHub repo during rollout, or push fails loudly).
        sh("git", "-C", str(repo.path), "push", "-u", "origin", "main")
    else:
        sh("git", "-C", str(repo.path), "push", "--force", "origin", "main")
    remote = sh("git", "-C", str(repo.path), "ls-remote", "--tags", "origin", check=False)
    stale = [ln.split("refs/tags/")[-1].strip() for ln in remote.splitlines()
             if "refs/tags/" in ln and "^{}" not in ln]
    if stale:
        sh("git", "-C", str(repo.path), "push", "origin", "--delete",
           *[f"refs/tags/{t}" for t in stale])
    if repo.pinned_tags:
        sh("git", "-C", str(repo.path), "push", "origin", *repo.pinned_tags)
    # Re-anchor remote-tracking refs to the snapshot so the old chain is
    # locally unreachable too, then purge it (HIST §3 completeness: gc
    # alone cannot drop commits the remote-tracking ref still holds).
    sh("git", "-C", str(repo.path), "fetch", "--prune", "origin")
    sh("git", "-C", str(repo.path), "reflog", "expire", "--expire=now", "--all")
    sh("git", "-C", str(repo.path), "gc", "--prune=now", "--quiet")
    done.add(repo.slug)
    print(f"  {rel}: pushed (main, {len(repo.pinned_tags)} tags, "
          f"{len(stale)} stale remote tags deleted)")


def verify(repo: Repo, tree: Path, problems: list[str]) -> None:
    rel = str(repo.path.relative_to(tree))
    n = sh("git", "-C", str(repo.path), "rev-list", "--count", "main", check=False)
    msg = sh("git", "-C", str(repo.path), "log", "-1", "--format=%s", check=False)
    if n != "1":
        problems.append(f"{rel}: {n} commits on main (want 1)")
    if msg != COMMIT_MESSAGE:
        problems.append(f"{rel}: message mismatch: {msg!r}")
    head = sh("git", "-C", str(repo.path), "rev-parse", "HEAD", check=False)
    for t in repo.pinned_tags:
        tgt = sh("git", "-C", str(repo.path), "rev-list", "-1", t, check=False)
        if tgt != head:
            problems.append(f"{rel}: tag {t} not at HEAD")
    if sh("git", "-C", str(repo.path), "status", "--porcelain", check=False):
        problems.append(f"{rel}: worktree not clean")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backup-path", required=True,
                    help="tarball of all .git dirs (refuses to run without)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--repo", default=None,
                    help="pilot a single repo (relative path)")
    args = ap.parse_args()
    if not Path(args.backup_path).is_file():
        print(f"backup tarball not found: {args.backup_path}", file=sys.stderr)
        return 2

    tree = Path.cwd()
    if not (tree / "sentin8").is_dir():
        print("run from the aggregation tree root", file=sys.stderr)
        return 2

    repos = discover(tree)
    collect_pin_graph(repos)
    ordered = dag_order(repos, tree)
    if args.repo:
        ordered = [r for r in ordered
                   if str(r.path.relative_to(tree)) == args.repo]
        if not ordered:
            print(f"repo not found: {args.repo}", file=sys.stderr)
            return 2

    print(f"── asserting preconditions ({len(ordered)} repos) ──")
    problems = assert_preconditions(ordered, tree)
    if problems:
        print("\n".join(problems[:20]), file=sys.stderr)
        print(f"… {len(problems)} problems — aborting before ANY mutation",
              file=sys.stderr)
        return 1

    print(f"── wiping ({'dry-run' if args.dry_run else 'LIVE'}) ──")
    new_shas: dict[str, str] = {}
    if args.dry_run:
        # Planning pass: seed the map with each upstream's CURRENT tag
        # rev (valid until the real wipe replaces them) so consumers'
        # lock rewrites can be traced without mutation.
        for r in ordered:
            if r.pinned_tags and r.has_git:
                t = r.pinned_tags[-1]
                rev = sh("git", "-C", str(r.path), "rev-list", "-1", t, check=False)
                if re.fullmatch(r"[0-9a-f]{40}", rev):
                    new_shas[r.slug] = rev
    for r in ordered:
        wipe_repo(r, new_shas, args.dry_run, tree)

    if not args.no_push:
        print("── pushing ──")
        done: set[str] = set()
        for r in ordered:
            push_repo(r, args.dry_run, tree, done)

    print("── verifying ──")
    problems: list[str] = []
    for r in ordered:
        if not args.dry_run and r.has_git:
            verify(r, tree, problems)
    if problems:
        print("\n".join(problems[:20]), file=sys.stderr)
        return 1
    print(f"OK — {len(ordered)} repos wiped{' (dry-run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
