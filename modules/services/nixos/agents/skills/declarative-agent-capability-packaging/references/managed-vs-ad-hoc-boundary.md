# Managed versus ad-hoc capability boundary

Use this reference when deciding whether an agent skill, helper, workflow, schedule, or cross-service behavior belongs in declarative configuration or mutable profile context.

## Decision rule

Treat an artifact or behavior as **managed** when any of these apply:

- it is recurring or scheduled;
- it is intended for reuse across requests or profiles;
- it should not change without review;
- it participates in automation, deterministic collection, projection, reconciliation, or lifecycle transitions;
- it owns credentials, authorization boundaries, durable state, cursors, retries, acknowledgements, idempotency, or no-op/model-wake gates;
- silent drift could cause external mutation, duplicate/missing work, data corruption, secret exposure, or unbounded model cost.

Treat work as **agent-managed ad hoc** only when it is a one-off, interactive response to an explicit user request and introduces no standing schedule, reusable procedure, integration state, background behavior, or new authority.

A reusable service skill remains managed even when it is normally invoked interactively. Managed service frameworks include authentication, schema discovery, supported operations, helper scripts, app-specific conventions, safe mutation, confirmation, duplicate prevention, pagination, error handling, and readback verification.

## Cross-service distinction

Do not classify behavior merely because it touches multiple services.

- A standing, recurring, automated, event-driven, stateful, or retrying edge is a managed directional `serviceIntegration`.
- A one-off user request may let an agent compose multiple managed service capabilities interactively.
- If the ad-hoc pattern becomes reusable or recurring, promote it to a managed package or integration before continued use.

Mutable user preferences and current goals may remain profile-owned context. Changing an authoritative source or enabling synchronization still requires explicit user direction; mutable context is not authority to invent a standing integration.

## Inventory before migration

Other profiles' mutable skill and scheduler trees may be inaccessible to the reviewing profile. Do not infer their contents from repository connectivity declarations or conversation history.

Create read-only native Kanban inventory tasks for each owning profile. Require exact skill paths, linked references/scripts, cron metadata, credential-discovery method without secret inspection, operation coverage, safety/idempotency behavior, runtime state ownership, and a keep/replace/merge/remove recommendation. Prohibit mutations. Reconcile the returned evidence into the managed package design.

For duplicated API skills, compare mechanics and role policy separately. Promote canonical reusable mechanics and reusable safety/application conventions into managed packages; retain only genuinely mutable goals, preferences, and ad-hoc interpretation in profile context.

## Documentation discipline

Record refinements explicitly when a later decision narrows or supersedes earlier wording. End with a reconciled decision that distinguishes:

- architecture reviewed;
- inventory complete;
- implementation authorized;
- implemented;
- deployed;
- runtime verified.

Never report architecture completion as implementation completion.
