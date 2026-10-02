---
name: forgejo-pr-lifecycle
description: Native repository implementation, independent review, handoff and human-action lifecycle. Use for PR work and its continuations.
version: 1.1.0
metadata:
  hermes:
    tags: [forgejo, kanban, repository, human-action]
---

# Native PR lifecycle

Forgejo is authoritative for code, PR outcome, reviews and exact-head CI.
Native Kanban owns execution and durable handoffs. The selected
`forgejo-pr-lifecycle` integration reads those facts through the existing
Kanban-to-Vikunja writer. Workers do not write Vikunja or its projection ledger.
This protocol grants no merge, deployment, secret or issue-closure authorization.

## Configuration and repository knowledge

Load `repository-knowledge` before repository work and follow its discovery,
ownership and maintenance contract. Resolve repository, main branch, namespace,
board, implementing/reviewing profiles and human projection from the explicit
instance configuration supplied by the owning integration. Service endpoints and
credential references come from the assigned profile's service bindings. Missing
or contradictory values block work; a private skill cannot supply machine policy.
The uppercase placeholders below mean those configured values, never defaults.
Include repository identity, revision, relevant procedures and accessible sources
in each handoff; recipients must not depend on the sender's private skill.

## One stable projection anchor

Before creating anything, read the assigned native card and all supplied lineage
references with `kanban_show`. Reuse the known PR anchor, Questions task and
execution cards. The existing F11 intake routes meaningful updates to active
execution. Do not create a competing implementation or PR.

The IMPLEMENTER_PROFILE creates one permanent, blocked native anchor per PR through
`kanban_create`, with `assignee="IMPLEMENTER_PROFILE"`, `initial_status="blocked"`, no
parents, and `idempotency_key="NAMESPACE-pr-N-projection"`. Substitute the actual
PR number, board and exact Forgejo URL. Keep the original title, rationale,
changes, risks/rollback, source issue and lineage, required post-merge steps and
completion condition in its body. Add these exact contract lines:

```text
PR workflow: native-v1
Forgejo authority: FORGEJO_ORIGIN/OWNER/REPOSITORY/pulls/N
Human action: Merge
Vikunja project: HUMAN_PROJECT_ID
Vikunja assignee: none
Vikunja done: false
Vikunja automation reference: NAMESPACE-pr-N-merge
```

Only this anchor carries the Human action/Vikunja mapping contract. Its blocked
status is a permanent projection anchor, not a worker waiting for dispatch. Do
not complete it to signal PR completion and do not make workers depend on it.
The projection calculates assignee/done from fresh evidence, ignoring the
anchor's initial values. Use a checklist, not invented Vikunja subtasks.
Questions remain one existing Questions action per planning gate; carry its URL
in the PR anchor. Respect the existing planning approval before implementation.

## Execution and review handoff

Every execution card uses these exact lines in addition to its full assignment:

```text
PR projection anchor: BOARD/ANCHOR_ID
PR workflow phase: implementation
PR head: FULL_40_CHARACTER_SHA
Forgejo authority: EXACT_PR_URL
```

Valid phases are `implementation`, `review`, `handoff`, `outcome`, `rebuild`,
`verify`, `manual-verify`. Only review belongs to `REVIEWER_PROFILE`; the other
phases belong to `IMPLEMENTER_PROFILE`. Use `skills=["forgejo-pr-lifecycle"]` and the
applicable existing review/implementation skills. Child cards use the supported
creation default `initial_status="running"` and their actual predecessor in
`parents`. In the pinned native API this resolves to `todo` while a parent is
unfinished, and `ready` when all parents are done; it does not launch a worker
immediately. `todo` is not an accepted creation argument. Do not reassign the
current card or add an unfinished root whose completion itself waits for review.

1. The implementing profile implements/tests/publishes the exact head and creates a separate
   review card assigned to REVIEWER_PROFILE, dependent only on the completed
   implementation phase. It then completes its current implementation card.
2. The reviewing profile performs the formal Forgejo review using the existing review
   skill, submits a pending review, and reads back ID, author, state, head,
   staleness and every inline finding. It comments the result on its own card.
3. Before completing, REVIEWER_PROFILE creates a child handoff card assigned to
   IMPLEMENTER_PROFILE with this review card as parent. It includes the formal review
   ID, findings, exact head and source links. Both changes-requested and approved
   results return to IMPLEMENTER_PROFILE; neither goes directly to the human merge gate.
4. The implementing profile handles findings within the granted scope. A code change starts
   another implementation→review→handoff cycle bound to the new head. Without
   remaining findings it records the exact-head handoff; the projection checks
   current required CI and formal reviews before offering the human merge action.

For each child use a stable idempotency key containing PR, head, phase and
predecessor task ID. Preserve its arguments on retry. Read back the child and
check assignee, parents, body and skills before including its ID in the native
`kanban_complete(created_cards=[...])` handoff. Never claim an uncreated child.

Complete a phase through the native tool with a human-readable summary, durable
artifacts and `metadata.pr_workflow`:

```json
{"anchor":"BOARD/ANCHOR_ID","phase":"implementation","head":"FULL_SHA",
 "result":"success","evidence":["durable artifact or source URL"]}
```

Use the actual phase and commit. Review and handoff additionally record the
integer `review_id`. Handoff also records `actionable_digest` from the packaged
read-only command `forgejo-kanban-workflow --instance-config INSTANCE_CONFIG --endpoint ENDPOINT --credential-file
PROFILE_CREDENTIAL inspect-pr --number N --reviewer REVIEWER_LOGIN`. Use the
existing profile-scoped credential reference, never print its contents. Read the
returned evidence before completing the handoff. This digest uses the existing
intake classification: changed meaningful comments/review bodies invalidate the
handoff, while timestamps, CI status events and authenticated receipt echoes do
not create another semantic pass. The projection independently rechecks live CI
and review readiness. Evidence must describe what was observed, never fixture
results as live evidence. `success` means that phase's assignment was completed:
for review, a submitted REQUEST_CHANGES is a completed review, not merge readiness.
Native run profile/status and parent/run ordering are independently checked.
Do not copy an old receipt to a new card or omit a failed/restarted run.
An outstanding human REQUEST_CHANGES cannot be overruled by agent approval.

## Explicit human intermediate actions

Task bodies are immutable through the worker toolset. The head in a creation
body is an initial context; the completed run receipt must contain the actual
published head. Do not invent a task-update tool or use SQLite/CLI as a worker.

Before implementation dispatch, settle known scope, interface and permission
questions at the existing planning gate. Bundle related decisions. When a real
prerequisite has its own bounded deliverable, express it as a native parent;
complete it only after the decision and deliverable exist. One PR does not require
one execution card. Do not manufacture replacement cards to reset block history.

Before another human wait or an authorized unblock, inspect `kanban_show`, prior
runs/comments and `block_recurrences` when exposed. The pinned native lifecycle
counts repeated `needs_input` by kind: a different question can still recur.
Unblocking does not reset that history. Do not assume a new gate revision or a
new run buys another safe wait on the same card.

When IMPLEMENTER_PROFILE needs a specific human action, publish a native `kanban_comment`
on its execution card using the following exact structured request. Explain the
concrete need, call `kanban_block(kind="needs_input")`, then read back the card: a
successful tool response does not imply the resulting status is `blocked`. If
history already shows a prior human wait, explicitly flag the recurrence risk to
the owner; this protocol cannot guarantee another durable hold on that card.

```text
PR workflow update: native-v1
PR head: FULL_CURRENT_SHA
Human decision: secret
Human gate: UNIQUE_REQUEST_REVISION
```

Allowed decisions: `secret`, `manual`, `rebuild`, `verify`, `rollback`, `continue`.
Never put secret values in the body. A new request uses a new gate revision and
the current full head. After merge also add `Merge commit: FULL_MERGE_SHA`.
The latest such comment from the assigned native profile replaces the request;
other authors cannot update it. Its native creation timestamp prevents an older
Vikunja comment from releasing a newly created request. Only one human gate may
be active per anchor. The same PR task is assigned for
this explicit Human-Wait even when merge readiness is false.

If the response/readback is `triage`, this is not a durable human hold:
auto-decomposition may rewrite and release the same card without an answer or
children. Record the actual status and unresolved question, report immediately
that containment is incomplete, and stop implementation. Do not keep retrying,
reset counters, change blocker kinds to evade recurrence, complete unfinished
work, or invent replacement cards. Prompt guidance/readback alone cannot stop the
dispatcher race; any operational containment needs separate authorization.

The reverse projector records human input on that continuation. Within that
projector, only a matching gate/head and an owner-authored response may wake it.
This restriction does not govern the native auto-decomposer. Waking permits the worker
to assess the response; it does not approve a merge, rebuild or rollback. Confirm
its meaning, source and scope before acting. A stale gate response is evidence,
not permission for a replacement request. Never treat a checked Vikunja task as
PR/issue completion. The permanent anchor is not unblocked by comments.

## Required CI policy

Readiness uses the effective PR base-branch response from Forgejo, including
`enable_status_check` and `status_check_contexts`. An enabled, nonempty set of
required contexts is a deployment prerequisite for this integration. The
checked-in CI path-scope policy is not a replacement for that required-check
contract. Neither a protected-branch flag nor a historical count of green jobs
establishes required checks. Missing/disabled policy remains
`required-ci-policy-missing`; never remove the gate to make the task assignable.

The coordinator must obtain an explicitly authorized required-check policy and
read it back using the projection owner's service credential. `inspect-pr`
returns the observed branch policy and the readiness reasons. A fixture with
that policy is not a positive live acceptance case. Configuring branch protection
is a separate authorized operation, not an action granted to this worker.

For required contexts, `success` with `Has been skipped` is not execution proof.
For Forgejo Actions, the adapter supports the legacy `Has succeeded` and the
observed duration-bearing `Successful in <duration>` serialization (for example
`3s`, `52s`, `1m14s`). Both require status `success`; skipped, cancelled, pending,
unknown or missing execution evidence remains blocked. Do not infer success from
arbitrary prose or broaden the adapter without verified API fixtures. The latest attempt per
context is authoritative, and every context matched by a required glob must
succeed. Skipped contexts outside the required policy remain optional.

## Post-merge and closure

F11 intake routes a closed/merged PR snapshot only when a live `native-v1` anchor
exists. It creates one IMPLEMENTER_PROFILE `outcome` execution, with a stable identity
bound to that anchor, PR head and outcome/merge commit. Retries, timestamp changes
and lost responses recover that same execution, including after archival; they
do not append repetitive live comments. The anchor never becomes a worker or a
dependency. Other active PR executions remain native parents, so their human
gates are not bypassed. No untracked PR receives new work from closure alone.

Before acting on the outcome execution, reread the PR. If its head/outcome no
longer matches the assignment, reconcile the changed source within existing
authority; do not publish the stale outcome receipt or deploy from it. A source
read failure is incomplete evidence. Intake ACK follows native readback and a
fresh matching source observation, not the initial poll alone.

After observing the authoritative PR outcome, IMPLEMENTER_PROFILE completes an `outcome`
phase with the source URL and recorded outcome. Its receipt includes
`outcome="merged"` or `"closed"`, boolean `rebuild_required` and
`manual_verification_required`, and, for a merge, `merge_commit=FULL_MERGE_SHA`.
Derive these requirements from the reviewed scope, never default an unknown
requirement to false. Closed without merge can finish only with a recorded
outcome and no remaining required work.

For merged work, required rebuild follows outcome; verification follows the
successful rebuild, or outcome if no rebuild is required. Optional human
verification follows successful technical verification. All receipts use the
PR head plus the same `merge_commit`; manual-verify also supplies nonempty
`human_evidence` identifying the actual owner's response/artifact. Record fresh
runtime observations. A later failed run invalidates its earlier receipt.
On failure keep the task open and request the authorized rollback/continue
choice through a Human-Wait gate. A choice alone does not replace verification.
Complete required repair/rebuild/verification in dependency order.

The projection closes the PR action only after the outcome and required ordered
proofs exist and no execution remains active. Issue closure is separate:
issue ownership and the repository's confirmed closure rules determine whose
confirmation is needed. Never infer issue-closure authority from PR merge.

## Explicit legacy adoption

A known legacy task can be adopted only through an authorized native anchor
containing the exact `Vikunja legacy task: ID`, `Vikunja legacy creator: USERNAME`
and `Vikunja legacy root: TASK_ID`. These are supplied identity facts, not IDs to
guess from a title. Inspect the task's project, creator, exact PR link, automation
marker and native root before requesting adoption. The reconciler persists
intent, preserves its narrative as historical evidence, adds one reciprocal
link, reads back the result and then records the mapping. Ambiguity or drift
stops adoption. Historical checkboxes/done flags are not readiness evidence.
