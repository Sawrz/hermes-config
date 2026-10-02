---
name: security-sensitive-protocol-review
description: Use when reviewing durable state, external writes, and proof-bearing protocols for security or correctness flaws.
version: 2.0.0
metadata:
  hermes:
    tags: [security, protocol, state, idempotency, evidence]
    related_skills: [evidence-driven-change-control]
---

# Security-sensitive protocol review

Review protocols whose correctness depends on durable state, trust boundaries, retries, external side effects, or evidence. This skill is for protocol invariants and adversarial verification. It does not define project campaigns, reviewer assignments, PR sequencing, deployment policy, or capability packaging.

## 1. State the protocol

Before reviewing code, write down:

- trusted and untrusted inputs;
- authoritative systems;
- identities and privilege boundaries;
- durable state and its owner;
- every externally visible side effect;
- success, retry, reconciliation, and terminal-failure states;
- what evidence proves each transition;
- crash boundaries before and after every write.

If the implementation has no explicit model for one of these, treat that as a finding rather than inventing semantics.

## 2. Separate observations from proof

A local return code, rendered message, queued request, or generated artifact proves only that local step. It does not prove remote acceptance, human receipt, durable publication, deployment, activation, or runtime health.

Bind evidence to the exact object it claims to prove: resource ID, source version, commit, request digest, generation, state revision, or equivalent stable identity. Reject stale, ambiguous, skipped, unavailable, or wrong-target evidence.

## 3. Review writes and retries

For every mutation, determine:

1. whether the target is read immediately before writing;
2. how authorization is bound to exact target and payload;
3. whether the operation is naturally idempotent;
4. what happens if transport fails before dispatch, during dispatch, or after remote commit;
5. how an ambiguous outcome is reconciled without a blind retry;
6. what independent readback proves the requested state;
7. whether concurrent workers can both pass the precondition.

A write with uncertain outcome remains `indeterminate` until authoritative readback resolves it. Do not relabel it failed and retry automatically when duplication or destructive repetition is possible.

## 4. Review durable state transitions

Inspect ordering around:

- write-ahead intent;
- claim or lease acquisition;
- external mutation;
- acknowledgement;
- cursor advancement;
- cleanup or compaction.

State must survive restart without permanently consuming uncompleted work or repeating a committed destructive action. Use atomic replacement for single-file state and transactions or explicit ledgers for multi-object state. Synchronize concurrent writers with a lock or storage primitive that spans the actual process boundary.

Reject:

- acknowledgement before the event it claims to acknowledge;
- cursor advancement before durable consumption;
- state files overwritten in place;
- shared mutable state without concurrency control;
- retries that lose exact request identity;
- fallback parsing of free-form prose as protocol state;
- unbounded ledgers, queues, payloads, or pagination.

## 5. Review trust boundaries

Untrusted content must never become instructions, executable arguments, paths, URLs, SQL, configuration keys, or authorization decisions without deterministic validation.

Credentials must stay out of command arguments, environment fallbacks, logs, hashes, generated prompts, artifacts, and error messages. Verify effective identity and permitted scope through harmless operations; file presence alone is not proof.

For URLs, redirects, archive paths, filesystem trees, and container mounts, validate the normalized effective target rather than the input string. Symlinks, alternate encodings, case folding, default ports, parent traversal, and mount visibility are common boundary escapes.

## 6. Adversarial verification

Exercise the highest-risk real boundary. Include:

- restart at each durable transition;
- duplicate and replayed input;
- two concurrent workers;
- stale source revision;
- malformed and oversized input;
- partial pagination and cap exhaustion;
- remote success followed by local timeout;
- readback mismatch;
- unauthorized identity;
- symlink or path-substitution attempts;
- state-write and atomic-rename failure;
- no-change execution.

A killed, skipped, timed-out, unavailable, or resource-exhausted test is incomplete, never passing.

## Finding format

For each finding provide:

- severity;
- violated invariant;
- exact evidence and source location;
- reproducible failure path;
- blast radius;
- smallest durable fix;
- verification required at the real boundary.

Do not report style preferences as security defects. Do not claim a mitigation closes a protocol gap unless it restores the violated invariant.

## References

- `references/durable-state-adversarial-probes.md`
- `references/atomic-replace-writable-tree-review.md`
- `references/bidirectional-projection-adversarial-matrix.md`
- `references/python-urlsplit-normalization.md`
