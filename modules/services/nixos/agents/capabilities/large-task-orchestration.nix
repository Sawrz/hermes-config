{ lib, ... }:
let
  capabilityLib = import ./lib.nix { inherit lib; };

  largeTaskOrchestration = capabilityLib.mkAgentSkill "hermes-kanban-large-task-orchestration" {
    requiredHermesCapabilities = [
      "kanban"
      "toolsets"
    ];
    provides = {
      skills.hermes-kanban-large-task-orchestration = {
        category = "repo-managed";
        source = ../skills/hermes-kanban-large-task-orchestration;
      };
      toolsets = [ "kanban" ];
    };
  };
in
{
  config.custom.services.hermes.capabilityPackages.${largeTaskOrchestration.id} =
    largeTaskOrchestration;
}
