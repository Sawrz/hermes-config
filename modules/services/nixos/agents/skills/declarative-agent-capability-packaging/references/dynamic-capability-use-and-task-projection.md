# Dynamic capability use and optional task projection

Use this reference when a declaratively packaged agent capability is available to a profile, but its human and assistant need to decide at runtime whether and how to use it.

## Two-layer contract

Keep availability/authorization separate from use:

```text
Declarative managed layer
  installs implementation and managed skills/CLIs
  authorizes profiles, services, routes, and action classes
  materializes credentials
  owns scheduler templates, deterministic state, validation, and safety bounds

Validated mutable preference layer
  enables/disables already-authorized behavior
  chooses bounded cadence, editions, categories, limits, and delivery windows
  selects an already-authorized destination
  selects interaction mode such as off/manual/suggest/auto
```

Installing a capability must not start behavior by itself. Disabling runtime use must not remove its package or require a repository edit, rebuild, deployment, service restart, or direct scheduler-state mutation.

A runtime preference may not grant new authority. New credentials, profiles, service backends, integration directions, unrestricted targets, implementation code, schemas, tool access, or weaker safety bounds remain declarative changes.

The human owns policy intent. An authorized agent may apply that intent through a managed CLI/API. Enabling a standing side-effect mode such as automatic external task creation requires explicit human direction; disabling it is always safe. Preference writes must be atomic, schema-validated, profile/human scoped, auditable, and read back after mutation.

## Managed implementation, dynamic activation

Automatic cross-service behavior remains a managed directional `serviceIntegration` even when its activation is mutable. Dynamic activation does not turn the workflow into ad-hoc agent behavior.

The managed implementation retains:

- source and target validation;
- stable source identity and idempotency;
- bounded selectors and limits;
- deterministic pending/retry/acknowledgement state;
- mutation readback;
- monitoring and failure classification;
- explicit authorization checks on every run.

The mutable layer selects only supported behavior inside those bounds.

## Optional projection to a human task system

Candidate discovery and human-action projection are separate capabilities. A profile contract may select a predeclared directional integration and provide bounded mutable policy such as:

```yaml
projection:
  destination_binding: <configured-binding>
  mode: manual
  resource_selector: <configured-resource>
  threshold: <bounded-rule>
  require_current: true
  require_verified: true
```

The preference file references only bindings and resources already authorized by declarative configuration. It must not name a backend that is absent, add credentials, carry cursors or pending writes, or provide arbitrary executable hooks.

Before automatic projection, require stable candidate identity, recent authoritative verification, bounded semantic output, threshold match, destination validation, idempotency lookup, one target mutation, and exact readback.

If a candidate becomes invalid before projection, do not create the task. If it changes after projection, reconcile according to the selected integration contract; do not infer human completion or delete human work automatically.

Supporting another task system requires a separately managed backend and explicit profile authorization, not a speculative universal adapter.

## Verification checklist

- Capability installation alone starts no behavior.
- The profile cannot enable a backend or destination it was not declaratively authorized to use.
- Runtime enable/disable succeeds without Nix/repository/deployment/scheduler changes.
- Preference mutation is atomic, validated, profile-scoped, audited, and read back.
- Automatic side effects require explicit human opt-in.
- Disabled/no-op runs perform no target mutation, agent wake, delivery, or model call.
- Projection is idempotent across retries and restart.
- Candidate lifecycle and human workflow lifecycle are not conflated.
