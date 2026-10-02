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

  vikunjaWorkflow = capabilityLib.mkExternalServiceCapability "vikunja" "workflow" {
    requiredHermesCapabilities = [ "toolsets" ];
    provides.managedArtifacts = [ "file:vikunja/human-action-contract" ];
  };
  kanbanToVikunja = capabilityLib.mkServiceIntegration "kanban-to-vikunja" {
    safety = {
      credentialRequirements = [
        {
          service = "vikunja";
          field = "credential";
        }
      ];
      pathRequirements = [
        {
          path = "/var/lib/hermes-kanban-vikunja-projection";
          access = "read-write";
        }
      ];
      writeScope = "external-system";
      confirmation = "policy-preauthorized";
      verification = "external-readback-required";
    };
    allowedSettings = [
      "instance"
      "board"
      "defaultPolicy"
      "interval"
      "humanActionProject"
      "humanActionProjectTitle"
      "projectOwner"
      "projectPolicies"
    ];
    requires = [ vikunjaWorkflow.id ];
    requiredExternalServices = [ "vikunja" ];
    requiredHermesCapabilities = [
      "kanban"
      "dispatcher"
    ];
    provides.managedArtifacts = [
      "unit:hermes-kanban-to-vikunja"
      "timer:hermes-kanban-to-vikunja"
    ];
  };
  forgejoPrLifecycle = capabilityLib.mkServiceIntegration "forgejo-pr-lifecycle" {
    requires = [
      kanbanToVikunja.id
      "externalService:forgejo:workflow"
      "agentSkill:forgejo-pr-lifecycle"
    ];
    requiredExternalServices = [ "forgejo" ];
    requiredHermesCapabilities = [ "kanban" ];
    safety = {
      credentialRequirements = [
        {
          service = "forgejo";
          field = "credential";
        }
      ];
      pathRequirements = [ ];
      writeScope = "external-system";
      confirmation = "policy-preauthorized";
      verification = "external-readback-required";
    };
    provides.runtimePackages = [ pkgs.forgejo-kanban-workflow ];
    provides.managedArtifacts = [ "file:forgejo-pr-lifecycle/contract" ];
  };
  vikunjaToKanban = capabilityLib.mkServiceIntegration "vikunja-to-kanban" {
    safety = {
      credentialRequirements = [
        {
          service = "vikunja";
          field = "credential";
        }
      ];
      pathRequirements = [
        {
          path = "/var/lib/hermes-kanban-vikunja-projection";
          access = "read-write";
        }
      ];
      writeScope = "external-system";
      confirmation = "policy-preauthorized";
      verification = "external-readback-required";
    };
    allowedSettings = [
      "instance"
      "actorUsername"
      "board"
      "defaultPolicy"
      "interval"
      "humanActionProject"
      "humanActionProjectTitle"
      "projectOwner"
      "projectPolicies"
      "wakeOnHumanComment"
    ];
    requires = [ vikunjaWorkflow.id ];
    requiredExternalServices = [ "vikunja" ];
    requiredHermesCapabilities = [
      "kanban"
      "dispatcher"
    ];
    provides.managedArtifacts = [
      "unit:hermes-vikunja-to-kanban"
      "timer:hermes-vikunja-to-kanban"
    ];
  };

  profiles = lib.mapAttrs (
    name: profile: lib.recursiveUpdate profile (config.services.hermes-agent.profiles.${name} or { })
  ) hermesCfg.profiles;
  integrationSelection = profile: name: profile.serviceIntegrations.${name} or false;
  integrationSelected =
    profile: name:
    let
      selection = integrationSelection profile name;
    in
    profile.enable && (if builtins.isBool selection then selection else selection.enable);
  integrationSettings =
    profile: name:
    let
      selection = integrationSelection profile name;
    in
    if builtins.isBool selection then { } else selection.settings;

  contextFor =
    name: provider:
    let
      selectedProfiles = lib.filterAttrs (_: profile: integrationSelected profile name) profiles;
      selectedNames = builtins.attrNames selectedProfiles;
      ownerName = if builtins.length selectedNames == 1 then builtins.head selectedNames else null;
      ownerProfile = if ownerName == null then null else profiles.${ownerName};
      settings = if ownerProfile == null then { } else integrationSettings ownerProfile name;
      vikunjaService = if ownerProfile == null then { } else ownerProfile.externalServices.vikunja or { };
      board = settings.board or "";
      projectScope = vikunjaService.scope.include or [ ];
    in
    {
      inherit
        board
        name
        ownerName
        ownerProfile
        projectScope
        provider
        selectedNames
        settings
        vikunjaService
        ;
      selected = selectedNames != [ ];
      projectIds = map builtins.fromJSON projectScope;
      endpoint = vikunjaService.endpoint or "";
      credentialFile = vikunjaService.credentialFiles.credential or null;
    };

  kanbanToVikunjaContext = contextFor "kanban-to-vikunja" kanbanToVikunja;
  vikunjaToKanbanContext = contextFor "vikunja-to-kanban" vikunjaToKanban;
  prLifecycleSelected =
    kanbanToVikunjaContext.ownerProfile != null
    && integrationSelected kanbanToVikunjaContext.ownerProfile "forgejo-pr-lifecycle";
  prForgejo =
    if prLifecycleSelected then
      kanbanToVikunjaContext.ownerProfile.externalServices.forgejo or { }
    else
      { };
  anySelected = kanbanToVikunjaContext.selected || vikunjaToKanbanContext.selected;

  policyModes = [
    "linkedOnly"
    "taskAllowlist"
    "projectWide"
  ];
  projectPoliciesFor = settings: settings.projectPolicies or { };
  projectPoliciesValid =
    projectScope: settings:
    let
      policies = projectPoliciesFor settings;
      policyValid =
        policy:
        builtins.all (
          field:
          builtins.elem field [
            "mode"
            "taskAllowlist"
          ]
        ) (builtins.attrNames policy)
        && builtins.elem (policy.mode or "linkedOnly") policyModes
        && builtins.isList (policy.taskAllowlist or [ ])
        && builtins.all (taskId: builtins.isInt taskId && taskId > 0) (policy.taskAllowlist or [ ])
        && (
          if (policy.mode or "linkedOnly") == "taskAllowlist" then
            (policy.taskAllowlist or [ ]) != [ ]
          else
            (policy.taskAllowlist or [ ]) == [ ]
        );
    in
    builtins.all (projectId: builtins.elem projectId projectScope) (builtins.attrNames policies)
    && builtins.all policyValid (builtins.attrValues policies)
    && builtins.hasAttr (toString (settings.humanActionProject or 0)) policies
    && (policies.${toString (settings.humanActionProject or 0)}.mode or "linkedOnly") == "linkedOnly"
    && (policies.${toString (settings.humanActionProject or 0)}.taskAllowlist or [ ]) == [ ];
  normalizeProjectPolicies =
    settings:
    lib.mapAttrs (_: policy: {
      mode = policy.mode or "linkedOnly";
      task_allowlist = policy.taskAllowlist or [ ];
    }) (projectPoliciesFor settings);
  edgeJson = enabled: projectIds: settings: {
    inherit enabled;
    selector = {
      include = projectIds;
      exclude = null;
      include_future_projects = false;
    };
    default_policy = settings.defaultPolicy or "linkedOnly";
    project_policies = if enabled then normalizeProjectPolicies settings else { };
  };
  configFileFor =
    context: direction:
    pkgs.writeText "${direction}.json" (
      builtins.toJSON {
        schema_version = 1;
        inherit (context) board;
        project_owner = context.settings.projectOwner or "";
        edges = {
          kanban_to_vikunja = edgeJson (direction == "kanban-to-vikunja") (
            if direction == "kanban-to-vikunja" then context.projectIds else [ ]
          ) context.settings;
          vikunja_to_kanban =
            (edgeJson (direction == "vikunja-to-kanban") (
              if direction == "vikunja-to-kanban" then context.projectIds else [ ]
            ) context.settings)
            // {
              wake_on_human_comment = context.settings.wakeOnHumanComment or true;
            };
        };
      }
    );

  stateDirectory = "hermes-kanban-vikunja-projection";
  stateRoot = "/var/lib/${stateDirectory}";
  hermesHome = "${hermesCfg.stateDir}/.hermes";
  commonService =
    context: direction:
    let
      credentialPath =
        if context.credentialFile == null then "/invalid" else toString context.credentialFile;
      configFile = configFileFor context direction;
      withPrLifecycle = direction == "kanban-to-vikunja" && prLifecycleSelected;
    in
    {
      description = "Deterministic ${direction} human-action projection";
      environment.HERMES_REPOSITORY_INSTANCES = toString hermesCfg.repositoryInstancesDirectory;
      requires = [ "hermes-agent.service" ];
      after = [ "hermes-agent.service" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = hermesCfg.user;
        Group = hermesCfg.group;
        LoadCredential = [
          "vikunja-credential:${credentialPath}"
        ]
        ++ lib.optionals withPrLifecycle [
          "forgejo-credential:${toString (prForgejo.credentialFiles.credential or "/invalid")}"
        ];
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
        ReadWritePaths = [ hermesHome ];
        MemoryMax = "256M";
        TasksMax = 64;
        TimeoutStartSec = "5m";
        ExecStart = pkgs.writeShellScript "hermes-repository-projection" (
          "set -eu\n"
          + lib.concatStringsSep "\n" (
            lib.mapAttrsToList (
              name: file:
              lib.escapeShellArgs (
                [
                  "${pkgs.kanban-vikunja-projection}/bin/kanban-vikunja-projection"
                  "--instance-config"
                  (toString file)
                  "--config"
                  configFile
                  "--endpoint"
                  context.endpoint
                  "--credential-file"
                  "%d/vikunja-credential"
                  "--hermes"
                  "${config.services.hermes-agent.package}/bin/hermes"
                  "--hermes-home"
                  hermesHome
                  "--actor-username"
                  (context.settings.actorUsername or context.ownerName)
                  "--state-root"
                  "${stateRoot}${hermesCfg.repositoryStateSubdirectories.${name}}"
                  direction
                ]
                ++ lib.optionals withPrLifecycle [
                  "--pr-lifecycle"
                  "--forgejo-endpoint"
                  (prForgejo.endpoint or "")
                  "--forgejo-credential-file"
                  "%d/forgejo-credential"
                ]
              )
            ) hermesCfg.repositoryInstanceFiles
          )
        );
      };
    };

  contextAssertions =
    context: wake:
    [
      {
        assertion = builtins.length context.selectedNames <= 1;
        message = "${context.name} must have at most one owning profile; selected by: ${lib.concatStringsSep ", " context.selectedNames}.";
      }
      {
        assertion = !context.selected || context.endpoint != "";
        message = "${context.name} requires the owning profile's externalServices.vikunja.endpoint.";
      }
      {
        assertion = !context.selected || context.credentialFile != null;
        message = "${context.name} requires the owning profile's externalServices.vikunja.credentialFiles.credential.";
      }
      {
        assertion =
          !context.selected
          || lib.elem (toString (context.settings.humanActionProject or 0)) context.projectScope;
        message = "${context.name} the configured human-action project must remain explicitly included and linkedOnly.";
      }
      {
        assertion =
          !context.selected
          || builtins.all (setting: builtins.elem setting context.provider.allowedSettings) (
            builtins.attrNames context.settings
          );
        message = "${context.name} contains unsupported settings.";
      }
      {
        assertion = !context.selected || builtins.match "^[a-z0-9][a-z0-9_-]{0,63}$" context.board != null;
        message = "${context.name} board must be a safe native board slug.";
      }
      {
        assertion =
          !context.selected || builtins.elem (context.settings.defaultPolicy or "linkedOnly") policyModes;
        message = "${context.name} defaultPolicy must be linkedOnly, taskAllowlist, or projectWide.";
      }
      {
        assertion =
          !context.selected
          || builtins.match "^[1-9][0-9]*(s|m|h|d)$" (context.settings.interval or "1m") != null;
        message = "${context.name} interval must be a positive systemd duration in s, m, h, or d.";
      }
      {
        assertion = !context.selected || (context.settings.projectOwner or "") != "";
        message = "${context.name} projectOwner is an explicit human owner.";
      }
      {
        assertion = !context.selected || projectPoliciesValid context.projectScope context.settings;
        message = "${context.name} project policies must be scoped, typed, and keep the configured human-action project linkedOnly.";
      }
    ]
    ++ lib.optionals wake [
      {
        assertion =
          !context.selected
          ||
            builtins.match "^[a-z0-9][a-z0-9_-]{0,63}$" (context.settings.actorUsername or context.ownerName)
            != null;
        message = "${context.name} actorUsername must be a safe automation identity.";
      }
      {
        assertion = !context.selected || builtins.isBool (context.settings.wakeOnHumanComment or true);
        message = "${context.name} wakeOnHumanComment must be boolean.";
      }
    ];
in
{
  imports = [ ./repository-instance.nix ];
  config = mkMerge [
    {
      custom.services.hermes.capabilityPackages = {
        ${vikunjaWorkflow.id} = vikunjaWorkflow;
        ${forgejoPrLifecycle.id} = forgejoPrLifecycle;
        ${kanbanToVikunja.id} = kanbanToVikunja;
        ${vikunjaToKanban.id} = vikunjaToKanban;
      };
      assertions = [
        {
          assertion =
            !prLifecycleSelected
            || (
              (prForgejo.endpoint or "") != ""
              && (prForgejo.credentialFiles.credential or null) != null
              && (prForgejo.scope.include or [ ]) != [ ]
            );
          message = "forgejo-pr-lifecycle requires the projection owner's scoped Forgejo read interface.";
        }
      ]
      ++ contextAssertions kanbanToVikunjaContext false
      ++ contextAssertions vikunjaToKanbanContext true;
    }
    (mkIf anySelected {
      assertions = [
        {
          assertion = hermesCfg.enable;
          message = "Kanban/Vikunja projection selected by an enabled Hermes profile requires custom.services.hermes.enable.";
        }
      ];

      systemd = {
        services = {
          hermes-kanban-to-vikunja = mkIf kanbanToVikunjaContext.selected (
            commonService kanbanToVikunjaContext "kanban-to-vikunja"
          );
          hermes-vikunja-to-kanban = mkIf vikunjaToKanbanContext.selected (
            commonService vikunjaToKanbanContext "vikunja-to-kanban"
          );
        };
        timers = {
          hermes-kanban-to-vikunja = mkIf kanbanToVikunjaContext.selected {
            description = "Schedule deterministic Kanban-to-Vikunja projection";
            wantedBy = [ "timers.target" ];
            timerConfig = {
              OnBootSec = "4m";
              OnUnitActiveSec = kanbanToVikunjaContext.settings.interval or "1m";
              AccuracySec = "15s";
              RandomizedDelaySec = "15s";
              Persistent = true;
              Unit = "hermes-kanban-to-vikunja.service";
            };
          };
          hermes-vikunja-to-kanban = mkIf vikunjaToKanbanContext.selected {
            description = "Schedule deterministic Vikunja-to-Kanban projection";
            wantedBy = [ "timers.target" ];
            timerConfig = {
              OnBootSec = "4m30s";
              OnUnitActiveSec = vikunjaToKanbanContext.settings.interval or "1m";
              AccuracySec = "15s";
              RandomizedDelaySec = "15s";
              Persistent = true;
              Unit = "hermes-vikunja-to-kanban.service";
            };
          };
        };
      };
    })
  ];
}
