{ lib, pkgs, ... }:
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

  maintenanceRuntime = capabilityLib.mkServiceIntegration "nix-maintenance-runtime" {
    safety = {
      credentialRequirements = [ ];
      pathRequirements = [ ];
      writeScope = "none";
      confirmation = "not-required";
      verification = "not-required";
    };
    provides.runtimePackages = [ maintenancePackage ];
    requires = [ "agentSkill:nix-maintenance-protocol" ];
    provides.managedArtifacts = [
      "package:hermes-nix-maintenance"
    ];
  };
in
{
  config.custom.services.hermes.capabilityPackages = {
    "${maintenanceRuntime.id}" = maintenanceRuntime;
  };
}
