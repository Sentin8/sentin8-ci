#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>
# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>
# SPDX-FileCopyrightText: Copyright Contributors to the Sentin8:CI
# SPDX-License-Identifier: Apache-2.0
"""lint-workflows.py — validate this repo's workflow files.

Checks (DESIGN §9 self-check lane):
1. Every .github/workflows/*.yml parses as YAML.
2. Every third-party `uses:` reference is pinned to a full 40-char
   commit SHA (self-references ./… and Sentin8/sentin8-ci/… exempt).

Exit 0 = clean, 1 = findings.
"""

import glob
import re
import sys

import yaml


def main() -> int:
    bad = 0
    for f in sorted(glob.glob(".github/workflows/*.yml")):
        try:
            yaml.safe_load(open(f))
        except yaml.YAMLError as e:
            print(f"INVALID: {f}: {e}")
            bad += 1
        for ln, line in enumerate(open(f), 1):
            s = line.strip()
            if not s.startswith("uses:"):
                continue
            ref = s.split("uses:")[1].strip().split("#")[0].strip()
            if not ref or ref.startswith(("./", ".")) or ref.startswith("Sentin8/sentin8-ci/"):
                continue
            if not re.match(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", ref):
                print(f"NOT SHA-PINNED: {f}:{ln}: {ref}")
                bad += 1
    if bad:
        return 1
    print("workflows valid + SHA-pinned")
    return 0


if __name__ == "__main__":
    sys.exit(main())