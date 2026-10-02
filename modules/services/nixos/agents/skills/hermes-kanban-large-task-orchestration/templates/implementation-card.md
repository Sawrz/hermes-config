# Implementation task

## Goal

[State one independently understandable implementation result.]

## Context and accepted decisions

- Authority references: [issue/specification/accepted decision URLs or immutable IDs]
- Relevant decisions: [only decisions needed for this task]
- Exclusions: [what this task must not do]

## Inputs

- Parent artifacts: [commits, files, schemas, or `none`]
- Frozen interfaces: [contracts this task consumes but does not change]

## Ownership and collisions

- Owned files/subsystem: [exact boundary]
- Overlap class: [isolated | shared interface | serialized]
- Serialization rule: [required ordering or `none`]

## Workspace

- Repository/base revision: [repository and exact base]
- Task branch/worktree: [native Kanban workspace expectation]
- PR policy: commit and hand off; do not open a separate PR unless this card explicitly owns the final integration.

## Changes and handoffs

Continue on the existing branch and PR, including after a verified replacement: one integration owner means one active writer, not an immutable card ID. Check source/replacement cards, artifacts, accepted scope and dependencies before editing; preserve human gates and scratch artifacts. A preparation-only handoff stays preparation-only. Respect explicit human restrictions, resolve conflicting old instructions with the contract owner, and record authorized corrections on the affected cards. Do not create competing work, bypass blockers or treat routing as implementation completion.

## Prerequisites and human waits

- Accepted planning decisions: [authority references; unresolved prerequisite work belongs in native parents, not an unconditional implementation instruction].
- Before release or another human wait, inspect prior runs, blocker comments and recurrence history. An unblock does not reset it.
- After `kanban_block(kind="needs_input")`, read back the actual status. A second different question can return `triage`, which auto-decomposition may release without an answer. Report this immediately as incomplete containment and stop implementation; do not retry, change kinds, reset counters or create replacement cards to evade recurrence. The `forgejo-pr-lifecycle` contract applies to PR human actions.

## Acceptance criteria

- [Observable result]
- [Required compatibility or safety condition]
- [Required artifact]

## Verification

- Highest-risk boundary: [boundary]
- Commands: [exact commands]
- Required result: [unambiguous pass condition]

Failed, skipped, unavailable, timed-out, killed, ambiguous, or stale checks are incomplete.

## Safety and authority

- Secrets: [allowed handling or none]
- Destructive operations: [explicitly authorized actions or none]
- Live systems/deployment: [explicitly authorized actions or none]
- Human gates: [merge, deployment, rollback, closure, or other gates]

## Completion handoff

Provide a concise summary plus structured metadata containing changed files, exact commits, verification evidence, decisions, and residual risks. Do not include secrets or duplicate native Kanban status, assignee, parent, workspace, or attempt state.
