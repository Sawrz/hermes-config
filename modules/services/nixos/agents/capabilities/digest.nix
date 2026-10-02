{
  lib,
  pkgs,
  ...
}:
let
  capabilityLib = import ./lib.nix { inherit lib; };
  workflowStatePackage =
    pkgs.hermes-workflow-state
      or (pkgs.callPackage ../../../../../pkgs/common/hermes-workflow-state { });
  digestWorkflowPackage =
    pkgs.hermes-digest-workflow or (pkgs.callPackage ../../../../../pkgs/common/hermes-digest-workflow {
      hermes-workflow-state = workflowStatePackage;
    });

  digestEditorial = capabilityLib.mkAgentSkill "digest-editorial" {
    provides.skills.digest-editorial = {
      category = "productivity";
      source = ../skills/digest-editorial;
    };
  };

  digestRead = capabilityLib.mkExternalServiceCapability "miniflux" "digest-read" {
    requires = [ digestEditorial.id ];
  };

  digest = capabilityLib.mkServiceIntegration "digest" {
    safety = {
      credentialRequirements = [
        {
          service = "miniflux";
          field = "credential";
        }
      ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/job-config/digest";
          access = "read";
        }
        {
          path = "$HERMES_HOME/state/digest";
          access = "read-write";
        }
      ];
      writeScope = "local-state";
      confirmation = "policy-preauthorized";
      verification = "durable-readback-required";
    };
    requires = [ digestRead.id ];
    requiredExternalServices = [ "miniflux" ];
    requiredHermesCapabilities = [
      "cron"
      "gateway-delivery"
    ];
    provides.runtimePackages = [ digestWorkflowPackage ];
    provides.nativeJobs.morning-news-summary = {
      noAgent = false;
      skills = [ "digest-editorial" ];
      deliverPrefix = "telegram:";
      script = pkgs.writeShellScript "hermes-digest-morning-news-summary" ''
        set -eu
        route="$(hermes-native-cron-reconciler \
          --contract "$HERMES_NATIVE_CRON_CONTRACT" \
          --policy "$HERMES_NATIVE_CRON_POLICY" \
          --hermes "$HERMES_NATIVE_CRON_CLI" \
          --check --print-deliver morning-news-summary)"
        exec ${digestWorkflowPackage}/bin/hermes-digest-workflow live-gate \
          --state-dir "''${HERMES_HOME}/state/digest" \
          --preferences "''${HERMES_HOME}/job-config/digest/preferences.json" \
          --route "$route"
      '';
    };
    provides.managedArtifacts = [
      "cron:digest-edition-template"
      "file:digest-preferences-contract"
      "file:digest-delivery-route-contract"
    ];
  };
in
{
  config = {
    custom.services.hermes.capabilityPackages = {
      ${digestEditorial.id} = digestEditorial;
      ${digestRead.id} = digestRead;
      ${digest.id} = digest;
    };
  };
}
