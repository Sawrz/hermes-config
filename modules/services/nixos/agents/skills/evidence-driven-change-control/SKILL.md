---
name: evidence-driven-change-control
description: Use when changing or repairing operational systems.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [change-control, evidence, verification, deployment, debugging]
    related_skills: [systematic-debugging, test-driven-development, requesting-code-review]
---

# Evidence-Driven Change Control

## Overview

Use this skill for any technical change where correctness depends on more than writing plausible code or configuration. It applies across software, infrastructure, automation, integrations, migrations, and incident repair.

The governing principle is simple:

> Advance only on evidence from the boundary that can actually fail.

Plans, diffs, commits, builds, reviews, tasks, and status labels are useful evidence of progress. None of them alone proves that the requested outcome works.

## When to Use

Use when:

- diagnosing unexpected or failed behavior;
- changing an existing system;
- delivering a new operational capability;
- deploying, migrating, upgrading, or repairing something;
- deciding whether work is ready, complete, or safe to hand off;
- reviewing a change whose failure could affect users, data, availability, security, or recovery.

For a purely explanatory or read-only question, use only the relevant evidence and status-language rules.

## Core Rules

1. **Inspect before changing.** Use authoritative sources and current state. Distinguish intended configuration from actual behavior.
2. **Define the outcome first.** State what must be true for the user's request to be complete.
3. **Diagnose before fixing.** Establish symptom, evidence, cause hypothesis, and a safe reproduction before implementation.
4. **Test the real risk.** Choose verification from the failure mode, not from convenience.
5. **Fail closed on missing evidence.** Failed, skipped, ambiguous, unavailable, timed-out, or resource-killed checks are incomplete, not successful.
6. **Deliver coherent outcomes.** Prefer one issue, ideally one coherent PR, and a completed result. Split only at real independent change boundaries.
7. **Use precise state.** Never call work ready, done, deployed, or verified beyond the evidence.
8. **Stabilize before iterating.** After a failed change, protect data and restore safe service before attempting improvement.
9. **Respect authority immediately.** A stop, scope limit, or ownership transfer overrides prior plans and in-flight work.

## Design Rules

### Start from the supported product contract

Before designing integration machinery:

- inspect current official deployment and administration documentation;
- inspect the shipped entrypoints, supported configuration interfaces, lifecycle commands, and reference topology where needed;
- identify which requested capabilities are natively supported, unsupported, or require an explicit extension;
- treat a material deviation from the supported path as a design decision requiring justification, risk analysis, and stronger tests.

### Module and configuration options must be honest

A module or configuration option is a behavioral contract. Its name, type, default, documentation, assertions, generated configuration, and tests must describe what the exact underlying system can actually deliver.

Classify each exposed capability as one of:

- **natively supported** — implemented through a current documented interface;
- **deliberately extended** — custom behavior with explicit ownership, upgrade responsibility, failure semantics, and exact-runtime tests;
- **unsupported** — omitted or rejected with a clear assertion.

Do not expose desired behavior as though support already exists. Do not silently translate an unsupported combination into custom policy, direct database manipulation, generated application code, or another hidden application fork.

When capabilities interact, validate the combination rather than each option in isolation. Prefer rejecting an unsupported or unverified combination over accepting configuration that cannot honor its promise.

Tests must prove the behavior promised by the option, including important disabled and invalid combinations. Testing only that generated text contains the option is insufficient.

### Prefer native interfaces over parallel machinery

Use supported configuration, APIs, data models, management commands, and entrypoints before generated application code, source mounts, direct database manipulation, or wrapper lifecycle logic.

Custom machinery is acceptable only when the native path is genuinely insufficient and the extension boundary is deliberate. Record why it is needed, who owns it, how it is upgraded, and how its real runtime path is tested.

Treat generated executable code as code, not as inert configuration. Exercise it through the exact consumer and lifecycle that load it.

### Give each lifecycle transition one owner

Every migration, initialization, schema change, publication, recovery action, or other state transition needs one authoritative owner. Do not run the same lifecycle responsibility from both the application entrypoint and an external bootstrap path.

Separate concerns that fail or recover independently. Readiness, migrations, optional feature setup, and service startup should not become one all-or-nothing control point unless the application contract requires it.

### Model dependency semantics exactly

Do not treat these as interchangeable:

- process creation order;
- port availability;
- application readiness;
- successful initialization;
- dependency health;
- user-visible readiness.

Use the weakest coupling that is correct. An optional integration failure should not unnecessarily disable a healthy core service.

### Keep capabilities and secrets local

Give each component only the database role, files, credentials, network access, and application capabilities required for its function. Shared convenience environments and broad roles increase both blast radius and ambiguity about ownership.

### Treat complexity as a diagnostic signal

When an integration requires generated application code, manual database operations, custom policy, recovery orchestration, and cross-layer coupling, stop and re-check the supported architecture before adding more machinery.

Complexity is not proof that the problem is inherently complex. It is often evidence that the design is working against the product boundary.

## Workflow

### 1. Define the complete contract

Before implementation, write a compact acceptance contract:

- requested outcome;
- authoritative source of truth;
- current state if one exists;
- affected boundaries and dependencies;
- safety, data, security, and recovery constraints;
- evidence required before handoff;
- evidence required after application or deployment.

Completion criterion: every required part of the user-visible or operational outcome has a corresponding verification method.

### 2. Choose the delivery boundary

Default to:

```text
one issue -> ideally one coherent PR -> apply/deploy -> verify -> done
```

A PR or equivalent delivery unit should contain all pieces required for one independently valuable and independently safe outcome, even when those pieces affect different implementation layers.

**Split when:**

- the work covers different topics or different services;
- each part is independently useful;
- each part can be reviewed, applied, verified, and reversed independently;
- failure of one part does not leave the other incomplete or misleading;
- separate ownership or release timing is materially safer.

**Keep together when:**

- the parts jointly satisfy one acceptance contract;
- one part is unusable, unsafe, or misleading without another;
- the proposed split follows file type or implementation layer rather than operational outcome;
- splitting would turn required work into later cleanup or activation;
- partial delivery would be described as progress but not solve the issue.

**Do not combine:** unrelated topics or services merely to satisfy a superficial one-PR preference.

Completion criterion: every delivery unit has its own coherent outcome and can stand on its own after application.

### 3. Inspect authoritative and live state

For an existing system, inspect before editing:

- authoritative configuration or source;
- effective or generated configuration;
- current service/process state;
- relevant logs and error output;
- harmless probes at important component boundaries;
- recent changes that may explain the difference.

Record:

```text
Symptom:
Observed state:
Expected state:
Evidence:
Cause hypothesis:
Safe reproduction:
```

If required access or evidence is unavailable, report the blocker. Do not replace live evidence with repository assumptions.

Completion criterion: the problem and the boundary where it occurs are supported by direct evidence.

### 4. Build a red-capable reproduction

Create the closest safe reproduction of the real failure:

- use the actual consumer, protocol, lifecycle, or runtime where practical;
- preserve relevant generated configuration and dependency behavior;
- isolate data-changing tests from production;
- assert the exact symptom, not a nearby implementation detail;
- run it once and confirm it fails for the expected reason.

If the failure cannot be reproduced, gather more evidence or clearly label the hypothesis unproven. Do not begin a speculative fix.

Completion criterion: one repeatable check is red for the observed problem and is capable of turning green only when that problem is resolved.

### 5. Implement the smallest durable correction

- Fix the established cause, not only the symptom.
- Change one hypothesis at a time.
- Preserve existing conventions and known-good behavior.
- Avoid unrelated cleanup and premature abstraction.
- Add or retain the regression test that proves the failure.
- Label temporary mitigation explicitly and define the durable follow-up.

Completion criterion: the red reproduction is green because the cause was corrected, not because the assertion or environment was weakened.

### 6. Verify according to risk

Select the required layers from the actual failure modes:

1. formatting and static analysis;
2. unit and contract tests;
3. generated-artifact validation;
4. integration or lifecycle initialization;
5. build or packaging verification;
6. migration, restart, and clean-start behavior;
7. end-to-end or user-facing behavior;
8. live health and recovery behavior after application.

Lower layers cannot substitute for a higher layer when the risk exists at the higher layer. A syntax check does not prove initialization. A build does not prove runtime behavior. A review does not prove deployment. A deployment does not prove the requested outcome.

For every mandatory check, record:

```text
Check:
Boundary tested:
Exact artifact/revision:
Result: pass | failed | blocked | not run
Evidence:
```

Only `pass` advances the gate.

Completion criterion: the highest-risk boundary has direct behavioral evidence, and every mandatory check has an explicit non-ambiguous result.

### 7. Review the assumptions, not just the diff

Independent review must challenge:

- the acceptance contract;
- the selected delivery boundary;
- the cause hypothesis;
- the highest-risk boundary;
- whether tests exercise behavior rather than implementation presence;
- whether evidence belongs to the exact artifact under review;
- rollback and data-safety assumptions.

Repeating the implementer's static checks is not independent verification.

Completion criterion: review covers the claims that would cause the most damage if wrong and applies to the exact current artifact.

### 8. Hand off with precise state

Use exact state terms:

- **planned** — approach defined, no implementation claim;
- **implemented** — changes exist;
- **tested** — named checks passed against a named artifact;
- **reviewed** — current artifact received the required review;
- **ready** — every mandatory pre-application gate passed;
- **applied/deployed** — the target accepted the change;
- **verified** — required behavior passed on the target;
- **complete** — the full acceptance contract passed.

Never use `ready`, `done`, `working`, or `verified` while a required action is private, pending, blocked, skipped, planned, or inferred.

Completion criterion: the handoff states exactly what is proven, what is not, and the next authorized action.

### 9. Apply safely and verify the outcome

Before a risky change:

- define rollback or recovery;
- protect data and preserve evidence;
- minimize blast radius;
- verify prerequisites immediately before action.

After application:

- verify the effective target state;
- verify restart or re-entry behavior where relevant;
- exercise the requested behavior;
- check dependent systems and monitoring;
- record only observed results.

Completion criterion: the requested outcome works at the target boundary and recovery remains available.

## Failure and Incident Rules

If an applied change fails:

1. Stop further rollout.
2. Protect data and preserve diagnostic evidence.
3. Stabilize or restore known-safe service when possible.
4. Inspect the actual failing system.
5. Reproduce the failure safely.
6. Re-evaluate the cause and acceptance contract.
7. Implement only after a red-capable reproduction exists.

Do not start with a plausible hotfix merely because the traceback or symptom suggests one.

After three failed corrective hypotheses, stop and question the architecture or delivery approach. Do not stack a fourth patch on sunk work without explicit reassessment.

## Hard Stop Conditions

Stop and report the exact blocker when:

- current state cannot be inspected and the change depends on it;
- the failure is not understood or safely reproducible;
- a mandatory check failed, was skipped, timed out, was killed, or cannot be reached;
- the evidence belongs to a different artifact or revision;
- required safety, backup, rollback, or authorization is missing;
- partial delivery would leave the requested outcome incomplete;
- the user stops, limits, or transfers the work.

Do not lower the completion standard to make progress appear complete.

## Verification Checklist

- [ ] Complete requested outcome and acceptance criteria are explicit.
- [ ] Current official interfaces, entrypoints, lifecycle ownership, and reference topology were inspected where relevant.
- [ ] The design uses native supported interfaces, or every deviation has an explicit justification and stronger runtime test.
- [ ] Exposed options describe capabilities the underlying system actually supports.
- [ ] Every state-changing lifecycle action has one authoritative owner.
- [ ] Ordering, readiness, initialization, health, and user-visible readiness are modeled distinctly.
- [ ] Optional integrations do not unnecessarily become failure gates for the healthy core service.
- [ ] Credentials, roles, files, network access, and capabilities are limited per component.
- [ ] Unusual cross-layer complexity triggered an upstream architecture reassessment.
- [ ] Delivery boundaries follow independent operational outcomes, not file or implementation layers.
- [ ] Different topics or services are split when independently useful and safe.
- [ ] Required pieces of one outcome remain together.
- [ ] Authoritative and current state were inspected where relevant.
- [ ] Failure or risk has a red-capable safe reproduction.
- [ ] The highest-risk boundary was exercised directly.
- [ ] No failed, skipped, ambiguous, unavailable, timed-out, or killed check is represented as passed.
- [ ] Review covers risky assumptions and the exact artifact.
- [ ] Rollback, data safety, and blast radius are addressed.
- [ ] Status language matches the evidence exactly.
- [ ] Completion means the requested outcome was applied and verified.
