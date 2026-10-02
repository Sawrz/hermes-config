# Native Kanban capability closure audit

Use this reference when a declarative capability graph must package or validate Hermes Kanban roles and Forgejo/Vikunja edges without becoming a second Kanban configuration source.

## Native ownership boundary

Hermes owns Kanban storage, boards, task IDs, dispatcher, claims, runs, lifecycle, routing, tools, and dashboard behavior. The host configuration may validate that the target provides Kanban, install role guidance and integration adapters, require toolsets, and package deterministic jobs. It must not expose `internalServices.kanban`, construct database paths, open SQLite from reconcilers, or define a parallel task schema.

Model native capability, interactive access, and domain-role requirements separately:

```nix
requires = {
  hermesCapabilities = [ "kanban" ];
  # Empty for ordinary dispatcher assignees: task scope injects worker tools.
  hermesToolsets = [ ];
  skills = [ forgejoApi ];
  integrationPackages = [ forgejoCollector ];
  externalServiceCapabilities = [
    { service = "forgejo"; capability = "issueIntake"; }
  ];
};
```

Use actual package references for skills and integration packages. Reserve symbolic strings for target-native capability/toolset checks and configured service capability assertions. A deterministic collector implemented as a host systemd service/timer is an integration package, not a Hermes cron dependency.

## Worker, interactive operator, and orchestrator contexts

Follow the native Hermes gating rather than inventing a synthetic worker bundle:

- **Dispatcher-spawned worker:** `HERMES_KANBAN_TASK` automatically injects the task-scoped lifecycle tools and worker protocol. Do not package a `kanbanWorker` base or globally enable the Kanban toolset merely to make an assignee work.
- **Interactive Kanban operator:** a trusted profile explicitly enables the native `kanban` toolset so a normal conversation can inspect, create, comment on, route, unblock, and troubleshoot board work. This is privileged board-wide access. Hermes currently exposes one full out-of-task routing surface, not a technically restricted read/create-only subset; do not claim policy labels provide verb-level isolation.
- **Kanban orchestrator:** interactive operator access plus explicit decomposition, assignment, fan-out/fan-in, and dependency-routing guidance. The orchestration skill adds behavioral policy, not another tool implementation or permission system.
- **Integration roles:** issue work, PR authoring, PR review, and lifecycle reconciliation add only their domain skills, external-service authority, and typed policy. They require a valid spawnable assignee but do not transitively install a worker lifecycle bundle.

Select interactive operators explicitly and narrowly; trusted operational profiles may need this access for troubleshooting outside a dispatched task, while ordinary specialists should not receive board-wide tools by accident. An orchestrator must not implicitly receive Forgejo PR, implementation, review, SSH, deployment, or service credentials.

Test dispatcher scope and profile toolset separately: task scope must hide board-wide routing tools even when the same profile also has interactive access in normal conversations.

## Native task linking contract

Use native primitives before metadata extensions:

- native task ID plus board slug for Kanban identity;
- native idempotency key for retry-safe logical creation;
- body/comments for durable visible source and reciprocal links;
- native parent/child links for workflow lineage;
- completion-run summary/metadata for immutable attempt and handoff evidence only;
- independently directional integration ledgers for exact pair mappings, cursors, pending delivery, retries, acknowledgements, and observed external revisions.

Do not add host-owned `external_refs`, service-specific task columns, or treat completion metadata as mutable live task metadata.

## Dashboard deep-link boundary

A task-detail API by ID is not evidence of a human dashboard deep link. Verify both sides independently:

1. The backend can fetch one task by native ID.
2. The frontend parses a documented browser URL on direct load.
3. It selects the correct board and opens task detail after validation.
4. Opening/closing updates browser history and back/forward restores state.
5. Unknown board/task fails explicitly without cross-board fallback.
6. Hermes exposes or documents the canonical URL builder/format.

If frontend route parsing and history behavior are absent, record a narrow native dashboard capability gap. Do not infer URL syntax from API paths and do not concatenate a guessed URL in Nix or an integration package. Hermes implements the native route; host packages consume the verified contract.

## Directional edge contract

- Forgejo → Kanban creates or reuses one canonical native task and records exact source/event state in that direction's ledger.
- Kanban → Forgejo publishes only configured replies/reviews/proof and retries only that edge.
- Kanban → Vikunja places the board/task reference and supported native deep link in the human task, then appends the verified Vikunja ID/URL as a native Kanban comment.
- Vikunja → Kanban ingests replies as comments and applies only policy-authorized deterministic transitions. Human completion is evidence, never implicit merge, deploy, rebuild, rollback, or source closure authorization.
- There is no direct Forgejo → Vikunja authority edge when Forgejo owns source truth, Kanban owns execution, and Vikunja is only a human-action projection.

## Minimum acceptance matrix

Require tests for:

- capability unavailable → evaluation failure with dependency provenance;
- exact worker versus orchestrator tool surfaces;
- role isolation and least privilege;
- transitive deduplication, explicit-root retention, and final-reference removal;
- idempotent replay returning the same native task;
- comments, parent/child links, and completion-run evidence round-tripping;
- absence of `internalServices.kanban`, DB paths, and host-owned external-reference schemas;
- dashboard references use board slug plus native task ID and never synthesize an undocumented URL; when a native deep-link contract exists, additionally test direct-load/refresh/history/invalid-target behavior;
- durable-before-wake and healthy no-change zero-session behavior;
- independent edge retry, especially downstream projection failure without source replay or another model call;
- human-task completion not escalating authorization;
- unrelated profiles receiving no transitive credentials or role packages.
