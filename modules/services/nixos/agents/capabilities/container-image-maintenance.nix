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

  containerImageMaintenance = capabilityLib.mkServiceIntegration "container-image-maintenance" {
    allowedSettings = [
      "board"
    ];
    safety = {
      credentialRequirements = [ ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/state/container-image-maintenance/update-intent";
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
      "serviceIntegration:forgejo-to-kanban"
    ];
    provides.managedArtifacts = [
      "file:container-image-maintenance/update-intent"
      "file:container-image-maintenance/inventory"
    ];
    provides.nativeJobs.container-image-monthly-inventory-refresh = {
      noAgent = true;
      skills = [ ];

      deliver = "local";
      script = pkgs.writeShellScript "hermes-container-image-inventory-refresh" ''
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
          container-refresh \
          --registry "''${HERMES_HOME}/job-config/repository-sync/registry.json" \
          --feeds "''${HERMES_HOME}/job-config/container-image-maintenance/feeds.json" \
          --state-dir "''${HERMES_HOME}/state/container-image-maintenance/inventory"
      '';
    };
    provides.nativeJobs.container-image-monthly-workflow-curation-apply = {
      noAgent = true;
      skills = [ ];

      deliver = "local";
      script = pkgs.writeShellScript "hermes-container-image-monthly-curation" ''
        set -eu
        ${maintenancePackage}/bin/hermes-nix-maintenance \
          --instance-config ${
            if
              config.custom.services.hermes.repositoryInstanceFiles.${config.custom.services.hermes.maintenanceRepositoryInstance}
              == null
            then
              "/invalid-instance"
            else
              config.custom.services.hermes.repositoryInstanceFiles.${config.custom.services.hermes.maintenanceRepositoryInstance}
          } \
          container-curation-apply \
          --snapshot-state-dir "''${HERMES_HOME}/state/container-image-maintenance/inventory" \
          --state-dir "''${HERMES_HOME}/state/container-image-maintenance/update-intent" \
          >/dev/null
      '';
    };
    provides.nativeJobs.container-image-monthly-update-issue-gate = {
      noAgent = true;
      skills = [ ];
      deliverPrefix = "telegram:";
      script = pkgs.writeShellScript "hermes-container-update-issue-delivery-gate" ''
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
          container-request-delivery \
          --state-dir "''${HERMES_HOME}/state/container-image-maintenance/update-intent"
      '';
    };
  };
in
{
  config.custom.services.hermes.capabilityPackages = {
    "${containerImageMaintenance.id}" = containerImageMaintenance;
  };
}
