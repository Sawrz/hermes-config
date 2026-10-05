# hermes-config

Reusable Hermes NixOS runtime integration, capability resolution, managed
skills, deterministic workflows, native cron reconciliation, repository caches,
Forgejo adapters, Vikunja projections and Nix maintenance helpers.

Import `nixosModules.default` explicitly on agent hosts. The optional
`nixosModules.incident-reconciliation` accepts an explicit metrics directory;
its host exporter is configured by deployment. `overlays.default` and `packages`
expose project helpers and the pinned Hermes Python/SQLite runtime selection.
Hosts, profile selection, personal identity, endpoints, secrets and policies
remain in the consuming deployment.

The module owns each enabled profile's `cache` parent and `cache/vision`
directory, including the default profile. Activation and tmpfiles repair their
ownership and mode using the configured Hermes service user/group, without
recursively changing existing files or agent memory. Rebuild and activate the
consuming host after updating the input; a successful build alone does not repair
an existing runtime. Check this contract with
`nix build --no-link .#checks.x86_64-linux.hermes-cache`.

`custom.services.hermes.repositoryAuthority` declares the owning persistent profile,
review executor, board context and `repositories`, keyed by instance name. This
registry is independent of Kanban selection. `reviewExecutor = "paperclip"` delegates
reviews to Paperclip task agents and requires the Hermes reviewer to remain disabled;
it does not provision a new Hermes profile or copy its knowledge.
Each declaration binds one Forgejo repository, roles, native namespace and state
subdirectory. Existing deployments explicitly retain the empty state suffix;
additional repositories use `/repositories/<name>`. The same persistent implementation agent and review executor can
serve every instance. Helpers receive an exact per-invocation contract; agent
sandboxes receive `/run/hermes-repository-instances/<name>.json`.

`managedSourceRepositories` attributes packaged source roots and change routes.
Managed skills remain immutable; private profile knowledge, native identities,
receipts, pending work and state are never seeded or copied from these sources.
Maintenance inventory selects `maintenanceRepositoryInstance` and continues to
evaluate the deployment flake's real `nixosConfigurations`.
Each image candidate retains its owning repository, revision and path in
`change_route`; catalogue changes and deployment overrides are planned against
their respective repositories. Refresh reports without ownership before new
curation. Existing pending intents keep their original identities and receipts.

Run `nix flake check --no-build` to evaluate standalone fixtures. CI builds checks
one at a time to bound evaluator memory. Development and agent writes use the
private Forgejo repository. GitHub is a private recovery mirror.

`hermes-repository-task` prepares review handoffs bound to the repository, PR and
exact head commit. It rejects unknown repositories and colliding registry entries.
The caller must use its authorized Paperclip task workflow to dispatch and read back
the task; the helper performs no network writes and never claims dispatch. Paperclip
owns task-agent provisioning and their credentials, while Hermes owns persistent
profiles with memory. Paperclip routing cannot open existing Hermes adapter stores.
Existing receipts retain their binding and are not replayed or migrated by this change.

## Source access and recovery

Standalone inputs use the private Forgejo upstream mirrors. CI requires the
`NIX_FORGEJO_SSH_KEY` Actions secret with read-only access through `nix-builders`.
Public upstream recovery uses HTTPS GitHub Git, without an API token. CI runs
checks through `recovery/sources.py`; raw Nix commands do not invoke fallback.

For local checks, use your normal Forgejo Git identity:

```sh
python3 recovery/sources.py run --source . --target checks -- \
  nix flake check '{flake}' --no-write-lock-file --no-update-lock-file
```

Recovery retains the locked commit and content hash, and only handles transport
outages. Authentication and integrity errors stop the operation. Run
`python3 recovery/sources.py update-check --source .` before `nix flake update`;
updates require Forgejo. The recovery and runner-access helpers are shared
copies of the tested `nix-config` implementation and should be updated together.
