# Architecture Source Freeze and Supersession Map

Use this procedure before turning a long-lived architecture issue with many comments into a concept paper or implementation plan.

## Why this exists

Long architecture threads accumulate several kinds of text:

- observed inventory;
- accepted decisions;
- later refinements;
- explicit reversals;
- implementation proposals;
- unresolved runtime gates.

Chronological reading alone is unsafe. A concept paper can accidentally revive an early rejected design or promote observed current state into desired policy.

## Closure procedure

### 1. Inventory every owning profile

For each relevant profile, obtain a read-only native inventory of its complete scheduler registry and attached artifacts. Ask for:

- total job count, explicitly including zero;
- job name/ID, enabled state, schedule, repeat, script, `no_agent`, skills, delivery class, and toolsets;
- deterministic versus model-driven stages;
- cursor/idempotency/retry/ack/no-change behavior;
- attached and safely discoverable orphan scripts;
- keep, replace, merge, or remove disposition;
- dynamic human/profile preferences versus managed invariants.

If another profile's mutable tree is inaccessible, dispatch the inventory to that profile through native Kanban. Do not infer absence from repository declarations or connectivity. Require privacy-minimized metadata and prohibit job execution or mutation.

A zero-job result is useful evidence. Record it durably; do not treat silence as proof.

### 2. Reconcile evidence into architecture

Do not paste private prompts or raw personal content into the issue. Convert each completed inventory into a concise architecture reconciliation:

- exact observed mechanics and defects;
- reusable managed framework;
- dynamic profile/human policy;
- authority and privacy boundary;
- zero-token/no-op behavior;
- migration and live acceptance gates;
- explicit statement that current jobs remain until replacement proof.

Exact routine enablement, wording, schedule, and personal thresholds normally remain runtime policy and need not block a concept paper.

### 3. Build the supersession map

Fetch the live issue body and every decision comment. For each source classify it as:

- **baseline evidence** — describes observed state;
- **current accepted contract** — remains authoritative;
- **partially superseded** — name the exact clause replaced and preserve unaffected clauses;
- **fully superseded** — must not be used for implementation;
- **open implementation/verification gate** — architecture is settled but proof or exact schema remains pending.

Use these precedence rules:

1. Explicit correction/supersession wins over conflicting earlier text.
2. A later narrow clarification refines rather than silently replaces compatible decisions.
3. Evidence does not become desired policy merely because it is current.
4. The concept paper is derivative until reviewed; it must not silently create policy.

Avoid blunt statements such as “comment X is obsolete” when only one clause was reversed. Partial supersession must name both the rejected and retained portions.

### 4. Freeze concept-paper inputs

The concept paper should consume:

```text
accepted comments and explicit supersessions
  -> consolidated capability and authority model
  -> observed inventory and current-state gaps
  -> implementation decomposition
  -> migration and acceptance gates
```

Label material statements as one of:

- observed current state;
- accepted target architecture;
- proposed implementation detail;
- unresolved verification gate.

Do not require implementation details such as final CLI spelling, exact schema keys, deployed-version proof, or adapter choice to be decided before writing. Preserve them as explicit gates.

### 5. Publish and verify

When adding reconciliations or the map to a durable issue:

- construct the complete body locally;
- publish through the supported issue API;
- fetch the live comment back;
- compare the exact body;
- record the resulting stable comment ID/URL;
- state explicitly that documentation does not authorize implementation, deployment, secret work, job resumption, or retirement.

## Completion test

Concept-paper input is frozen only when:

- every relevant profile registry was observed or explicitly marked inaccessible;
- every scheduled/reusable artifact has a primary disposition;
- zero-job profiles are recorded as evidence;
- all explicit reversals are represented in the supersession map;
- current jobs remain protected until verified cutover;
- remaining unknowns are implementation/verification gates rather than missing architecture owners.
