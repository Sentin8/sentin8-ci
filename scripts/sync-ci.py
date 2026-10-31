#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>
# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>
# SPDX-FileCopyrightText: Copyright Contributors to the Sentin8:CI
# SPDX-License-Identifier: Apache-2.0
"""sync-ci.py — generate per-repo CI callers from the tree (CICD design §2.4).

Run from the aggregation tree root (the directory holding sentin8/, bank8/,
hotel8/, …). The script:

1. Classifies every git repository on disk by its markers (§1.4):
   rust | flutter | e2e | docs
2. Parses every Cargo.toml git-tag pin to derive:
   - the SDK-repo set (repos other repos pin by git tag),
   - each SDK's tag prefix (from the tags its consumers pin),
   - the consumer graph (SDK pin name → consumer repos).
3. Writes into each repo:
   - .github/workflows/ci.yml         — class-appropriate thin caller
   - .github/workflows/release.yml    — SDK repos only (tag triggers)
   - .github/workflows/sdk-bump.yml   — consumer repos only (dispatch receiver)
   - rust-toolchain.toml              — Rust class only
   - .github/dependabot.yml           — github-actions updates
4. Regenerates this ci repo's repos.json + consumers.json manifests.

Idempotent: re-running rewrites the same bytes. --dry-run prints without
writing; --check exits nonzero if any repo would change (drift gate).

Caller files are GENERATED — never hand-edit.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CI_REF = "ci-v1"
CI_REPO = "Sentin8/sentin8-ci"
RUSTC_VERSION = "1.98.1"
FLUTTER_VERSION = "3.47.2"
ROOT_ENV_PATH_DEP = "../../sentin8/sentin8-flutter"

# ── repo classification ──────────────────────────────────────────────


@dataclass
class Repo:
    path: Path
    org: str
    name: str
    cls: str = "docs"  # rust | flutter | e2e | docs
    # SDK repo slug → tag this repo pins (e.g. {"Sentin8/sentin8-sdk":
    # "sdk-v0.6.3"} — a pinned workspace exports many crates, one tag).
    pins: dict[str, str] = field(default_factory=dict)
    # SDK repo only: the tag prefix its releases use.
    sdk_tag_prefix: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.org}/{self.name}"


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def remote_slug(repo: Path) -> tuple[str, str] | None:
    """(org, name) from the repo's origin remote; None if unset."""
    try:
        url = sh("git", "-C", str(repo), "remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        return None
    m = re.match(r"(?:https://github\.com/|git@github\.com:)([^/]+)/(.+?)(?:\.git)?$", url)
    if not m:
        return None
    return m.group(1), m.group(2)


def classify(repo: Path) -> str:
    if (repo / "playwright.config.ts").is_file() and (repo / "package.json").is_file():
        return "e2e"
    if (repo / "pubspec.yaml").is_file():
        return "flutter"
    if (repo / "Cargo.toml").is_file():
        return "rust"
    if (repo / "site.json").is_file():
        return "site"
    return "docs"


PIN_RE = re.compile(
    r'^(?P<dep>\S+) = \{ git = "https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^"]+?)(?:\.git)?", '
    r'tag = "(?P<tag>[^"]+)" \}',
    re.M,
)


def parse_pins(repo: Path) -> dict[str, str]:
    """SDK repo slug → pinned tag (keyed by REPO, not dep name — a
    git-pinned workspace exports many crates under one tag)."""
    pins: dict[str, str] = {}
    for mf in repo.rglob("Cargo.toml"):
        if "/target/" in str(mf) or "/.git/" in str(mf):
            continue
        for m in PIN_RE.finditer(mf.read_text()):
            pins[f"{m.group('owner')}/{m.group('repo')}"] = m.group("tag")
    return pins


def flutter_path_dep(repo: Path) -> str:
    """The repo's sentin8-flutter path dependency as written in pubspec, or ''."""
    pub = repo / "pubspec.yaml"
    if not pub.is_file():
        return ""
    txt = pub.read_text()
    m = re.search(r"sentin8:\s*\n\s+path:\s*(\S+)", txt)
    return m.group(1) if m else ""


def discover(tree: Path) -> list[Repo]:
    repos: list[Repo] = []
    for gitdir in sorted(tree.rglob(".git")):
        s = str(gitdir)
        # Scratch dirs (.tmp/, .run/) may hold extracted backup .git
        # snapshots — never sync targets (AGENTS: scratch under .tmp/).
        # Nested org-profile clones (<group>/.github → <org>/.github) are
        # README/profile repositories — not CI sync targets either.
        if (
            "/target/" in s
            or "/node_modules/" in s
            or "/.tmp/" in s
            or "/.run/" in s
            or gitdir.parent.name == ".github"
        ):
            continue
        repo = gitdir.parent
        slug = remote_slug(repo)
        if slug is None:
            continue
        org, name = slug
        r = Repo(path=repo, org=org, name=name)
        r.cls = classify(repo)
        if r.cls == "rust":
            r.pins = parse_pins(repo)
        repos.append(r)
    return repos


# ── SDK graph derivation ─────────────────────────────────────────────


def derive_sdk_graph(repos: list[Repo]) -> None:
    """Populate sdk_tag_prefix + the consumer graph.

    A repo is an SDK repo iff some other repo pins it BY REPO SLUG. The
    consumer graph is keyed by the SDK's repo slug: one release → one
    dispatch per consumer, whose receiver rewrites EVERY pin line
    pointing at that repo (a pinned workspace exports many crates —
    `sentin8`, `sentin8-middleware`, `sdk_git`, … — under one tag).
    """
    consumers: dict[str, set[str]] = {}
    sdk_tags: dict[str, set[str]] = {}

    for r in repos:
        if r.cls != "rust":
            continue
        for sdk_slug, tag in r.pins.items():
            consumers.setdefault(sdk_slug, set()).add(r.slug)
            sdk_tags.setdefault(sdk_slug, set()).add(tag)

    for r in repos:
        if r.cls != "rust":
            continue
        if r.slug in sdk_tags:
            r.sdk_tag_prefix = common_tag_prefix(sdk_tags[r.slug])


def common_tag_prefix(tags: set[str]) -> str:
    """sdk-v0.6.3 + sdk-v0.6.2 → 'sdk-v'. cron-sdk-v0.6.2 → 'cron-sdk-v'."""
    if not tags:
        return ""
    prefix = ""
    for candidate in sorted(tags):
        m = re.match(r"^(.*?v)\d", candidate)
        if m:
            return m.group(1)
    return ""


# ── templates ────────────────────────────────────────────────────────

SPDX = [
    "# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>",
    "# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>",
    "# SPDX-FileCopyrightText: Copyright Contributors to the Sentin8:CI",
    "# SPDX-License-Identifier: Apache-2.0",
    "",
]

CALLER_HEADER = SPDX + [
    "# GENERATED by sentin8-ci/scripts/sync-ci.py — do not edit by hand",
    "# Regenerate with a sync run from the tree root.",
    "",
]


def caller_ci(r: Repo) -> str:
    # The aggregation root hosts the NIGHTLY caller (schedule + manual
    # dispatch), not the docs lane — it is the only repo that runs the
    # whole-tree verification (design §2.3).
    if r.name == "verticals":
        body = f"""name: nightly
on:
  schedule:
    - cron: '0 3 * * *'
  workflow_dispatch:

permissions:
  contents: read

jobs:
  nightly:
    uses: {CI_REPO}/.github/workflows/aggregation-nightly.yml@{CI_REF}
"""
        return "\n".join(CALLER_HEADER + body.splitlines()) + "\n"
    body = {
        "rust": f"""name: ci
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ci-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  ci:
    uses: {CI_REPO}/.github/workflows/rust.yml@{CI_REF}
""",
        "flutter": f"""name: ci
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ci-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  ci:
    uses: {CI_REPO}/.github/workflows/flutter-app.yml@{CI_REF}
    with:
      flutter-version: '{FLUTTER_VERSION}'
      sentin8-flutter-path: '{flutter_path_dep(r.path)}'
      build-web: {'true' if (r.path / 'web').is_dir() else 'false'}
""",
        "e2e": f"""name: ci
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ci-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  ci:
    uses: {CI_REPO}/.github/workflows/e2e-repo.yml@{CI_REF}
""",        "site": f"""name: ci
# Site lane caller (Sentin8/sentin8-ci site.yml @ci-v1).
# Push to main: harvest + build + deploy to GitHub Pages.

on:
  push:
    branches: [main]
  pull_request:
  schedule:
    # Weekly harvest refresh: rebuild so sibling-repo doc changes land
    # on the site without a site-repo commit (harvest pulls public main).
    - cron: '0 5 * * 1'

permissions:
  contents: read

jobs:
  ci:
    uses: {CI_REPO}/.github/workflows/site.yml@{CI_REF}
    with:
      site-dir: .
      deploy: ${{{{ github.event_name != 'pull_request' }}}}
    permissions:
      contents: read
      pages: write
      id-token: write
""",
        "docs": f"""name: ci
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ci-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  ci:
    uses: {CI_REPO}/.github/workflows/docs-group.yml@{CI_REF}
    with:
      has-compose: {'true' if (r.path / 'docker-compose.yml').is_file() else 'false'}
""",
    }[r.cls]
    return "\n".join(CALLER_HEADER + body.splitlines()) + "\n"


def caller_release(r: Repo) -> str:
    body = f"""name: release
# SDK tag-push release lane: builds the tag, fans out the pin-bump
# cascade to consumers (gated behind the cascade GitHub App secrets).
on:
  push:
    tags: ['{r.sdk_tag_prefix}*']

jobs:
  release:
    uses: {CI_REPO}/.github/workflows/sdk-release.yml@{CI_REF}
    with:
      sdk-repo: '{r.slug}'
    secrets:
      SENTIN8_CI_APP_ID: ${{{{ secrets.SENTIN8_CI_APP_ID }}}}
      SENTIN8_CI_APP_PRIVATE_KEY: ${{{{ secrets.SENTIN8_CI_APP_PRIVATE_KEY }}}}
"""
    return "\n".join(CALLER_HEADER + body.splitlines()) + "\n"


def caller_sdk_bump() -> str:
    body = f"""name: sdk-bump
# Pin-bump receiver: runs when an upstream SDK publishes a tag and the
# cascade dispatches sdk-released. Opens a pin-bump PR.
on:
  repository_dispatch:
    types: [sdk-released]

jobs:
  bump:
    uses: {CI_REPO}/.github/workflows/sdk-bump.yml@{CI_REF}
    secrets:
      SENTIN8_CI_APP_ID: ${{{{ secrets.SENTIN8_CI_APP_ID }}}}
      SENTIN8_CI_APP_PRIVATE_KEY: ${{{{ secrets.SENTIN8_CI_APP_PRIVATE_KEY }}}}
"""
    return "\n".join(CALLER_HEADER + body.splitlines()) + "\n"


RUST_TOOLCHAIN = "\n".join(SPDX + [
    "[toolchain]",
    f'channel = "{RUSTC_VERSION}"',
    "components = [\"rustfmt\", \"clippy\"]",
    "",
])


DEPENDABOT = "\n".join(CALLER_HEADER + [
    "version: 2",
    "updates:",
    "  - package-ecosystem: github-actions",
    "    directory: /",
    "    schedule:",
    "      interval: weekly",
    "",
])


# ── main ─────────────────────────────────────────────────────────────


def write(path: Path, content: str, dry: bool, check: bool) -> bool:
    """Write if changed. Returns True when the file changed/created."""
    if path.is_file() and path.read_text() == content:
        return False
    if check:
        print(f"DRIFT: {path}")
        return True
    if dry:
        print(f"would write: {path}")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"wrote: {path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    tree = Path.cwd()
    if not (tree / "sentin8").is_dir():
        print("run from the aggregation tree root (dir holding sentin8/)", file=sys.stderr)
        return 2

    repos = discover(tree)
    if not repos:
        print("no git repos found", file=sys.stderr)
        return 2
    derive_sdk_graph(repos)

    drift = False

    # Per-repo callers.
    consumers: dict[str, list[str]] = {}
    for r in repos:
        # The ci repo itself is never a sync target — its own ci.yml is
        # the hand-written self-check lane (exempted by name).
        if r.name == "sentin8-ci":
            continue
        wf = r.path / ".github" / "workflows"
        # The aggregation root's caller file is `nightly.yml` (schedule
        # + manual dispatch), not `ci.yml` — different trigger surface.
        ci_file = "nightly.yml" if r.name == "verticals" else "ci.yml"
        drift |= write(wf / ci_file, caller_ci(r), args.dry_run, args.check)
        drift |= write(r.path / ".github" / "dependabot.yml",
                       DEPENDABOT, args.dry_run, args.check)
        if r.cls == "rust":
            drift |= write(r.path / "rust-toolchain.toml",
                           RUST_TOOLCHAIN, args.dry_run, args.check)
        if r.cls == "rust" and r.sdk_tag_prefix:
            drift |= write(wf / "release.yml", caller_release(r),
                           args.dry_run, args.check)
        if r.cls == "rust" and r.pins:
            drift |= write(wf / "sdk-bump.yml", caller_sdk_bump(),
                           args.dry_run, args.check)
            for sdk_slug in r.pins:
                consumers.setdefault(sdk_slug, []).append(r.slug)

    # ci-repo manifests (repos.json, consumers.json).
    ci_repo = tree / "sentin8" / "sentin8-ci"
    if ci_repo.is_dir():
        repos_manifest = [
            {"path": str(r.path.relative_to(tree)), "org": r.org,
             "name": r.name, "class": r.cls}
            for r in sorted(repos, key=lambda r: str(r.path))
            if r.name != "sentin8-ci"
        ]
        drift |= write(ci_repo / "repos.json",
                       json.dumps(repos_manifest, indent=2) + "\n",
                       args.dry_run, args.check)
        consumers_manifest = {k: sorted(v) for k, v in sorted(consumers.items())}
        drift |= write(ci_repo / "consumers.json",
                       json.dumps(consumers_manifest, indent=2) + "\n",
                       args.dry_run, args.check)

    sdk_count = sum(1 for r in repos if r.sdk_tag_prefix)
    print(f"\n{len(repos)} repos ({sdk_count} SDK repos) — "
          f"{'DRIFT PRESENT' if drift else 'in sync'}")
    return 1 if (drift and args.check) else 0


if __name__ == "__main__":
    sys.exit(main())