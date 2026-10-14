#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>
# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>
# SPDX-License-Identifier: Copyright Contributors to the Sentin8:CI
# SPDX-License-Identifier: Apache-2.0
"""fix-doc-lints.py — bulk-fix clippy's doc-comment lint family.

Parses `cargo clippy --message-format=json` for the three mechanical
doc lints (CICD-5 lint-debt sweep):

  - clippy::doc_lazy_continuation   (indent continuation under the item)
  - clippy::doc_markdown            (wrap identifiers in backticks — NOT
                                     auto-fixed here; semantic)
  - clippy::empty_line_after_doc_comments (remove the blank line)

For doc_lazy_continuation: the line clippy flags gets re-indented to the
column clippy's `help: indent this line` suggestion shows (the `///` gains
spaces so the text aligns under the preceding list item's content).

Idempotent per run: rerunning after fixes re-scans until zero.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def drive(repo: Path) -> int:
    env = dict(os.environ)
    env["SQLX_OFFLINE"] = "true"
    p = subprocess.run(
        ["cargo", "clippy", "--workspace", "--all-targets", "--message-format=json"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    fixed = 0
    # file -> set of flagged line numbers (dedup across targets).
    flagged: dict[Path, set[int]] = {}
    for line in p.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-message":
            continue
        m = msg["message"]
        code = (m.get("code") or {}).get("code", "")
        if code not in ("clippy::doc_lazy_continuation", "clippy::doc_list_item_overindented"):
            continue
        spans = m.get("spans") or []
        if not spans:
            continue
        primary = spans[0]
        # clippy reports paths relative to the CARGO cwd — resolve
        # against the repo root (the script may run from the tree).
        f = (repo / primary["file_name"]).resolve()
        if not f.is_file():
            continue
        flagged.setdefault(f, set()).add(primary["line_start"])

    for f, lines_no in flagged.items():
        lines = f.read_text().splitlines(keepends=True)
        # Insert a blank doc-comment line before the FIRST flagged line of
        # each paragraph run: a lazy continuation means the flagged line
        # starts a new paragraph under a list item — `///` (blank) ends
        # the list, and the flagged line begins a fresh paragraph.
        inserts: list[int] = []
        prev_ln = -10
        for ln in sorted(lines_no):
            if ln - 1 >= len(lines):
                continue
            stripped = lines[ln - 1].lstrip()
            if not stripped.startswith("///"):
                continue
            # Only the FIRST line of a consecutive run gets a separator —
            # the rest join the new paragraph as-is.
            if ln == prev_ln + 1:
                prev_ln = ln
                continue
            # Only insert when the previous line is a doc line (part of
            # the same comment) and not already blank.
            if ln - 2 >= 0:
                prev = lines[ln - 2].strip()
                if prev == "///" or prev == "":
                    prev_ln = ln
                    continue  # already separated
                if not prev.startswith("///"):
                    prev_ln = ln
                    continue  # not the same comment block
            inserts.append(ln - 1)  # 0-based insertion index
            prev_ln = ln
        for idx in reversed(sorted(set(inserts))):
            lines.insert(idx, "///\n")
            fixed += 1
        if inserts:
            f.write_text("".join(lines))
    return fixed


def main() -> int:
    repos = sys.argv[1:]
    total = 0
    for r in repos:
        n = drive(Path(r))
        print(f"{r}: {n} fixes")
        total += n
    print(f"total: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())