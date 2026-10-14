<!-- SPDX-FileCopyrightText: Copyright Logically Magic Inc. <hello@logicallymagic.com> -->
<!-- SPDX-FileCopyrightText: Copyright Hamid Jahanzad <Hamid@Jahanzad.com> -->
<!-- SPDX-FileCopyrightText: Copyright Contributors to the Sentin8:CI -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# BUSINESS.md: Sentin8:CI — Business Requirements & Functionality

> Audience: product owners, business analysts, and anyone evaluating
> what Sentin8:CI **does for the business**. For architecture read
> `README.md`; for wire-level detail read `DESIGN.md`.

## 1. Business Purpose

* **Protect every repository's main branch** — no broken build, test,
  or app bundle reaches `main` anywhere in the ~98-repo tree.
* **Make releases safe and repeatable** — publishing an SDK tag
  automatically verifies it and propagates the pin bump to every
  consumer, replacing a full manual cascade that was error-prone
  (missed facades caused two production incidents).
* **Prove the whole platform works** — a nightly run boots the entire
  fleet from public source and exercises real user journeys end to
  end (browser UI + CLI).
* **Cost nothing on public repos** — runners, security scanning, and
  Dependabot are free; the design deliberately needs no product
  secrets in CI.

## 2. Business Scope (What the Service Provides)

| Capability | Business rule |
|---|---|
| PR quality gate | Every push/PR to `main` runs the class-appropriate checks; blocking ones must pass before merge (once branch protection is on) |
| SDK release verification | A release tag is only "shipped" after its build+tests pass in CI |
| Consumer pin-bump automation | On a verified release, every consumer repo gets a ready-to-merge bump PR with passing CI |
| Whole-tree nightly verification | The fleet boots from scratch nightly; e2e evidence (reports, traces) is retained 30 days |
| Supply-chain hygiene | Actions pinned to hashes; dependencies alert automatically (Dependabot) |
| Tooling distribution | One version tag (`ci-v1`) upgrades every repository's pipeline at once |

### What the service does NOT own (business boundary)

| Out of scope | Owning unit |
|---|---|
| Product build/test logic | Each service repo |
| Org security settings, branch protection | Platform operator (rollout checklist) |
| Deployment/hosting of the platform | Packaging + fleet (captain) planes |
| Local developer experience | Per-repo Makefiles |

## 3. Business Capabilities

### 3.1 Continuous integration per repository

* **Rust repos** (services, SDKs, starters): compile with pinned,
  offline SQL contracts; run the full test suite (database-backed
  suites skip in PRs, run nightly); formatting and lint findings are
  reported without blocking (until the debt story flips them).
* **Flutter apps**: static analysis and tests must pass; the web
  bundle must build with the renderer's icon requirements honored;
  bundles are attached as artifacts.
* **E2E repos**: dependency install and browser fetch must succeed;
  type errors reported advisory.
* **Group/docs repos**: the group's catalog and registry files must
  be valid JSON; the Makefile must parse; the platform compose file
  must be valid.

### 3.2 Release cascade

* An SDK release (tag push) triggers verification; when the operator
  has armed the cascade (GitHub App installed), every consumer repo
  automatically receives a pin-bump pull request with fresh CI.
* Until armed, releases are still verified — only the propagation is
  manual (today's status quo, unchanged).
* The ring closes link by link: mini-facades and vertical SDKs are
  consumers too, so their releases cascade onward automatically.

### 3.3 Nightly whole-tree verification

* Builds the entire platform in dependency order from public source.
* Boots the real infrastructure (PostgreSQL with certificate
  identity, Valkey), seeds it, starts every service under the
  supervisor, and waits for health.
* Runs the admin browser journeys and CLI journeys end to end.
* Audits every dependency pin against published tags (stale pins are
  reported — the manual cascade's blind spot).
* Always uploads evidence, even on failure; always tears the fleet
  down.

## 4. Business Events

No outbound Sentin8:Bus events — this repo is a build-plane tool. Its
GitHub-native events: `repository_dispatch` (`sdk-released`) outbound
in the cascade; reusable-workflow invocations inbound.

## 5. Business Rules & Invariants

1. A blocking check that fails stops the merge — no warnings-as-pass.
2. The cascade never half-fires: unarmed (no App) means fully skipped.
3. Release tags are pushed one at a time (bulk pushes >3 lose events —
   platform rule documented in the operator checklist).
4. Caller workflows are generated; hand edits are drift and fail the
   check.
5. CI never needs product secrets; the only optional credential is
   the cascade App.

## 6. Target-User Roles & Use Cases

| Persona | Use case |
|---|---|
| Service developer | Push a branch → immediate pass/fail + advisory lint; merge with confidence |
| SDK maintainer | Publish a tag → verified release; consumers get bump PRs automatically |
| Platform operator | Arm the cascade once; watch the nightly; audit pin freshness |
| Vertical team lead | Same pipeline as platform repos, no separate setup |
| Auditor | 30-day e2e evidence artifacts; hashed-pinned actions; scan alerts |

## 7. KPIs / Success Criteria

* Time-to-signal on a PR: **under 10 minutes** (fast lane, cached).
* Broken `main` regressions reaching protected branches: **zero**
  (required checks).
* SDK release → all consumer bump PRs opened: **under 1 hour**
  (armed state).
* Nightly green rate measured over rolling month; every red has
  artifacts attached.

## 8. Dependencies & Reliance on Other Services

* **GitHub Actions** (reusable workflows, dispatch API, runners) — the
  entire execution plane.
* **Aggregation root repo** (`Sentin8/verticals`) — hosts the nightly
  caller and the `.env.example` the nightly derives from.
* **The tree's own tooling** — root Makefile (DAG build), captain
  (fleet), Acme (infra TLS), sentin8-cli (bootstrap/enroll), the e2e
  suites.

## 9. Acceptance Criteria

1. Every repository in the tree has a working `ci` workflow whose
   blocking checks pass on its current `main`.
2. An SDK tag push runs its release workflow; unarmed fan-out skips
   cleanly; armed fan-out produces consumer PRs whose CI runs.
3. The aggregation nightly completes a full fleet boot + both e2e
   suites at least once, producing artifacts.
4. `sync-ci.py --check` passes on a freshly synced tree (no drift).
5. Org allowlists in all 8 orgs permit `Sentin8/sentin8-ci/**`.

## 10. Open Business Questions

1. Should trivial pin-bump PRs auto-merge after green CI (currently a
   human merges)?
2. Is a public status page (nightly badge) wanted for each group?
3. Priority of the ARM64 nightly leg versus other backlog work.