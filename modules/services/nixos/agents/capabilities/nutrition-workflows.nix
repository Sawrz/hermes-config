{ lib, pkgs, ... }:
let
  capabilityLib = import ./lib.nix { inherit lib; };
  workflowStatePackage =
    pkgs.hermes-workflow-state
      or (pkgs.callPackage ../../../../../pkgs/common/hermes-workflow-state { });
  nutritionWorkflowsPackage =
    pkgs.hermes-nutrition-workflows
      or (pkgs.callPackage ../../../../../pkgs/common/hermes-nutrition-workflows {
        hermes-workflow-state = workflowStatePackage;
      });
  nutritionJob = name: {
    noAgent = true;
    skills = [ ];
    deliverPrefix = "telegram:";
    script = pkgs.writeShellScript "hermes-nutrition-${name}" ''
      set -eu
      exec ${nutritionWorkflowsPackage}/bin/hermes-nutrition-workflows \
        --preferences "''${HERMES_HOME}/job-config/nutrition/${name}/preferences.json" \
        --private-overlay "''${HERMES_HOME}/job-config/nutrition/${name}/private-overlay.json" \
        --state-dir "''${HERMES_HOME}/state/nutrition/${name}"
    '';
  };

  weightCheckIn = capabilityLib.mkAgentSkill "weight-check-in-and-trend" {
    safety = {
      credentialRequirements = [ ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/state/nutrition/weight-check-in-and-trend";
          access = "read-write";
        }
      ];
      writeScope = "local-state";
      confirmation = "policy-preauthorized";
      verification = "durable-readback-required";
    };
    provides.runtimePackages = [ nutritionWorkflowsPackage ];
    provides.nativeJobs.weight-check-in-and-trend = nutritionJob "weight-check-in-and-trend";
    provides.skills.weight-check-in-and-trend = {
      category = "health";
      source = ../skills/weight-check-in-and-trend;
    };
  };

  dailyIntake = capabilityLib.mkAgentSkill "daily-intake-closeout" {
    safety = {
      credentialRequirements = [ ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/state/nutrition/daily-intake-closeout";
          access = "read-write";
        }
      ];
      writeScope = "local-state";
      confirmation = "policy-preauthorized";
      verification = "durable-readback-required";
    };
    provides.runtimePackages = [ nutritionWorkflowsPackage ];
    provides.nativeJobs.daily-intake-closeout = nutritionJob "daily-intake-closeout";
    provides.skills.daily-intake-closeout = {
      category = "health";
      source = ../skills/daily-intake-closeout;
    };
  };

  householdPortions = capabilityLib.mkAgentSkill "household-portion-planning" {
    safety = {
      credentialRequirements = [ ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/state/nutrition/household-portion-planning";
          access = "read-write";
        }
      ];
      writeScope = "local-state";
      confirmation = "policy-preauthorized";
      verification = "durable-readback-required";
    };
    provides.runtimePackages = [ nutritionWorkflowsPackage ];
    provides.nativeJobs.household-portion-planning = nutritionJob "household-portion-planning";
    provides.skills.household-portion-planning = {
      category = "health";
      source = ../skills/household-portion-planning;
    };
  };
in
{
  config.custom.services.hermes.capabilityPackages = {
    ${weightCheckIn.id} = weightCheckIn;
    ${dailyIntake.id} = dailyIntake;
    ${householdPortions.id} = householdPortions;
  };
}
