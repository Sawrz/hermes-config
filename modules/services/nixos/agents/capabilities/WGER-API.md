# Managed wger API capability

This package is the shared, interactive wger application framework for issue #200 A40.

Public roots remain service-role bindings:

- `externalService:wger:nutritionist`
- `externalService:wger:fitness-coach`

Both depend on one canonical `agentSkill:wger-api` package and publish one immutable `wger-api` skill. They do not select profiles themselves; P70 owns final profile root selection and secret wiring. The role names identify the intended application interface, not a claim that the current permanent tokens have independent wger scopes.

The helper is standard-library Python. It owns runtime-file validation, permanent-token header authentication, live OpenAPI checks, bounded pagination and retries, origin-safe redirects, typed failures, preview and exact confirmation, read-before-write reconciliation, indeterminate-write handling, and mandatory readback. Write authority is statically limited to reviewed weight-entry create/update routes; live schema discovery cannot substitute another mutation path. It has no scheduler, collector, delivery, Kanban, mutable state, environment fallback, or live-service deployment behavior.

Focused tests:

```console
python3 -m unittest discover modules/services/nixos/agents/skills/wger-api/tests -v
```

Nix package/binding and fresh-worker publication are covered by:

```console
nix build .#checks.x86_64-linux.hermes-wger-api-framework --no-link
```
