---
name: nix-maintenance-protocol
description: Use when handling managed maintenance intents or commit audits. Preserve deterministic intake and exact completion proofs.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [maintenance, durable-state, containers, documentation, cron]
    related_skills: [evidence-driven-change-control]
---

# Managed maintenance protocol

## Overview

Use this skill for the packaged maintenance pipeline, its downstream planning and
commit-audit tasks, or recovery of their durable state. It describes the managed
protocol, not a schedule, recipient, repository inventory, or deployment grant.
For this protocol, these execution rules supersede legacy instructions to launch
an agent to poll sources or mechanically publish an issue.

## Repository context

Load `repository-knowledge` before repository work. Reuse or create your own
profile's knowledge through native skill tools under that contract. The explicit
instance configuration selects the repository, default branch, roles and registry
entry. The registry ref and repository must agree; no skill can override them.
Carry repository/revision, relevant procedures and accessible sources in handoffs.
The container adapter supports the Nix `nixosConfigurations` and
`custom.containers` model; unsupported structures are errors, not empty inventory.

## When to use

- A native task contains a `docs_audit` scope or a container-update intent.
- A maintenance source failed, an acknowledgement was lost, or a job is being migrated.
- A source or scheduler readback must be distinguished from semantic completion.

Do not use this skill to authorize merge, deployment, cleanup, secrets, or closure.

## Pre-model boundary

Keep source reads, immutable-cache validation, inventory evaluation, release
filtering, reviewed-policy application, deduplication, publication/readback/ACK,
and native routing deterministic. They run without inference, including when
there is actionable mechanical work. A `wakeAgent` JSON field alone does not
prove that a launcher avoids a model: inspect its generated `no_agent` contract
and execute the pinned native scheduler boundary.

The container pipeline is:

1. `container-refresh` validates the declared immutable reference cache, evaluates
   effective host/service/component image tuples at one Git commit, and queries
   supported curated release sources. Completion means an atomic inventory/report
   generation with `health.json.status=ok`, not an existing file or a successful
   validation of yesterday's report.
2. `container-curation-apply` reads that successful generation and stages a bound
   issue intent. Preserve mixed-case component names, host URL/tag overrides, tag
   flavours, manual/compatibility exclusions, source evidence and risk notes.
   Missing, unsupported or unreviewed feed rules are errors requiring curation;
   never substitute an empty success or guess a replacement tag family.
3. The model-free native publication job calls `container-request-delivery`.
   This grants publication of the selected digest in the existing intent ledger;
   preparing an intent alone is not that grant. Omitting the publication job does
   not implicitly publish every curation result.
4. The existing Forgejo workflow service consumes requested intents with
   `container-deliver --require-request`. It retains its own host credential
   boundary. Native cron must not assume terminal-container credential mounts
   exist on the host, copy secrets into job environment variables, or invoke an
   agent to bridge the two contexts.
5. Publication creates one monthly issue, or appends one exact report to that
   issue. It preserves human text. Exact authenticated readback precedes ACK.
   The ordinary source journal and native issue router create semantic planning
   work; the producer does not create implementation cards or PRs itself.

Repeated or acknowledged work remains silent before model and delivery. Source
failure is a non-success state, not evidence of no updates. Keep a previously
valid pending scope intact until its delivery outcome is resolved.

## Semantic container planning

Start only from the source-bound issue and its exact candidate facts. Verify
current enablement and pins before implementation; the report describes its
recorded source commit, not an assertion about the live host.

Inventory always evaluates the deployment repository. Each candidate's
`change_route` identifies the repository, exact revision and source path owning
its effective image definition. Use that repository's registered instance and
writable worktree for the proposed change and PR. Catalogue defaults belong to
the container project; deployment overrides belong to the deployment repository.
Group implementation work by owning repository and retain the deployment issue
as the cross-repository planning record. An issue number alone is never identity.
Do not edit immutable reference caches or infer ownership from the issue's home.

For pending intents created before the split, preserve their digest, receipts,
publication state and native task identity. Resolve the current definition owner
through a fresh deployment inventory before implementing a proposal without
`change_route`; do not rewrite the old intent or reset its ledger. New curation
requires source-bound routes. If the old proposal cannot be matched unambiguously,
keep it pending for clarification rather than choosing a repository by name.

Research meaningful upgrade differences per logical service, keeping coupled
components together. Treat policy notes as constraints, not proof that an upgrade
is safe. Preserve application/database/cache/search compatibility, lockstep rules,
image flavour and manual-only exclusions. Review all crossed release notes and
migration guidance, not just the latest release headline.

Use native concurrency controls for independent research; dependencies represent
real ordering. Consolidate the evidence before proposing implementation groups.
Separate low-risk independent updates from stateful, major, migration-heavy,
backup-sensitive or restore-dependent work. Resolve uncertainty explicitly. No
mechanical intake helper grants merge, restart, migration, or deployment authority.

## Commit-audit completion

1. Read the task's fixed `docs_audit.identity`, `docs_audit.base` and
   `docs_audit.target`. Match identity (instance, repository, origin, branch,
   namespace, board and profile) to the configured source and your assignment.
   Validate the repository and full ancestor history. An initial null base means full-tree
   coverage; otherwise inspect the Git log and diff across the entire range.
   A changed cadence or missed invocation does not change this scope.
2. Compare code, generated configuration and documentation semantically. A link,
   heading or placeholder inventory is supporting evidence, not this audit.
3. Create durable finding follow-ups through native task interfaces before
   declaring coverage successful. Keep unrelated work and production actions
   outside the audit's authority.
4. Complete the native task with metadata of this exact form:

   `docs_audit: {identity: <exact opening identity object>, key: <opening key>, task_id: <this native task>, base: <fixed base or null>, target: <fixed SHA>, result: "success", findings: [{task_id: <durable task>}, ...]}`

   Only assert success after covering the scope. An empty findings list means a
   completed audit found no required follow-up; it does not mean the audit was
   skipped. Failed or partial work must not carry a success proof.
5. The deterministic gate verifies native task identity, assigned and executing
   profile, terminal task state, exact source/scope and every finding reference before advancing `successful.json`. Never edit that marker
   manually, acknowledge at task creation, or mark a newer moving target audited.

Legacy audit cards without verified source identity cannot be resumed or ACKed
by matching a base commit, including a null first-run base. The gate blocks them
for explicit, verified adoption; do not rewrite their identity or create duplicate
audits to bypass that blocker. No automatic production migration is authorized.

## Recovery and ownership

Inspect the existing selected generation, native task, source event and mapping
before retrying. Do not write native Kanban or cron databases directly. Preserve
source revisions, in-flight work and acknowledgement tombstones.

An issue attempt is recorded immediately before its possible write. Ambiguous or
in-flight attempts recover through authenticated readback only, never a blind
second POST. No matching readback means unresolved work, not permission to reset
state. A failure before any write remains retryable. Concurrent producers cannot
replace an unacknowledged intent; investigate or finish that scope first.

Use supported native cron interfaces for managed adoption and removal. A name
match or historical job ID is not ownership. Remove only positively owned
undeclared launchers; retain independent jobs, counters, history, private policy
and workflow state. An explicit empty policy disables selected jobs, but is not
authority to erase pending work or restore retired pollers.

## Verification checklist

- Generated launchers and the service consumer agree with the package interfaces.
- A missing/failed source cannot masquerade as an empty successful report.
- Installed commands run without inherited development `PYTHONPATH`.
- Pinned native scheduler tests observe zero provider/agent construction before
  semantic work, not merely zero worker-spawn calls at an earlier gate.
- Crash, replay, concurrent staging/publication and exact readback are exercised
  with isolated state and controlled endpoints.
- Repository tests, deployed activation, live scheduler/token measurements and
  delivery are reported as separate evidence. Never infer the last three from
  an isolated fixture.
