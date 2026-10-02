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
