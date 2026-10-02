# Native-first target integration

Use this reference when a capability graph proposes a target adapter, renderer, scheduler registration layer, generated access artifact, or other harness-specific glue.

## Decision order

For every proposed responsibility:

1. Inspect the current target's documented and deployed native contract.
2. Inspect the existing host/module mechanism already used in production.
3. Use the native or existing mechanism directly when it satisfies the requirement.
4. Treat pure host-language closure evaluation as internal plumbing, not a public architectural component.
5. Add a true adapter only for a demonstrated residual layout, invocation, or registration mismatch.
6. If support is absent, reject or defer unless custom ownership is explicitly authorized. Do not silently require an upstream PR, local fork, source clone, private-module import, or direct runtime-database mutation.

## Classification table

| Need | Preferred owner | Adapter? |
|---|---|---|
| Endpoint and credential materialization | Existing host/service credential mechanism | No |
| Skill discovery and immutable package exposure | Harness-native skill directories plus host package closure | No |
| Toolset or plugin enablement | Harness-native configuration | No |
| Native task/work-queue lifecycle | Harness-native tools, CLI, API, dispatcher, and storage | No |
| Scheduled agent reasoning | Harness-native scheduler | No |
| Deterministic machine transport/reconciliation | Existing authoritative host service/timer when declarative host ownership is required | No; it is an integration implementation |
| Cross-target file-layout translation | Minimal deterministic renderer after mismatch proof | Possibly |
| Service-to-service protocol, cursor, retry, acknowledgement | Directional integration package and ledger | No |
| Secret values | Existing secret owner only | Never |

## Agent cron versus machine integration

Do not force all periodic activity into one scheduler merely because both are time-triggered.

- **Agent cron** schedules a fresh agent session or a harness-native script job and remains owned by the harness.
- **Machine integration transport** collects, persists, reconciles, and acknowledges external state deterministically. When NixOS already owns deployment and the harness lacks a supported declarative managed-job contract, a systemd service/timer is the honest owner.

The host service may route durable actionable work through the harness's supported CLI/API. It must not open the harness's private database directly. Unchanged runs must create no agent session or model call.

This split is not a second agent scheduler: systemd owns deterministic machine execution; the harness owns agent scheduling and task dispatch.

## Genuine adapter contract

If a residual adapter remains, document:

- the exact native-contract mismatch;
- inputs and deterministic outputs;
- supported target versions;
- collision and removal behavior;
- runtime verification at the real consumer;
- explicit non-ownership of credentials, service semantics, scheduler behavior, workflow authority, and mutable state.

If this list cannot be completed with a concrete consumer, the adapter is speculative and should not exist.

## Generated service access

A generated service-instance overview is optional, not automatically an adapter. Use the harness's native skill mechanism. Include only non-secret endpoint/credential-path references, explicit resource scope, selected capabilities, and canonical skill links. Do not duplicate API documentation or workflow policy.

## Missing convenience features

Do not invent unsupported interfaces. For example, if a task system lacks a human dashboard deep link, preserve its native task ID and omit the URL. A convenience gap does not authorize an invented route or a target fork.
