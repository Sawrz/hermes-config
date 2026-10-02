# Integration task

## Goal

[State the complete outcome this task must integrate and verify.]

## Context and accepted decisions

- Authority references: [issue/specification/accepted decision URLs or immutable IDs]
- Required parent outcomes: [list all implementation parents]
- Exclusions: [what this task must not do]

## Inputs

For each parent, require:

- exact commit or artifact identity;
- changed files;
- verification evidence;
- decisions and residual risks.

Treat handoffs as claims until the artifacts are independently inspected.

## Integration ownership

- Integration repository and branch: [exact target]
- Collision hotspots: [shared files/interfaces]
- Conflict rule: resolve against accepted contracts, not last-writer wins.
- PR ownership: this task owns the single coherent PR when publication is in scope.

## Changes and handoffs

Continue on the existing branch and PR, including after a verified replacement: one integration owner means one active writer, not an immutable card ID. Check source/replacement cards, artifacts, accepted scope and dependencies before editing; preserve human gates and scratch artifacts. A preparation-only handoff stays preparation-only. Respect explicit human restrictions, resolve conflicting old instructions with the contract owner, and record authorized corrections on the affected cards. Do not create competing work, bypass blockers or treat routing as implementation completion.

## Prerequisites and human waits

- Accepted planning decisions: [authority references; unresolved prerequisite work belongs in native parents, not an unconditional implementation instruction].
- Before release or another human wait, inspect prior runs, blocker comments and recurrence history. An unblock does not reset it.
- After `kanban_block(kind="needs_input")`, read back the actual status. A second different question can return `triage`, which auto-decomposition may release without an answer. Report this immediately as incomplete containment and stop implementation; do not retry, change kinds, reset counters or create replacement cards to evade recurrence. The `forgejo-pr-lifecycle` contract applies to PR human actions.

## Acceptance criteria

- Every required parent artifact is present and attributable.
- Shared interfaces and central files are internally consistent.
- The integrated result satisfies the original outcome.
- Required focused and full checks pass on the exact integrated revision.
- Review is bound to that exact revision.

## Verification

- Highest-risk boundary: [boundary]
- Focused commands: [exact commands]
- Full commands: [exact commands]
- Required result: [unambiguous pass condition]

Failed, skipped, unavailable, timed-out, killed, ambiguous, or stale checks are incomplete.

## Safety and authority

- Secrets: [allowed handling or none]
- Destructive operations: [explicitly authorized actions or none]
- Merge/deployment: [explicitly authorized actions or human gate]
- Rollback/live verification: [owner and required evidence]

## Completion handoff

Report the exact integrated revision, all changed files, verification evidence, review state, residual risks, and remaining human-controlled actions. Do not claim deployment or live verification unless those boundaries were actually exercised.
