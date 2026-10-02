# Declarative Hermes capability graph

This directory implements the shared capability-package foundation from issue #200. It is deliberately small: profiles select roots; package metadata supplies dependencies and generated artifacts; Hermes keeps ownership of its native scheduler, Kanban, dispatcher, toolsets, profile lifecycle, and delivery.

## Public profile model

```nix
services.hermes-agent.profiles.system-admin = {
  agentSkills.hermes-kanban-large-task-orchestration = true;

  externalServices.forgejo = {
    endpoint = "https://git.example.test";
    credentialFiles.credential = config.sops.secrets."...".path;
    capabilities = [ "issue-intake" ];
    scope.include = [ "nixos/nix-config" ];
  };

  serviceIntegrations.forgejo-to-kanban.settings = {
    board = "homelab-devops";
    reconcileInterval = "1m";
  };
};
```

Connectivity alone starts no behavior. `scope.include` and `scope.exclude` are mutually exclusive. Exclude mode includes future resources, so it requires `acknowledgeFutureResources = true`. Mutation-capable repository integrations should use positive include lists.

An integration may be `true` when provider defaults are sufficient, or may use the structured `enable`, `instances`, and `settings` form. `settings` contains only bounded interaction policy declared by that directional provider through `allowedSettings`; unknown fields fail closed. Endpoints and credential references remain under `externalServices`, while packages, commands, paths, units, timers, and state layout remain provider-owned.

There is intentionally no public `internalServices`, target-adapter, `kanbanWorker`, package-list override, mount injection, scheduler, or private Hermes-state option.

## Defining packages

Repository modules import `lib.nix`, use one of its typed constructors, and register the result under its canonical ID:

```nix
let
  capabilityLib = import ./capabilities/lib.nix { inherit lib; };
  package = capabilityLib.mkAgentSkill "example" {
    requires = [ "agentSkill:portable-leaf" ];
    requiredHermesCapabilities = [ "toolsets" ];
    provides = {
      skills.example = {
        category = "repo-managed";
        source = ./example-skill;
      };
      toolsets = [ "web" ];
    };
  };
in
{
  custom.services.hermes.capabilityPackages.${package.id} = package;
}
```

Constructors:

- `mkAgentSkill name attrs`
- `mkExternalServiceCapability service capability attrs`
- `mkServiceIntegration name attrs`

The registry is an internal module interface, not a profile-facing root model. Package IDs and artifact names are stable provenance identities. Dependencies use canonical package IDs.

## Closure and publication

For each profile, the module:

1. derives roots from enabled `agentSkills`, selected external-service capabilities, and enabled `serviceIntegrations`;
2. computes a deterministic dependency-first transitive closure;
3. rejects missing packages, cycles, conflicting packages, unsupported targets, duplicate artifact providers, unsafe names, unsupported integration settings, unsupported native-Hermes requirements, missing configured services, hidden reserved mounts, and direct mutable/config shadows;
4. publishes canonical skill directories through Hermes `skills.external_dirs`, adds native toolsets through the existing profile config interface, and merges non-colliding top-level Hermes config fragments;
5. exposes a derived immutable provenance manifest through
   `HERMES_CAPABILITY_MANIFEST` in the profile gateway service.

The manifest is output, not another source of truth. It contains package identities and provenance, not credentials or mutable runtime state. Keeping it in the Nix store avoids a second mutable publication and cleanup protocol.

Explicit roots are retention roots. Removing one root removes only packages no longer reachable from another explicit or transitive reference. Repeated dependencies produce one artifact.

Runtime cursors, ledgers, preferences, retries, mappings, credentials, and application data do not belong in this package graph or manifest.

## Verification

The flake check is:

```console
nix build .#checks.x86_64-linux.hermes-capability-graph-eval --no-link
```

It covers valid dependency closure and publication; explicit-root retention and final-reference removal; generated config, toolset, skill-directory, supporting-file, and immutable provenance output; cycle/missing dependency/duplicate provider/unsafe name/reserved artifact/unsupported target/conflict rejection; and hidden mount, mutable/config shadow, selector, and service requirement rejection.

## Managed-resource discovery and changes

Profiles with any effective managed skill **or selected native job** also receive
`agentSkill:nix-managed-resource-changes` from the normal capability registry.
Its implicit root is derived from ordinary selection, never its own output.
The name is reserved; selecting it manually fails evaluation. Empty profiles and
job providers with no selected instances publish neither the skill nor inventory.

`$HERMES_HOME/managed-resources.json` is an owned symlink to an immutable schema-v2
manifest. The same JSON bytes are exposed as the management skill's
`references/managed-resources.json`. The terminal's existing private store includes
only its own manifest in addition to its helper runtime. It does not mount the
host store, add tools, or grant service/repository credentials. Metadata-only
external input paths deliberately discard Nix string context: provenance must
not pull the external source's closure into the terminal store.

The inventory records the profile, exact `skills` and `native_jobs`, provider
identity, repository-relative source paths and effective declaration files.
`managedSourceRepositories` maps known source roots to actionable repositories and
request routes. A reusable skill change belongs to hermes-config; a deployment
selection belongs to the deployment repository. External sources retain explicit
external provenance. A request naming a different repository from the selected
source is rejected; ambiguous declaration ownership requires an explicit owner.
Repository identities use `owner/name` and the implementing profile's existing
access. This metadata grants no additional credentials or writable checkouts.

Native job entries describe immutable launchers/contracts only. Cadence, private
delivery preferences and input/state ownership are unchanged; discovery does not
schedule jobs or authorize modifications. The helper accepts `--skill` or `--job`.

The optional standard-library helper validates inventory/request data and emits
arguments for native Kanban tools; it never makes API calls or writes a board.
Known active requests are reused; a worker already assigned the same change
implements it instead of recursively requeueing. Local, non-managed skills remain
locally maintainable. Guidance is not a model-compliance/security guarantee.

Activation validates all desired profile manifests and local-shadow collisions
before changing links. It replaces/removes only links in its exact generated
namespace, including retired profiles and removal of the last root. Unowned
files/links collide when enabling and are preserved when not managed. Local
skills, credentials, preferences and workspace data are not cleanup targets.
Native publication and the existing Docker config-hash lifecycle remove stale
skill visibility/mounts; no runtime skill copy or parallel registry is maintained.

Verification (no production daemon or profile state is used):

```console
nix build .#checks.x86_64-linux.hermes-managed-skills --no-link
nix build .#checks.x86_64-linux.hermes-managed-skills.dockerTest --no-link
```

The first check exercises skill-only, mixed, cron-only, empty and unselected-job
profiles, inline/external provenance, pinned native Hermes discovery/reference
reading, request validation/reuse and real-filesystem activation. The second
boots a disposable NixOS VM and exercises actual Docker mounts, symlink reads,
read-only writes, profile isolation and retirement. It is a separately invoked
runtime gate, not a heavyweight addition to every PR canary.

## Docker executable dependency contract

Managed skills and capability runtime packages use the same resolved profile
closure as skill and credential selection. There is no profile helper list or
mount opt-in. A skill that needs an executable package owns that dependency even
when selected independently of an external service.

- `/run/hermes-capabilities/<skill-name>/` contains the complete immutable skill
  tree, including imported modules, templates and assets. Build-time copying
  dereferences source symlinks and preserves executable bits.
- `/run/hermes-capabilities/bin/` contains the selected executable packages.
  Canonical skills use these absolute entrypoints, independent of working directory
  and shell startup files.
- A second read-only mount at `/nix/store` contains **only a materialized copy of
  that profile's runtime closure**, computed by `pkgs.closureInfo`. Its host source
  is a dedicated immutable derivation, never the host `/nix/store` directory.
  This preserves Nix binary loaders, RPATHs, Python imports and package data without
  exposing unrelated store contents. Nix deduplication can share identical files.
- Credentials retain their separate `/run/hermes-credentials/services` mount.
  Requests and mutable state remain in `/workspace` and the designated profile
  `$HERMES_HOME` locations. Helper publication takes neither as a build input.

Python CLI helpers are discovered from the complete selected skill trees and use
the common packaged Python interpreter. Their harmless `--help` startup probes
exercise imports in Docker. Other executable families belong in the existing
`provides.runtimePackages`, with their complete Nix dependencies and a harmless
`meta.mainProgram --help` contract. Unsupported loose executables fail validation;
missing Python imports fail readiness. There is no new per-skill helper registry.
Package tests retain responsibility for deeper behavioral and lazy-import checks.
No startup probe grants permission to call a production mutation.

Every gateway startup runs the probes in a fresh read-only Docker container using
its configured image, without credentials, network access, workspace copies or
profile state. Missing imports, broken symlinks, unsupported runtimes and failed
probes stop readiness. This validates helper startup; it does not claim that a
service endpoint, credentials or an entire workflow is ready. Service preflights
remain a separate read-only check.

Generated Docker configuration includes the immutable publications in its hash.
Updates and final-consumer removal therefore use the existing pre-start container
replacement, including profiles without credentials. Bind-backed workspace and
profile data survive; other profiles and unrelated containers are not selected.
Deploy only once affected profiles have no active work, as with existing sandbox
configuration changes. The operator owns deployment and production verification.

Terminal overrides are merged before managed mounts are appended and hashed,
including overrides in generic `settings.terminal`. User mounts use the existing
`docker_volumes` interface and may not overlap the helper or runtime namespace;
mount flags in `docker_extra_args` are rejected so they cannot bypass validation.
Failed Docker inspection or replacement stops gateway startup instead of recording
a successful transition with stale containers still present. The supported base
is the configured Docker terminal image. A custom image whose own executables
require a different `/nix/store` is incompatible with this isolated runtime mount
and must fail readiness rather than be treated as supported.

### Audited executable families

| Family | Sources and dependencies | Alakazam selection |
| --- | --- | --- |
| Mealie, Grocy | Complete skill trees; Python standard library, SSL and `fcntl` | chef |
| Wger | Complete skill tree; Python standard library, SSL and `fcntl` | nutritionist, fitness-coach |
| Miniflux | Complete skill tree; Python standard library and SSL | personal-assistant-sandro |
| Vikunja reviews | Packaged CLI, workflow-state import, preference and config assets | personal-assistant-sandro; independent skill supported |
| Nutrition | Packaged CLI, workflow-state import, preferences asset | nutritionist |
| Digest and jobs | Packaged CLIs, workflow-state import, contract assets | personal-assistant-sandro |
| Repository sync | Packaged CLI, registry and schedule assets, Git and OpenSSH closures | system-admin, personal-assistant-sandro |
| Nix-config maintenance | Packaged CLIs, workflow-state import, Git closure | system-admin |
| Forgejo event journal, Prometheus reconciler | Packaged CLIs and workflow-state import | system-admin |

`personal-assistant-melanie` and `software-developer` currently select no executable
helpers; their publications were checked for the absence of these families.

Native scheduled-job wrappers remain host-executed by Hermes and keep their
existing designated `$HERMES_HOME/scripts` publication. Their selected runtime
packages are also available in the terminal. This change does not migrate legacy
local skills or implement additional workflow automation.

### Regression commands

`hermes-sandbox-helpers` builds a focused, credential-free Docker regression.
Run it on an authorized Linux Docker host:

```sh
bundle=$(nix build .#checks.x86_64-linux.hermes-sandbox-helpers --no-link --print-out-paths)
docker load < "$bundle/image.tar.gz"
"$bundle/run"
```

It reproduces the missing host path, then proves actual helper startup/imports,
read-only publication, profile isolation and a fresh writable workspace. Existing
capability-graph checks cover dependency selection, final-reference removal,
configuration overrides and reserved-namespace rejection. Production persistent
container replacement remains an operator acceptance check. Neither test calls
production APIs.
