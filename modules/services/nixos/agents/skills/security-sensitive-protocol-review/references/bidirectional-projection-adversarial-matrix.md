# Bidirectional projection adversarial matrix

Use this for deterministic, independently stateful A↔B projections where each edge polls a source, mutates a destination, and must recover exactly once after ambiguous writes.

## Core phase model

Test each direction independently and then concurrently across this boundary:

`observe → durable pending → dispatch → typed readback → durable acknowledgement → cursor advance`

Inject a crash or lost response before and after every boundary. A safe implementation preserves unresolved work, never advances a cursor beyond unresolved source events, and reconciles a committed-but-unacknowledged write without duplicating it.

## Highest-value probes

| Probe | Required result |
|---|---|
| Mutation commits; response is lost | Retry locates the exact destination mutation by stable identity, verifies its complete postcondition, and acknowledges without a duplicate. |
| Response succeeds; ledger commit is lost | Same reconciliation path; exactly one destination mutation remains. |
| Ledger commits; cursor commit is lost | Durable acknowledgement/tombstone suppresses replay while the cursor catches up. |
| Source changes, disappears, or leaves scope after ambiguous dispatch | Reconcile from the immutable durable pending snapshot, not the latest source snapshot. |
| Two workers process one source revision concurrently | Server-enforced idempotency/uniqueness or serialization produces one destination mutation. A dictionary fake is not evidence. |
| Two revisions share object ID and coarse timestamp | Canonical payload/action/state digest distinguishes them, or processing stops on an explicit collision. |
| Unrelated destination text contains the marker | Reject unless trusted creator, destination container, source identity, source revision, canonical payload, and exact intended body/state all match. |
| Destination readback is stale, partial, malformed, absent, or has multiple matches | Keep work pending; never acknowledge a guessed match. |
| Both edges run concurrently | Cursors, pending work, acknowledgements, retries, tombstones, generations, and marker namespaces remain direction-local. |
| Self-generated event returns through the opposite edge | Suppress only the exact projected revision; later human edits remain observable. |
| Pagination is empty, exact-cap, over-cap, mutating, cyclic, or partially failing | Complete a bounded traversal or fail explicitly; partial history is never treated as complete. |
| Retention limit is crossed | Compact only records proven safe to remove; preserve unresolved work and replay tombstones until cursor overlap has passed. |

## Marker requirements

Derive stable reconciliation identity from immutable fields such as:

- projection protocol/version;
- direction/edge identifier;
- source type and source object ID;
- immutable source revision identity;
- canonical payload digest.

Marker presence is only a lookup aid. Acknowledgement requires exact readback of the intended mutation and trusted provenance. Do not acknowledge based on substring presence, target ID alone, current source contents, or a caller-supplied match unrelated to the payload.

**Probe marker collision before mutation, not only before acknowledgement.** Preseed one unrelated destination object whose free text merely contains the predictable marker. The reconciler must fail closed and leave that object byte-for-byte unchanged. It is unsafe to find one marker candidate, overwrite it into the desired shape, and then let exact post-mutation readback manufacture the provenance needed to adopt it. Only a previously bound ledger identity or an already exact, trusted ambiguous-write result may be adopted.

## Full-replacement API mutations

Before using `PUT`, inspect the live operation semantics and request schema. If `PUT` replaces the whole resource, sending only projection-owned fields can silently clear due dates, priorities, scheduling, recurrence, labels, or other human-managed state while subset readback still reports success. Prefer a typed partial-update operation when available; otherwise perform a complete read-modify-write and verify both changed and preserved fields. Add a fixture with non-default unmanaged fields and assert they survive reconciliation.

## Scope and authority probes

- Missing, malformed, duplicated, contradictory, or unknown selectors must stop startup before polling or mutation; never broaden scope by default.
- Permanent inclusion rules must survive discovery gaps and exclusion attempts, or configuration must be rejected.
- Unsupported source classes should be ignored with an observable reason, not coerced into a supported class.
- Comments, completion flags, labels, or mirrored evidence must never acquire unrelated lifecycle authority such as merge, deploy, rebuild, rollback, verify, or close.
- Enforce credential and tool isolation structurally; prompt-only restrictions are not a capability boundary.

## Adapter realism

- Exercise every pinned native CLI verb and actual response envelope separately.
- Verify explicit destination/board/project IDs on every lookup and mutation; never rely on ambient defaults.
- Test real concurrency semantics around advertised idempotency keys; read-before-create is still a race.
- For APIs, require typed decoding, operation-specific postcondition readback, request/page bounds, pagination-loop detection, and error handling that cannot become empty-success.

## Generation-backed state

Apply the standard `GenerationStore` crash probes to each edge root: complete and sync generation content before selector publication, sync reused generation parents before selecting them, reject corrupt selectors and path escapes, and clean only after durable selection. Never share one mutable logical document between directions merely because they use the same state library.
