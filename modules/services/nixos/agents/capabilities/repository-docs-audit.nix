{
  config,
  lib,
  pkgs,
  ...
}:
let
  capabilityLib = import ./lib.nix { inherit lib; };
  workflowStatePackage =
    pkgs.hermes-workflow-state
      or (pkgs.callPackage ../../../../../pkgs/common/hermes-workflow-state { });
  maintenancePackage =
    pkgs.hermes-nix-maintenance or (pkgs.callPackage ../../../../../pkgs/common/hermes-nix-maintenance {
      hermes-workflow-state = workflowStatePackage;
      hermes-repository-sync =
        pkgs.hermes-repository-sync
          or (pkgs.callPackage ../../../../../pkgs/common/hermes-repository-sync { });
      forgejo-kanban-workflow =
        pkgs.forgejo-kanban-workflow or (pkgs.callPackage ../../../../../pkgs/common/forgejo-kanban-workflow
          {
            hermes-workflow-state = workflowStatePackage;
            forgejo-event-journal =
              pkgs.forgejo-event-journal or (pkgs.callPackage ../../../../../pkgs/common/forgejo-event-journal {
                hermes-workflow-state = workflowStatePackage;
              });
          }
        );
    });

  nixConfigDocsReview = capabilityLib.mkServiceIntegration "repository-docs-audit" {
    allowedSettings = [
      "board"
    ];
    safety = {
      credentialRequirements = [ ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/state/nix-config-docs-review";
          access = "read-write";
        }
      ];
      writeScope = "local-state";
      confirmation = "policy-preauthorized";
      verification = "durable-readback-required";
    };
    requires = [
      "serviceIntegration:nix-maintenance-runtime"
      "serviceIntegration:repository-sync"
    ];
    requiredHermesCapabilities = [
      "kanban"
      "cron"
    ];
    provides.managedArtifacts = [
      "file:repository-docs-audit/inventory"
      "ci:repository/forgejo-wiki-publication"
    ];
    provides.nativeJobs.nix-config-monthly-docs-review-gate = {
      noAgent = true;
      skills = [ ];
      deliver = "local";
      script = pkgs.writeShellScript "hermes-nix-config-monthly-docs-review-gate" ''
        set -eu
        exec ${maintenancePackage}/bin/hermes-nix-maintenance \
          --instance-config ${
            if
              config.custom.services.hermes.repositoryInstanceFiles.${config.custom.services.hermes.maintenanceRepositoryInstance}
              == null
            then
              "/invalid-instance"
            else
              config.custom.services.hermes.repositoryInstanceFiles.${config.custom.services.hermes.maintenanceRepositoryInstance}
          } \
          docs-gate \
          --registry "''${HERMES_HOME}/job-config/repository-sync/registry.json" \
          --hermes "$(command -v hermes)" \
          --board ${
            lib.escapeShellArg (
              config.custom.services.hermes.repositoryInstances.${config.custom.services.hermes.maintenanceRepositoryInstance}.board
                or ""
            )
          } \
          --state-dir "''${HERMES_HOME}/state/nix-config-docs-review/commit-audit"
      '';
    };
  };
in
{
  config.custom.services.hermes.capabilityPackages = {
    "${nixConfigDocsReview.id}" = nixConfigDocsReview;
  };
}
