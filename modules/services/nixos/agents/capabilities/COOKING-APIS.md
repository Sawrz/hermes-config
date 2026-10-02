# Managed Mealie and Grocy service capabilities

The cooking capability package publishes independent operation capabilities:

- `externalService:mealie:recipes` installs `agentSkill:mealie-api`;
- `externalService:mealie:meal-plans` installs `agentSkill:mealie-api`;
- `externalService:grocy:pantry` installs `agentSkill:grocy-api`;
- `externalService:grocy:shopping-list` installs `agentSkill:grocy-api`.

Each service skill describes supported operations, safe API use, authentication boundaries, confirmation, reconciliation, and readback for that service. Enabling a service capability starts no scheduler, synchronization, migration, or background workflow.

The skills do not decide which service an agent uses for recipes, meal planning, shopping lists, pantry data, or other responsibilities. That selection belongs to the agent contract and profile configuration.

The standard-library Python helpers require profile-private endpoint and credential files, verify live schemas and exact operations, bound pagination and retries, reject unsafe redirects, classify failures, require exact confirmation for writes, reconcile ambiguous outcomes once, and require readback. They do not transfer data between services or change service authority.

Focused tests:

```console
python3 -m unittest discover modules/services/nixos/agents/skills/mealie-api/tests -v
python3 -m unittest discover modules/services/nixos/agents/skills/grocy-api/tests -v
```

Nix publication and fresh-worker isolation are covered by:

```console
nix build .#checks.x86_64-linux.hermes-cooking-api-framework --no-link
```
