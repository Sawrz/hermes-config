{ lib, pkgs, ... }:
let
  capabilityLib = import ./lib.nix { inherit lib; };
  workflowStatePackage =
    pkgs.hermes-workflow-state
      or (pkgs.callPackage ../../../../../pkgs/common/hermes-workflow-state { });
  vikunjaReviewPackage =
    pkgs.hermes-vikunja-review or (pkgs.callPackage ../../../../../pkgs/common/hermes-vikunja-review {
      hermes-workflow-state = workflowStatePackage;
    });

  vikunjaTaskReviews = capabilityLib.mkAgentSkill "vikunja-task-reviews" {
    provides.runtimePackages = [ vikunjaReviewPackage ];
    provides.skills.vikunja-task-reviews = {
      category = "productivity";
      source = ../skills/vikunja-task-reviews;
    };
  };

  vikunjaReviews = capabilityLib.mkExternalServiceCapability "vikunja" "task-reviews" {
    safety = {
      credentialRequirements = [
        {
          service = "vikunja";
          field = "credential";
        }
      ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/state/vikunja-review";
          access = "read-write";
        }
      ];
      writeScope = "local-state";
      confirmation = "policy-preauthorized";
      verification = "durable-readback-required";
    };
    requires = [ vikunjaTaskReviews.id ];
    requiredHermesCapabilities = [
      "cron"
      "gateway-delivery"
    ];
    provides.nativeJobs = {
      vikunja-daily-review = {
        noAgent = true;
        skills = [ ];
        deliverPrefix = "telegram:";
        script = pkgs.writeShellScript "hermes-vikunja-daily-review" ''
          set -eu
          exec ${vikunjaReviewPackage}/bin/hermes-vikunja-review run --kind daily \
            --config "''${HERMES_HOME}/job-config/vikunja-review/config.json" \
            --preferences "''${HERMES_HOME}/job-config/vikunja-review/preferences.json" \
            --state-dir "''${HERMES_HOME}/state/vikunja-review/daily"
        '';
      };
      vikunja-weekly-review = {
        noAgent = true;
        skills = [ ];
        deliverPrefix = "telegram:";
        script = pkgs.writeShellScript "hermes-vikunja-weekly-review" ''
          set -eu
          exec ${vikunjaReviewPackage}/bin/hermes-vikunja-review run --kind weekly \
            --config "''${HERMES_HOME}/job-config/vikunja-review/config.json" \
            --preferences "''${HERMES_HOME}/job-config/vikunja-review/preferences.json" \
            --state-dir "''${HERMES_HOME}/state/vikunja-review/weekly"
        '';
      };
    };
  };
in
{
  config.custom.services.hermes.capabilityPackages = {
    ${vikunjaTaskReviews.id} = vikunjaTaskReviews;
    ${vikunjaReviews.id} = vikunjaReviews;
  };
}
