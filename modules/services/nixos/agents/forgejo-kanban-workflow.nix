{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkIf mkMerge;
  hermesCfg = config.custom.services.hermes;
  capabilityLib = import ./capabilities/lib.nix { inherit lib; };

  forgejoWorkflow = capabilityLib.mkExternalServiceCapability "forgejo" "workflow" {
    requiredHermesCapabilities = [ "toolsets" ];
    provides.runtimePackages = [ pkgs.hermes-workflow-state ];
    provides.managedArtifacts = [ "file:forgejo/workflow-contract" ];
  };
  forgejoToKanban = capabilityLib.mkServiceIntegration "forgejo-to-kanban" {
    safety = {
      credentialRequirements = [
        {
          service = "forgejo";
          field = "credential";
        }
      ];
      pathRequirements = [
        {
          path = "/var/lib/hermes-forgejo-kanban-workflow";
          access = "read-write";
        }
      ];
      writeScope = "external-system";
      confirmation = "policy-preauthorized";
      verification = "external-readback-required";
    };
    allowedSettings = [
      "board"
      "reconcileInterval"
    ];
    requires = [
      forgejoWorkflow.id
      "serviceIntegration:forgejo-event-ingest"
    ];
    requiredExternalServices = [ "forgejo" ];
    requiredHermesCapabilities = [
      "kanban"
      "dispatcher"
    ];
    provides.managedArtifacts = [
      "unit:hermes-forgejo-kanban-reconcile"
      "timer:hermes-forgejo-kanban-reconcile"
    ];
  };

  profiles = lib.mapAttrs (
    name: profile: lib.recursiveUpdate profile (config.services.hermes-agent.profiles.${name} or { })
  ) hermesCfg.profiles;
  integrationSelected =
    profile: name:
    let
      selection = profile.serviceIntegrations.${name} or false;
    in
    profile.enable && (if builtins.isBool selection then selection else selection.enable);
  integrationSettings =
    profile: name:
    let
      selection = profile.serviceIntegrations.${name} or false;
    in
    if builtins.isBool selection then { } else selection.settings;
  selectedProfiles = lib.filterAttrs (
    _name: profile: integrationSelected profile "forgejo-to-kanban"
  ) profiles;
  selectedNames = builtins.attrNames selectedProfiles;
  ownerProfile =
    if builtins.length selectedNames == 1 then
      selectedProfiles.${builtins.head selectedNames}
    else
      null;
  settings =
    if ownerProfile == null then { } else integrationSettings ownerProfile "forgejo-to-kanban";
  forgejoService = if ownerProfile == null then { } else ownerProfile.externalServices.forgejo or { };
  endpoint = forgejoService.endpoint or "";
  credentialFile = forgejoService.credentialFiles.credential or null;
  credentialPath = if credentialFile == null then "/invalid" else toString credentialFile;
  board = settings.board or "";
  reconcileInterval = settings.reconcileInterval or "1m";
  eventIngestSelected =
    ownerProfile != null && integrationSelected ownerProfile "forgejo-event-ingest";
  journalStateDirectory = "hermes-forgejo-event-journal";
  stateDirectory = "hermes-forgejo-kanban-workflow";
  journalRoot = "/var/lib/${journalStateDirectory}";
  stateRoot = "/var/lib/${stateDirectory}";
  hermesHome = "${hermesCfg.stateDir}/.hermes";
  containerSelected =
    ownerProfile != null && integrationSelected ownerProfile "container-image-maintenance";
  containerIntentRoot = "${hermesHome}/profiles/${
    if ownerProfile == null then "invalid" else builtins.head selectedNames
  }/state/container-image-maintenance/update-intent";
in
{
  imports = [ ./repository-instance.nix ];
  config = mkMerge [
    {
      custom.services.hermes.capabilityPackages = {
        ${forgejoWorkflow.id} = forgejoWorkflow;
        ${forgejoToKanban.id} = forgejoToKanban;
      };
      assertions = [
        {
          assertion = lib.all (
            profile:
            !(integrationSelected profile "container-image-maintenance")
            || integrationSelected profile "forgejo-to-kanban"
          ) (builtins.attrValues profiles);
          message = "Container maintenance requires its profile to select the singleton forgejo-to-kanban owner for deterministic issue delivery.";
        }
        {
          assertion =
            builtins.length selectedNames <= 1
            && (selectedNames == [ ] || selectedNames == [ hermesCfg.repositoryAuthority.ownerProfile ]);
          message = "Forgejo-to-Kanban selected by an enabled Hermes profile must have exactly one owner; selected by: ${lib.concatStringsSep ", " selectedNames}.";
        }
      ];
    }
    (mkIf (selectedNames != [ ]) {
      # Reuse the existing workflow owner/board; resource ownership discovery
      # itself is derived per profile and needs no extra host opt-in.
      assertions = [
        {
          assertion = hermesCfg.enable;
          message = "Forgejo-to-Kanban reconciliation requires custom.services.hermes.enable.";
        }
        {
          assertion = eventIngestSelected;
          message = "Forgejo-to-Kanban reconciliation requires the owning profile to select forgejo-event-ingest.";
        }
        {
          assertion = endpoint != "";
          message = "Forgejo-to-Kanban reconciliation requires the owning profile's externalServices.forgejo.endpoint.";
        }
        {
          assertion = credentialFile != null;
          message = "Forgejo-to-Kanban reconciliation requires the owning profile's externalServices.forgejo.credentialFiles.credential.";
        }
        {
          assertion = builtins.all (name: builtins.elem name forgejoToKanban.allowedSettings) (
            builtins.attrNames settings
          );
          message = "forgejo-to-kanban settings support only board and reconcileInterval.";
        }
        {
          assertion = builtins.match "^[a-z0-9][a-z0-9_-]{0,63}$" board != null;
          message = "forgejo-to-kanban board must be a safe native board slug.";
        }
        {
          assertion = builtins.match "^[1-9][0-9]*(s|m|h|d)$" reconcileInterval != null;
          message = "forgejo-to-kanban reconcileInterval must be a positive systemd duration in s, m, h, or d.";
        }
      ];

      systemd = {
        services.hermes-forgejo-kanban-reconcile = {
          description = "Deterministically reconcile Forgejo evidence into native Hermes Kanban";
          requires = [ "hermes-agent.service" ];
          after = [
            "hermes-agent.service"
            "hermes-forgejo-event-poll.service"
          ];
          wants = [ "network-online.target" ];
          serviceConfig = {
            Type = "oneshot";
            User = hermesCfg.user;
            Group = hermesCfg.group;
            UMask = "0027";
            StateDirectory = stateDirectory;
            StateDirectoryMode = "0750";
            NoNewPrivileges = true;
            PrivateTmp = true;
            PrivateDevices = true;
            ProtectSystem = "strict";
            ProtectHome = true;
            ProtectKernelTunables = true;
            ProtectKernelModules = true;
            ProtectControlGroups = true;
            RestrictSUIDSGID = true;
            LockPersonality = true;
            RestrictRealtime = true;
            RestrictAddressFamilies = [
              "AF_INET"
              "AF_INET6"
            ];
            CapabilityBoundingSet = "";
            AmbientCapabilities = "";
            SystemCallArchitectures = "native";
            ReadOnlyPaths = [ credentialPath ];
            ReadWritePaths = [
              journalRoot
              hermesHome
            ];
            MemoryMax = "256M";
            TasksMax = 64;
            # Native cron requests publication in the existing profile intent ledger.
            # F11 retains its host credential boundary; cron never receives secrets.
            ExecStartPost = lib.optionals containerSelected [
              (lib.escapeShellArgs [
                "${pkgs.hermes-nix-maintenance}/bin/hermes-nix-maintenance"
                "--instance-config"
                (toString hermesCfg.repositoryInstanceFiles.${hermesCfg.maintenanceRepositoryInstance})
                "container-deliver"
                "--require-request"
                "--state-dir"
                containerIntentRoot
                "--endpoint"
                endpoint
                "--credential-file"
                credentialPath
              ])
            ];
            ExecStart = pkgs.writeShellScript "hermes-repository-reconcile" (
              "set -eu\n"
              + lib.concatStringsSep "\n" (
                lib.mapAttrsToList (
                  name: file:
                  lib.escapeShellArgs [
                    "${pkgs.forgejo-kanban-workflow}/bin/forgejo-kanban-workflow"
                    "--instance-config"
                    (toString file)
                    "--endpoint"
                    endpoint
                    "--credential-file"
                    credentialPath
                    "reconcile"
                    "--hermes"
                    "${config.services.hermes-agent.package}/bin/hermes"
                    "--hermes-home"
                    hermesHome
                    "--board"
                    board
                    "--journal"
                    "${pkgs.forgejo-event-journal}/bin/forgejo-event-journal"
                    "--journal-state-root"
                    "${journalRoot}${hermesCfg.repositoryStateSubdirectories.${name}}"
                    "--state-root"
                    "${stateRoot}${hermesCfg.repositoryStateSubdirectories.${name}}"
                  ]
                ) hermesCfg.repositoryInstanceFiles
              )
            );
          };
        };

        timers.hermes-forgejo-kanban-reconcile = {
          description = "Schedule deterministic Forgejo-to-Kanban reconciliation";
          wantedBy = [ "timers.target" ];
          timerConfig = {
            OnBootSec = "3m";
            OnUnitActiveSec = reconcileInterval;
            AccuracySec = "15s";
            RandomizedDelaySec = "15s";
            Persistent = true;
            Unit = "hermes-forgejo-kanban-reconcile.service";
          };
        };
      };
    })
  ];
}
