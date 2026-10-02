# Hermes workflow state foundation

This package is the shared deterministic state boundary for issue #200 integrations. It is deliberately not a scheduler, workflow engine, Kanban mirror, target adapter, or compatibility reader. Hermes keeps native ownership of Kanban, cron, dispatch, lifecycle, and delivery. Domain integrations own only their directional state.

## Immutable generations

`GenerationStore` publishes one complete set of canonical JSON documents under:

```text
<state>/
  .lock
  current.json
  generations/
    g-<sha256-of-manifest>/
      manifest.json
      <domain documents>.json
```

Publication writes and `fsync`s every document, writes and `fsync`s the manifest, `fsync`s the generation directory, atomically renames it into `generations/`, `fsync`s the generations parent, then atomically replaces the sole selector and `fsync`s the state root. Readers hold a shared lock from selector read through generation validation; writers and cleanup hold the exclusive lock. `GenerationStore.update()` keeps read, transform, publication, and readback under that exclusive lock for read-modify-write clients. A restart therefore selects a complete old or complete new generation, never directory-order state or mixed top-level files.

The selected manifest and every document are exact-schema and digest validated. Missing, truncated, incompatible, duplicate-key, non-finite-number, extra-object, path-traversal, absolute-path, control-character, symlink, and unexpected-object state fails closed. Once protocol state exists, a missing or corrupt selector never falls back to old files. Cleanup validates the selected generation first, never removes it, and refuses symlinked or unexpected entries.

Domains supply document schemas and authoritative initialization logic. This library does not import historical files or infer current state from generation ordering. The coordinated hard cut initializes each domain from its authoritative live system without replaying old history.

## Directional state convention

`DirectionalWork` keeps logical work identity separate from each target edge. Each edge has one of:

- `pending`
- `in_flight`
- `ambiguous`
- `confirmed`
- `transient_failed`
- `permanent_failed`

A lost response becomes `ambiguous` and cannot retry until stable-identity readback reconciles the external effect. Confirmation requires bounded durable proof. Only a transiently failed edge below the retry limit is retryable; confirmed sibling edges remain confirmed. `RetryPolicy` supplies bounded exponential delay. Persist the resulting exact document inside a `GenerationStore` generation together with the domain cursor, pending work, receipts, acknowledgements, and tombstones.

Tombstones are domain-owned and must remain selected until a durably selected cursor has moved past the source overlap window. Do not acknowledge source receipt, target mutation, or local handling as though they were the same boundary.

## Bounded preferences

`hermes-workflow-preferences` provides:

```console
hermes-workflow-preferences --state-dir /var/lib/example/preferences \
  --contract /etc/example/preferences-contract.json show
hermes-workflow-preferences ... validate --input requested.json
hermes-workflow-preferences ... preview --input requested.json
hermes-workflow-preferences ... apply --input requested.json \
  --actor operator --reason 'enable weekly edition'
hermes-workflow-preferences ... readback --input requested.json
```

The immutable Nix-owned contract declares only bounded boolean, integer, enum string, and unique bounded string-list properties. It must include an `enabled` boolean with a safe `false` default. Unknown keys and out-of-bound values fail before mutation. Apply serializes validation of current state, publication, and exact readback; it then retains the selected and one previous complete generation. A retention-cleanup failure is reported separately from the already-durable apply so callers do not retry a successful mutation. The contract cannot add credentials, commands, external targets, scheduler state, or new authority.

Example contract:

```json
{
  "schema_version": 1,
  "name": "digest-edition",
  "preference_version": 1,
  "properties": {
    "enabled": {"type": "boolean", "default": false},
    "cadence_minutes": {"type": "integer", "minimum": 60, "maximum": 1440, "default": 240},
    "language": {"type": "string", "enum": ["de", "en"], "default": "en"},
    "topics": {"type": "string-list", "max_items": 16, "max_length": 80, "default": []}
  }
}
```

## Metrics

`render_metrics` and `write_metrics` accept only five fixed, label-free counters:

- `publications_total`
- `reconciliations_total`
- `retries_total`
- `corruptions_total`
- `lock_contentions_total`

Event, user, card, source, and generation identities belong in protected logs or state, never Prometheus labels.

## Verification

Run the behavioral suite directly:

```console
python3 -m unittest discover -s pkgs/common/hermes-workflow-state/tests -v
```

Build the exact Nix package from the Alakazam evaluation that carries the custom package overlay:

```console
nix build .#nixosConfigurations.alakazam.pkgs.hermes-workflow-state --no-link
```

The suite covers publication fault boundaries, first publication, immutable readback, corruption and incompatible schemas, duplicate JSON keys, selector traversal/canonicalization, symlinks, cleanup confinement, writer contention, reader/writer locking, ambiguous external mutation and readback, failed-edge-only retry, state invariants, bounded preferences, non-mutating rejection, CLI readback, and bounded metrics.
