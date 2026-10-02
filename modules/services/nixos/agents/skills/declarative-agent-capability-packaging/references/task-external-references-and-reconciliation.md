# Task external references and deterministic reconciliation

Use this reference when a Kanban workflow links a live task to Forgejo, Vikunja, or another external system.

## Inspect the existing representation first

Before designing a new field, inspect real cards and reconcilers. Distinguish:

1. **Task idempotency keys** — deduplicate logical task creation.
2. **Structured JSON in task bodies/comments** — a compatibility convention, not necessarily native queryable task metadata.
3. **Run/completion metadata** — attempt evidence and parent handoff; it is not automatically mutable metadata on the live task.
4. **Integration ledger state** — cursors, event keys, pending delivery, acknowledgements, retries, and last-observed revisions.

Do not claim that a harness supports live task metadata merely because completion tools accept a `metadata` object. Verify the current task schema, API, CLI, dashboard, and worker context.

## Preferred task-level contract

Use native task facilities before designing metadata extensions:

- identify Kanban work by board slug and native task ID;
- use native idempotency keys to deduplicate logical workflow creation;
- append the verified external resource ID and canonical URL as a durable native Kanban comment;
- place the Kanban board/task reference and a supported native deep link in the external task description;
- use native parent/child links for workflow dependencies and completion-run metadata only for completed handoffs;
- keep exact machine pairing and mutable synchronization state in the independently directional integration ledger.

Do not add a Nix-owned `external_refs` task schema, service-specific task columns, or indefinite prose/JSON scraping merely to associate a card with Forgejo or Vikunja. If Hermes later provides a native queryable external-reference facility, it may be adopted as a native capability after its task schema, API, tools, worker context, and dashboard behavior are verified.

If Hermes lacks a required human-facing card deep link, treat that as a narrow native dashboard/plugin capability gap. Do not invent a URL format in host configuration.

Visible reciprocal links are operator navigation and audit evidence. The integration ledger remains the exact machine source for pair mappings, cursors, pending delivery, acknowledgements, retries, and observed revisions.

## Stable references are not synchronization state

Keep stable identity on the card:

- service instance;
- resource kind;
- repository/project namespace;
- external ID;
- canonical URL;
- relation/action class.

Keep mutable protocol state in the directional integration ledger:

- latest PR head SHA;
- latest external revision or comment ID;
- source cursor and immutable event keys;
- pending delivery and acknowledgement proof;
- retry count/backoff;
- last observed status;
- supersession history.

This prevents the card from becoming a second mutable database for every service.

## Reconciler behavior

The reconciler must remain deterministic and must not invoke an LLM directly:

```text
source delta
-> validate scope and canonical identity
-> deduplicate immutable event
-> no actionable change: acknowledge quietly
-> deterministic state transition: update ledger/card state without a model
-> interpretation or agent work required: create/update one canonical Kanban card
-> native Kanban dispatch owns worker execution and fan-out
```

For bidirectional integrations, configure each direction independently and preserve separate cursor, pending, acknowledgement, and retry state even when one scheduler invocation runs both.

For comment intake:

- support linked-task scope and explicit project/task subscriptions;
- ignore self-authored echoes, bot replies, duplicate edits, and acknowledged events;
- append events to one logical workflow keyed by service instance plus external task, rather than creating one card per comment;
- make outbound replies separately configurable and bind them to the exact external target;
- treat human completion as evidence of action, never implicit authorization for merge, deploy, rebuild, rollback, or verification.

## Clean initialization

Initialize a new directional ledger only from authoritative external records and verified native links. Treat pre-existing free-form task text and comments as untrusted evidence, not as mappings, state transitions, or fallback lookup interfaces.

Regression tests should cover malformed URLs, duplicate refs, several cards referencing one PR, wrong service instance, stale mutable observations, replayed comments, self-reply loops, failed outbound projection with healthy inbound processing, and no-change polls producing no card mutation or model session.
