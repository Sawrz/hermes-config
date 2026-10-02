{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkIf mkMerge;
  capabilityLib = import ./capabilities/lib.nix { inherit lib; };
  forgejoEventIngest = capabilityLib.mkServiceIntegration "forgejo-event-ingest" {
    safety = {
      credentialRequirements = [
        {
          service = "forgejo";
          field = "credential";
        }
      ];
      pathRequirements = [
        {
          path = "/var/lib/hermes-forgejo-event-journal";
          access = "read-write";
        }
      ];
      writeScope = "local-state";
      confirmation = "policy-preauthorized";
      verification = "durable-readback-required";
    };
    allowedSettings = [
      "instance"
      "board"
      "initialSince"
      "pollInterval"
    ];
    requires = [ "externalService:forgejo:workflow" ];
    requiredExternalServices = [ "forgejo" ];
    provides.runtimePackages = [ pkgs.forgejo-event-journal ];
    provides.managedArtifacts = [
      "package:forgejo-event-journal"
      "unit:hermes-forgejo-event-poll"
      "timer:hermes-forgejo-event-poll"
    ];
  };
  profiles = lib.mapAttrs (
    name: profile: lib.recursiveUpdate profile (config.services.hermes-agent.profiles.${name} or { })
  ) config.custom.services.hermes.profiles;
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
    _name: profile: integrationSelected profile "forgejo-event-ingest"
  ) profiles;
  selectedNames = builtins.attrNames selectedProfiles;
  ownerProfile =
    if builtins.length selectedNames == 1 then
      selectedProfiles.${builtins.head selectedNames}
    else
      null;
  settings =
    if ownerProfile == null then { } else integrationSettings ownerProfile "forgejo-event-ingest";
  forgejoService = if ownerProfile == null then { } else ownerProfile.externalServices.forgejo or { };
  endpoint = forgejoService.endpoint or "";
  credentialFile = forgejoService.credentialFiles.credential or null;
  credentialPath = if credentialFile == null then "/invalid" else toString credentialFile;
  stateDirectory = "hermes-forgejo-event-journal";
  stateRoot = "/var/lib/${stateDirectory}";
  pollInterval = settings.pollInterval or "5m";
  initialSince = settings.initialSince or "service-start";
  pollStart = pkgs.writeShellScript "hermes-forgejo-event-poll" (
    "set -eu\n"
    + lib.concatStringsSep "\n" (
      lib.mapAttrsToList (
        name: file:
        lib.escapeShellArgs [
          "${pkgs.forgejo-event-journal}/bin/forgejo-event-journal"
          "--instance-config"
          (toString file)
          "poll"
          "--endpoint"
          endpoint
          "--credential-file"
          credentialPath
          "--state-root"
          "${stateRoot}${config.custom.services.hermes.repositoryStateSubdirectories.${name}}"
          "--initial-since"
          initialSince
        ]
      ) config.custom.services.hermes.repositoryInstanceFiles
    )
  );

  commonHardening = {
    User = config.custom.services.hermes.user;
    Group = config.custom.services.hermes.group;
    UMask = "0027";
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
    StateDirectory = stateDirectory;
    StateDirectoryMode = "0750";
  };
in
{
  imports = [ ./repository-instance.nix ];
  config = mkMerge [
    {
      custom.services.hermes.capabilityPackages.${forgejoEventIngest.id} = forgejoEventIngest;
      assertions = [
        {
          assertion = builtins.length selectedNames <= 1;
          message = "Forgejo event ingest selected by an enabled Hermes profile must have exactly one owner; selected by: ${lib.concatStringsSep ", " selectedNames}.";
        }
      ];
    }
    (mkIf (selectedNames != [ ]) {
      assertions = [
        {
          assertion = config.custom.services.hermes.enable;
          message = "Forgejo event ingest requires custom.services.hermes.enable.";
        }
        {
          assertion = endpoint != "";
          message = "Forgejo event ingest requires the owning profile's externalServices.forgejo.endpoint.";
        }
        {
          assertion = credentialFile != null;
          message = "Forgejo event ingest requires the owning profile's externalServices.forgejo.credentialFiles.credential.";
        }
        {
          assertion = builtins.all (name: builtins.elem name forgejoEventIngest.allowedSettings) (
            builtins.attrNames settings
          );
          message = "forgejo-event-ingest settings support only initialSince and pollInterval.";
        }
        {
          assertion =
            initialSince == "service-start"
            || initialSince == "epoch"
            || builtins.match "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$" initialSince != null;
          message = "forgejo-event-ingest initialSince must be service-start, epoch, or a UTC RFC3339 second.";
        }
        {
          assertion = builtins.match "^[1-9][0-9]*(s|m|h|d)$" pollInterval != null;
          message = "forgejo-event-ingest pollInterval must be a positive systemd duration in s, m, h, or d.";
        }
      ];

      systemd = {
        services.hermes-forgejo-event-poll = {
          description = "Deterministic Forgejo event polling repair for the configured repository";
          wants = [ "network-online.target" ];
          after = [ "network-online.target" ];
          serviceConfig = commonHardening // {
            Type = "oneshot";
            ExecStart = pollStart;
            ReadOnlyPaths = [ credentialPath ];
          };
        };

        timers.hermes-forgejo-event-poll = {
          description = "Schedule deterministic Forgejo event polling repair";
          wantedBy = [ "timers.target" ];
          timerConfig = {
            OnBootSec = "2m";
            OnUnitActiveSec = pollInterval;
            AccuracySec = "15s";
            RandomizedDelaySec = "15s";
            Persistent = true;
            Unit = "hermes-forgejo-event-poll.service";
          };
        };
      };
    })
  ];
}
