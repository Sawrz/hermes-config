---
name: hermes-kanban-large-task-orchestration
description: Use when one engineering outcome needs several coordinated Hermes Kanban workers and one integration owner.
version: 2.2.0
metadata:
  hermes:
    tags: [hermes, kanban, orchestration, worktrees, integration]
    related_skills: [evidence-driven-change-control]
---

# Hermes Kanban Large-Task Orchestration

## Purpose

Use native Hermes Kanban to split one large outcome into bounded tasks, express real dependencies, isolate concurrent repository work, and converge through one integration owner.

This skill does not create another scheduler, task database, status ledger, campaign manifest, or worker runtime. Native Kanban owns cards, dependencies, assignees, status, attempts, workspaces, comments, and completion handoffs.

## When to use

Use this skill when:

- several independently understandable workstreams are required;
- different specialist profiles are needed;
- safe parallelism materially helps;
- shared files or interfaces require explicit serialization;
- integration has a separate verification boundary.

Do not use it when one worker can implement and verify the outcome coherently. Do not split required parts merely by file type or implementation layer.

## Core rules

1. Start from one complete outcome and acceptance contract.
2. Discover valid assignee profiles before creating cards; never invent an assignee.
3. Express dependencies with native parent links, not prose or a shadow graph.
4. Give concurrent repository workers separate task-owned branches and worktrees.
5. Freeze or serialize shared interfaces and collision-prone files before fan-out.
6. Keep code, its tests, and its meaningful verification in one task when they form one atomic correction.
7. For one repository outcome, implementation workers hand off commits to one integration owner; they do not open competing PRs.
8. Review evidence applies to an exact integrated revision. A changed head requires fresh affected verification and review.
9. Secrets, destructive operations, merge, deployment, rollback, and closure retain their normal authorization boundaries.
10. Failed, skipped, unavailable, timed-out, killed, ambiguous, or stale verification is incomplete.

## Changes and handoffs

Continue an existing assignment rather than creating duplicate work. A verified native replacement may take over the existing branch and PR: one integration owner means one active writer, not an immutable card ID. Read the source and replacement cards, verify artifacts, accepted scope and dependencies, and preserve human gates. Wait if another writer is active; a preparation-only handoff remains preparation-only. Protect scratch artifacts before any completion or retirement that could remove them.

Respect an explicit human ID restriction. Do not invent one from “one owner/PR”; when inherited wording conflicts, trace it to the accepted request. Record an authorized correction as a superseding comment on affected cards, retaining the history. This does not change native assignment or dependencies, resolve an external blocker, or authorize production changes. A skill update does not rewrite existing cards; do not repeatedly unblock unchanged blockers or mark unfinished work complete.

Carry the short continuation clause from the existing templates into new cards so receiving workers have it without hidden conversation context.

## Procedure

### 1. Define the outcome

Record:

- requested result and exclusions;
- authoritative issue, specification, and accepted decisions;
- affected systems and highest-risk boundary;
- required tests and live verification;
- human-controlled actions;
- integration and rollback expectations.

Completion criterion: every required part of the outcome has an owner and a verification method.

### 2. Choose task boundaries

Split by independent responsibility and evidence boundary. A task is suitable when one worker can understand its inputs, produce a coherent artifact, and verify it without relying on hidden conversation context.

Classify overlap before parallelizing:

- **isolated** — no expected shared-file or shared-state collision;
- **shared interface** — consumes a frozen contract owned elsewhere;
- **serialized** — owns a shared contract or central file;
- **integration** — combines completed parent artifacts.

Settle known scope, interface and permission questions at the planning gate
before releasing implementation. Bundle related decisions; do not mix unresolved
contract design with an unconditional implementation/PR assignment. A real
prerequisite may have its own decision/artifact card; consumers use native
`parents` and stay gated until that work is genuinely complete. One PR does not
mean one card. Do not create artificial phase or replacement cards merely to
reset a block counter.

Completion criterion: concurrent tasks are independent or consume a frozen interface; collision-prone work is serialized.

### 3. Build the native DAG

Create implementation cards with `kanban_create`. Use `parents` only for genuine sequencing or data dependencies. Create one integration card whose parents include every required implementation card.

Typical shape:

```text
contract decision
  -> independent implementation cards
  -> integration and full checks
  -> exact-head review
  -> authorized merge/deployment
  -> live verification
```

Do not duplicate the graph in JSON, YAML, Markdown metadata, or repository state.

Completion criterion: Kanban itself contains every dependency required for correctness and has one fan-in owner.

### 4. Write self-contained cards

Use the optional templates in `templates/`. Every card must state:

- exact goal and exclusions;
- immutable authority references and relevant accepted decisions;
- parent inputs and expected outputs;
- owned files or subsystem;
- known collision points;
- workspace expectations;
- acceptance criteria;
- exact verification commands and highest-risk boundary;
- secret, destructive, deployment, and authorization limits;
- required completion summary and metadata.

Card bodies explain the work. Native Kanban fields remain authoritative for assignee, parents, status, workspace, attempts, and skills.

Completion criterion: a fresh worker can complete the task without access to this chat or unstated sibling context.

### 5. Isolate repository work

For concurrent code changes:

- keep the base checkout clean;
- use one task branch and worktree per implementation card;
- do not share a mutable checkout;
- commit coherent tested outputs on the task branch;
- hand off exact commit IDs and changed paths;
- use normal follow-up commits unless history rewriting is explicitly authorized.

Completion criterion: every implementation result is reproducible from a named revision and can be integrated without borrowing an uncommitted worktree.

### 6. Hand off evidence

Complete cards with a concise summary and structured metadata containing only evidence not already owned by Kanban, such as:

```json
{
  "changed_files": [],
  "commits": [],
  "verification": [],
  "decisions": [],
  "residual_risks": []
}
```

Never include secrets or duplicate mutable Kanban state. Treat a child summary as a claim until the integration owner verifies the referenced artifacts.

Completion criterion: the next worker can locate the artifacts, reproduce checks, and understand unresolved risks.

### 7. Integrate once

The integration owner:

1. reads every parent handoff;
2. verifies referenced commits and scope;
3. accounts for collisions and shared-interface changes;
4. integrates the required outputs;
5. runs focused and full risk-matched checks;
6. opens or updates the single coherent PR when a PR is part of the outcome;
7. records the exact integrated head for review.

Green child cards do not prove integration. A build does not prove deployment, and deployment does not prove live behavior.

Completion criterion: one exact integrated revision contains the complete outcome and has passed the required pre-application boundary.

## Human waits and recurrence

Read prior runs, blocker comments and recurrence history before releasing or
blocking work again. The pinned native lifecycle can send the second
`needs_input` on one card directly to `triage`, even for a different question;
an authorized unblock does not erase that history. Read back every block result.
With auto-decomposition enabled, `triage` can release the same card without a
human answer or child cards. It is not proof of a durable human wait.

If this occurs, report the actual status and unresolved decision immediately and
stop implementation; do not claim containment, retry unchanged blockers, reset
history, switch kinds to evade it, or complete unfinished work. Use the existing
`forgejo-pr-lifecycle` human-action contract when applicable. These rules improve
planning and reporting, not the scheduler: they do not guarantee that unexpected
repeated human waits are safe or authorize changing native runtime settings.

## Common pitfalls

- Splitting code, tests, and verification into artificial separate cards.
- Parallelizing central-file edits and planning to resolve everything later.
- Letting each implementation worker open a PR.
- Treating a child handoff as verified external side-effect evidence.
- Copying native Kanban state into a shadow manifest.
- Calling the root complete because every child is done.

## Verification checklist

- [ ] One complete outcome and acceptance contract are explicit.
- [ ] Every assignee is a real profile.
- [ ] Native parent links contain all real dependencies.
- [ ] Concurrent repository workers use separate worktrees.
- [ ] Shared interfaces are frozen or serialized.
- [ ] Cards are self-contained and contain no secrets.
- [ ] One integration owner verifies child artifacts.
- [ ] Review and tests refer to the exact integrated revision.
- [ ] Human-controlled operations remain separately authorized.
- [ ] Root completion is based on integrated and live evidence where required.
