{ lib, pkgs, ... }:
let
  capabilityLib = import ./lib.nix { inherit lib; };
  repositorySyncPackage =
    pkgs.hermes-repository-sync
      or (pkgs.callPackage ../../../../../pkgs/common/hermes-repository-sync { });
  repositorySyncJob = name: {
    noAgent = true;
    skills = [ ];
    deliver = "local";
    script = pkgs.writeShellScript "hermes-repository-sync-${name}" ''
      set -eu
      exec ${repositorySyncPackage}/bin/hermes-repository-sync \
        --registry "''${HERMES_HOME}/job-config/repository-sync/registry.json" \
        --job ${lib.escapeShellArg name} \
        --state-dir "''${HERMES_HOME}/state/repository-sync"
    '';
  };

  repositorySync = capabilityLib.mkServiceIntegration "repository-sync" {
    allowedSettings = [
      "repositories"
      "sshHost"
      "sshPort"
      "knownHosts"
    ];
    safety = {
      credentialRequirements = [ ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/job-config/repository-sync";
          access = "read";
        }
        {
          path = "$HERMES_HOME/state/repository-sync";
          access = "read-write";
        }
      ];
      writeScope = "local-state";
      confirmation = "policy-preauthorized";
      verification = "durable-readback-required";
    };
    requiredHermesCapabilities = [ "cron" ];
    provides.runtimePackages = [ repositorySyncPackage ];
    provides.nativeJobs = {
      cv-repo-pull = repositorySyncJob "cv-repo-pull";
      nix-config-pull = repositorySyncJob "nix-config-pull";
      omanix-pull = repositorySyncJob "omanix-pull";
      nix-containers-pull = repositorySyncJob "nix-containers-pull";
      family-controls-pull = repositorySyncJob "family-controls-pull";
      hermes-config-pull = repositorySyncJob "hermes-config-pull";
      obsidian-main-vault-pull = repositorySyncJob "obsidian-main-vault-pull";
    };
    provides.managedArtifacts = [
      "cron:cv-repo-pull"
      "cron:nix-config-pull"
      "cron:obsidian-main-vault-pull"
      "manifest:repository-sync/schedule-templates"
    ];
  };
in
{
  config.custom.services.hermes.capabilityPackages.${repositorySync.id} = repositorySync;
}
