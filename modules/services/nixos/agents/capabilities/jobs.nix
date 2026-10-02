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
  jobWorkflowPackage =
    pkgs.hermes-job-workflow or (pkgs.callPackage ../../../../../pkgs/common/hermes-job-workflow {
      hermes-workflow-state = workflowStatePackage;
    });

  jobRanking = capabilityLib.mkAgentSkill "job-ranking" {
    provides.skills.job-ranking = {
      category = "productivity";
      source = ../skills/job-ranking;
    };
  };

  jobFeedRead = capabilityLib.mkExternalServiceCapability "miniflux" "job-feed-read" {
    requires = [ jobRanking.id ];
  };

  jobDiscovery = capabilityLib.mkServiceIntegration "job-discovery" {
    safety = {
      credentialRequirements = [
        {
          service = "miniflux";
          field = "credential";
        }
      ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/job-config/job-discovery";
          access = "read";
        }
        {
          path = "$HERMES_HOME/state/job-discovery";
          access = "read-write";
        }
      ];
      writeScope = "local-state";
      confirmation = "policy-preauthorized";
      verification = "durable-readback-required";
    };
    requires = [ jobFeedRead.id ];
    requiredExternalServices = [ "miniflux" ];
    requiredHermesCapabilities = [ "cron" ];
    provides.runtimePackages = [ jobWorkflowPackage ];
    provides.nativeJobs = {
      job-passive-collection = {
        noAgent = true;
        skills = [ ];
        deliver = "local";
        script = pkgs.writeShellScript "hermes-job-passive-collection" ''
          set -eu
          entries="$(${pkgs.coreutils}/bin/mktemp)"
          trap '${pkgs.coreutils}/bin/rm -f "$entries"' EXIT INT TERM
          ${pkgs.python3}/bin/python3 ${../skills/miniflux-source/scripts/miniflux_source.py} \
            entries --config "''${HERMES_HOME}/job-config/job-discovery/sources.json" >"$entries"
          ${jobWorkflowPackage}/bin/hermes-job-workflow miniflux-passive \
            --state-dir "''${HERMES_HOME}/state/job-discovery/passive" \
            --preferences "''${HERMES_HOME}/job-config/job-discovery/preferences.json" \
            --entries "$entries" \
            --now "$(${pkgs.coreutils}/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
        '';
      };
      job-active-research = {
        noAgent = false;
        skills = [ "job-ranking" ];
        deliverPrefix = "telegram:";
        script = pkgs.writeShellScript "hermes-job-active-research" ''
          set -eu
          ${jobWorkflowPackage}/bin/hermes-job-workflow miniflux-active-gate \
            --state-dir "''${HERMES_HOME}/state/job-discovery/active" \
            --preferences "''${HERMES_HOME}/job-config/job-discovery/preferences.json" \
            --passive-state-dir "''${HERMES_HOME}/state/job-discovery/passive" \
            --now "$(${pkgs.coreutils}/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
        '';
      };
    };
    provides.managedArtifacts = [
      "cron:job-passive-collection-template"
      "cron:job-active-research-template"
      "file:job-discovery-preferences-contract"
      "file:job-discovery-public-state"
    ];
  };
in
{
  config = {
    custom.services.hermes.capabilityPackages = {
      ${jobRanking.id} = jobRanking;
      ${jobFeedRead.id} = jobFeedRead;
      ${jobDiscovery.id} = jobDiscovery;
    };
  };
}
