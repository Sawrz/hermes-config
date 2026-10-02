{ inputs }:
final: prev:
let
  lib = prev.lib;
  pkgs-unstable = import inputs.nixpkgs-unstable { system = prev.stdenv.hostPlatform.system; };
in
{
  # Hermes needs fixed SQLite; use unstable's matching, cached Python too.
  # Its nested callPackage must see that interpreter, not stable Python.
  hermes-agent =
    let
      runtime = lib.makeScope prev.newScope (_: {
        inherit (pkgs-unstable) sqlite python312;
      });
    in
    assert lib.assertMsg (lib.versionAtLeast runtime.sqlite.version "3.51.3")
      "Hermes requires a nixpkgs-unstable pin with SQLite >= 3.51.3 for the WAL-reset fix.";
    inputs.hermes-agent.packages.${prev.stdenv.hostPlatform.system}.default.override {
      inherit (runtime) python312 callPackage;
    };

  forgejo-event-journal = prev.callPackage ./common/forgejo-event-journal {
    inherit (final) hermes-workflow-state;
  };
  forgejo-kanban-workflow = prev.callPackage ./common/forgejo-kanban-workflow {
    inherit (final) forgejo-event-journal hermes-workflow-state;
  };
  hermes-prometheus-reconciler = prev.callPackage ./common/hermes-prometheus-reconciler {
    inherit (final) hermes-workflow-state;
  };
  hermes-digest-workflow = prev.callPackage ./common/hermes-digest-workflow { };
  hermes-job-workflow = prev.callPackage ./common/hermes-job-workflow { };
  hermes-repository-sync = prev.callPackage ./common/hermes-repository-sync { };
  hermes-nix-maintenance = prev.callPackage ./common/hermes-nix-maintenance {
    inherit (final) hermes-workflow-state forgejo-kanban-workflow hermes-repository-sync;
  };
  hermes-workflow-state = prev.callPackage ./common/hermes-workflow-state { };
  kanban-vikunja-projection = prev.callPackage ./common/kanban-vikunja-projection {
    inherit (final) hermes-workflow-state forgejo-kanban-workflow;
  };
  hermes-vikunja-review = prev.callPackage ./common/hermes-vikunja-review {
    inherit (final) hermes-workflow-state;
  };
  hermes-nutrition-workflows = prev.callPackage ./common/hermes-nutrition-workflows {
    inherit (final) hermes-workflow-state;
  };
  hermes-native-cron-reconciler = prev.callPackage ./common/hermes-native-cron-reconciler { };
}
