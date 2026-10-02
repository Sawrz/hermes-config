# Native task linking before metadata extensions

Use this note when a service integration must associate a native harness task with an external resource.

## Rule

Prefer the harness's existing task identity, idempotency, body/comments, links, and handoff facilities. Do not introduce a second host-managed task metadata model merely because completion runs accept metadata or because structured JSON has historically been embedded in prose.

For a Kanban↔human-inbox integration:

- identify the Kanban side by board slug and task ID;
- append the verified external task ID and canonical URL as a durable native Kanban comment;
- place the Kanban board/task reference and supported native deep link in the external task description;
- store exact machine pairing, cursors, pending delivery, acknowledgements, retries, and observed revisions in the independently directional integration ledger;
- never scrape titles or prose as the normal reconciliation source;
- never invent a dashboard URL format in host configuration.

If the harness lacks a required human-facing task deep link, treat that as a narrow native dashboard/plugin capability gap. Add or consume the native feature rather than creating a parallel Nix-owned Kanban schema.

A native queryable external-reference facility may be used when it already exists and is supported. Do not make speculative `external_refs` infrastructure a prerequisite when native task IDs, reciprocal visible links, and the integration ledger satisfy the requirement.

The deterministic reconciler must decide no-op before model creation. It may update native workflow state for deterministic transitions; only reasoning or agent work should create/update a canonical Kanban card and enter native dispatch.
