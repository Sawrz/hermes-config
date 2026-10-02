# Durable-state adversarial probes

Use these as small in-memory or temporary-directory tests during a read-only implementation audit. Adapt names to the target package; do not add fixtures to the repository unless asked.

## Existing-generation durability

Model an unselected generation left by a crash after `rename(generation_tmp, final)` but before `fsync(generations_parent)`. Retry identical publication while tracing directory-sync calls. Require this order even on the dedup/reuse branch:

1. Validate the complete existing generation.
2. `fsync(generations_parent)`.
3. Atomically replace the selector.
4. `fsync(selector_parent)`.

A trace containing only the selector parent is a crash-consistency defect: reading or hashing an existing generation does not persist its directory entry.

## Strict JSON constants

Probe both APIs:

```python
publish({"data.json": {"value": float("nan")}})
canonical_json_bytes({"value": float("inf")})
```

Also feed raw `NaN`, `Infinity`, and `-Infinity` through every decoder. Expected: a protocol error before mutation. Acceptance, round-trip success, or emitted non-standard tokens is fail-open.

## Concurrent apply/readback

Use two threads or processes plus barriers to force:

```text
A reads old
B reads old
A publishes generation A
B publishes generation B
A performs readback
```

If A reports failure only after generation A was durably published, apply/readback is not atomic and its outcome is ambiguous. Also inspect each audit record: `previous_generation` and changed keys must correspond to the predecessor selected inside the same exclusive transaction.

## Audit retention

Run more applies than the documented retention limit, including no-op applies with distinct timestamps/reasons. Count complete generation directories and readable audit records. Field-length limits are insufficient; retained history itself must be capped while preserving the selected generation and required rollback window.

## Bounded preference values

For each persisted string-like type, test:

- a one-million-character value;
- C0 controls and newline/tab;
- default-ignorable Unicode if the contract excludes it;
- maximum serialized document size;
- maximum collection cardinality and member length.

A finite enum count does not bound member size. Require explicit member-length/text-profile validation and bounded serialized state.

## Reporting

For each Blocking/Important defect, report the exact implementation line governing the bad branch, the shortest executable probe, and observed output. Passing repository tests do not override a direct adversarial reproduction.
