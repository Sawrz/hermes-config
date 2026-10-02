# Unscoped service-account credentials

Use this reference when several independently configured consumers may need one service account and the service does not provide usable per-token scopes.

## Verify the product contract

Before designing the binding, verify from official documentation, exact-version source when needed, and an identity-safe runtime probe:

- token cardinality per account;
- token scopes and resource ownership rules;
- delegated access or impersonation semantics;
- audit attribution;
- rotation and revocation coupling.

Do not generalize one product or version to another.

## Honest security model

Separate credential files or secret references are not separate security identities when they contain an equivalent unrestricted token. In that case state explicitly that:

- every holder has the same service-account authority;
- the service cannot attribute actions to an individual binding;
- compromise, revocation, and rotation are coupled;
- separate bindings are stable interfaces or future separation points, not current least-privilege enforcement.

Never compare secret values in Nix, CI, logs, skills, task metadata, or agent output. A secret manager may deduplicate storage internally without changing the declared consumer contracts.

## Available designs

Choose one supported design and state its limitations:

1. shared full-authority account;
2. one direct owner with explicit handoffs;
3. separate service accounts with genuinely separate permissions or data;
4. a deliberately owned authorization proxy.

Do not invent scopes or claim isolation the service cannot enforce. Behavioral role restrictions remain agent policy, not service-side authorization.

## Verification

- Authenticate every configured binding independently without exposing values.
- Verify effective account identity and allowed/denied operations.
- Confirm unrelated consumers receive no credential file.
- Confirm rotation and revocation procedures cover every equivalent binding.
- Re-evaluate the design when the deployed service adds usable scoped identities.

Profile selection, role policy, concrete secret paths, schedules, migration, and rotation execution belong to deployment configuration and explicitly authorized lifecycle tasks.
