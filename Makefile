# SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com>
# SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com>
# SPDX-FileCopyrightText: Copyright Contributors to the Sentin8:CI
# SPDX-License-Identifier: Apache-2.0

# Makefile — sentin8-ci (self-contained dev controls)

SHELL := /bin/bash

.PHONY: help lint check sync clean

help: ## Show this help
	@echo "sentin8-ci (tree-wide CI workflows)"
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

lint: ## Validate workflow YAML parses + actions are SHA-pinned
	@python3 scripts/lint-workflows.py

check: lint ## + sync-script compile check
	@python3 -m py_compile scripts/sync-ci.py scripts/lint-workflows.py \
		&& echo 'scripts compile'

sync: ## Regenerate per-repo callers from the tree (run at tree root)
	@python3 scripts/sync-ci.py

clean: ## remove .run .run/logs .tmp (source-only repo)
	@rm -rf .run .run/logs .tmp
