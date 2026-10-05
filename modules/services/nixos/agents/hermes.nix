{
  config,
  inputs,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    concatLists
    concatStringsSep
    filterAttrs
    mapAttrs'
    mapAttrsToList
    mkDefault
    mkEnableOption
    mkForce
    mkIf
    mkMerge
    mkOption
    nameValuePair
    optional
    optionalAttrs
    optionalString
    types
    unique
    ;

  cfg = config.custom.services.hermes;
  capabilityProfiles = config.services.hermes-agent.profiles;
  capabilityLib = import ./capabilities/lib.nix { inherit lib; };
  nativeHermesCapabilities = [
    "kanban"
    "cron"
    "delegation"
    "toolsets"
    "dispatcher"
    "profile-lifecycle"
    "gateway-delivery"
  ];
  integrationEnabled = selection: if builtins.isBool selection then selection else selection.enable;
  integrationNativeJobs =
    selection:
    if builtins.isBool selection then
      [ ]
    else
      builtins.attrNames (filterAttrs (_name: enabled: enabled) selection.instances);

  profileCapabilityRoots =
    profile:
    map (name: "agentSkill:${name}") (
      builtins.attrNames (filterAttrs (_name: enabled: enabled) profile.agentSkills)
    )
    ++ concatLists (
      mapAttrsToList (
        serviceName: service:
        map (capability: "externalService:${serviceName}:${capability}") service.capabilities
      ) profile.externalServices
    )
    ++ map (name: "serviceIntegration:${name}") (
      builtins.attrNames (
        filterAttrs (
          name: selection:
          integrationEnabled selection
          && (
            builtins.isBool selection
            || (cfg.capabilityPackages."serviceIntegration:${name}".provides.nativeJobs or { }) == { }
            || builtins.any (enabled: enabled) (builtins.attrValues selection.instances)
          )
        ) profile.serviceIntegrations
      )
    );

  profileSelectedCapabilityClosure =
    profile: capabilityLib.resolve cfg.capabilityPackages (profileCapabilityRoots profile);

  selectedNativeJobs =
    profile: closure:
    let
      selectedByProvider =
        jobName: providerId:
        let
          provider = cfg.capabilityPackages.${providerId};
          selection = profile.serviceIntegrations.${provider.name} or true;
          selectedNames = integrationNativeJobs selection;
        in
        provider.kind != "serviceIntegration"
        || builtins.isBool selection
        || builtins.elem jobName selectedNames;
    in
    filterAttrs (
      jobName: _job: selectedByProvider jobName closure.provenance."cron:${jobName}"
    ) closure.nativeJobs;

  # Derive the implicit root from ordinary resources, never from its own output.
  profileCapabilityClosure =
    profile:
    let
      selected = profileSelectedCapabilityClosure profile;
      managed =
        profile.declarativeSkills != { }
        || selected.skills != { }
        || selectedNativeJobs profile selected != { };
    in
    assert lib.assertMsg (
      !(builtins.elem "agentSkill:nix-managed-resource-changes" (profileCapabilityRoots profile))
    ) "nix-managed-resource-changes is automatic; do not select it as a profile root";
    capabilityLib.resolve cfg.capabilityPackages (
      profileCapabilityRoots profile ++ optional managed "agentSkill:nix-managed-resource-changes"
    );

  profileNativeJobs = profile: selectedNativeJobs profile (profileCapabilityClosure profile);

  managedSkillLib = import ./capabilities/managed-skills.nix {
    inherit lib;
    repositories = cfg.managedSourceRepositories;
  };
  profileEffectiveDeclarativeSkills =
    profile:
    assert lib.assertMsg (
      !(profile.declarativeSkills ? nix-managed-resource-changes)
    ) "nix-managed-resource-changes is reserved for automatic publication";
    profile.declarativeSkills // (profileCapabilityClosure profile).skills;

  profileManagedSkillManifest =
    name: profile:
    pkgs.writeText "hermes-managed-skills-${name}.json" (
      builtins.toJSON (
        managedSkillLib.manifest {
          profile = name;
          skills = profileEffectiveDeclarativeSkills profile;
          providers = (profileCapabilityClosure profile).provenance;
          nativeJobs = profileNativeJobs profile;
          selectionDeclarations = profile.selectionDeclarationFiles or [ ];
        }
      )
    );

  profilePublishedSkills =
    name: profile:
    let
      skills = profileEffectiveDeclarativeSkills profile;
    in
    skills
    // optionalAttrs (skills != { }) {
      nix-managed-resource-changes = skills.nix-managed-resource-changes // {
        source = pkgs.runCommand "hermes-${name}-managed-skill-changes" { } ''
          mkdir -p "$out/references"
          cp -R ${./skills/nix-managed-resource-changes}/. "$out/"
          cp ${profileManagedSkillManifest name profile} "$out/references/managed-resources.json"
        '';
      };
    };

  managedSkillsActivationSpec = pkgs.writeText "hermes-managed-skills-activation.json" (
    builtins.toJSON (
      lib.mapAttrs
        (name: profile: {
          target = toString (profileManagedSkillManifest name profile);
          skills = builtins.attrNames (profileEffectiveDeclarativeSkills profile);
        })
        (
          filterAttrs (_: profileHasDeclarativeSkills) (
            enabledProfiles // { default = effectiveDefaultProfile; }
          )
        )
    )
  );

  emptyCapabilityRoots = {
    agentSkills = { };
    externalServices = { };
    serviceIntegrations = { };
  };
  effectiveProfiles = lib.mapAttrs (
    name: profile:
    lib.recursiveUpdate profile (
      lib.recursiveUpdate emptyCapabilityRoots (capabilityProfiles.${name} or { })
    )
  ) cfg.profiles;
  enabledProfiles = filterAttrs (_name: profile: profile.enable) effectiveProfiles;
  effectiveDefaultProfile = lib.recursiveUpdate cfg.defaultProfile (
    lib.recursiveUpdate emptyCapabilityRoots (capabilityProfiles.default or { })
  );

  hermesHomeDir = "${cfg.stateDir}/.hermes";
  profileDir = name: "${hermesHomeDir}/profiles/${name}";
  gatewayUnits = [
    "hermes-agent.service"
  ]
  ++ mapAttrsToList (name: _profile: "hermes-agent-${name}.service") enabledProfiles;
  gatewayUnitArgs = lib.escapeShellArgs gatewayUnits;
  # Shared gateway tools; normal service.path merging also supplies NixOS's
  # default tools, including systemd-run and systemctl for restart-safe scopes.
  gatewayRuntimePackages = [
    config.services.hermes-agent.package
    pkgs.bash
    pkgs.coreutils
    pkgs.docker
    pkgs.git
  ]
  ++ config.services.hermes-agent.extraPackages;
  runtimeDir = "/run/user/${toString cfg.uid}";
  dockerSocket = "${runtimeDir}/docker.sock";
  defaultOutputDir =
    if cfg.outputDir != null then cfg.outputDir else "${hermesHomeDir}/cache/documents";
  profileOutputDir =
    name: profile:
    if profile.outputDir != null then profile.outputDir else "${profileDir name}/cache/documents";
  profileOutputDirs = [
    defaultOutputDir
  ]
  ++ mapAttrsToList (name: profile: profileOutputDir name profile) enabledProfiles;
  profileOutputDirsCanonical = builtins.all (
    path:
    path != "/"
    && lib.hasPrefix "/" path
    && !lib.hasSuffix "/" path
    && builtins.all (component: component != "" && component != "." && component != "..") (
      builtins.tail (lib.splitString "/" path)
    )
  ) profileOutputDirs;
  profileOutputDirsDisjoint = lib.all (row: lib.all (value: value) row) (
    lib.imap0 (
      leftIndex: left:
      lib.imap0 (
        rightIndex: right:
        leftIndex >= rightIndex
        || !(left == right || lib.hasPrefix "${left}/" right || lib.hasPrefix "${right}/" left)
      ) profileOutputDirs
    ) profileOutputDirs
  );
  defaultWorkspaceDir = "${hermesHomeDir}/sandboxes/docker/default/workspace";
  profileWorkspaceDir = name: "${profileDir name}/sandboxes/docker/default/workspace";
  defaultDockerHomeDir = "${hermesHomeDir}/sandboxes/docker/default/home";
  profileDockerHomeDir = name: "${profileDir name}/sandboxes/docker/default/home";
  defaultProfilesMaskDir = "${hermesHomeDir}/sandboxes/docker/default/empty-profiles";
  cacheSubdirs = [
    # Own the parent explicitly: install -d otherwise creates it as root,
    # preventing native tools from creating new cache subdirectories.
    "cache"
    "cache/vision"
    "cache/documents"
    "cache/images"
    "cache/audio"
    "cache/videos"
    "cache/screenshots"
    "cache/web"
    "cache/delegation"
  ];
  legacyCacheDirs = [
    "document_cache"
    "image_cache"
    "audio_cache"
    "video_cache"
    "browser_screenshots"
  ];
  cacheDirsFor =
    home:
    map (subdir: "${home}/${subdir}") cacheSubdirs ++ map (name: "${home}/${name}") legacyCacheDirs;
  defaultCacheDirs = cacheDirsFor hermesHomeDir;
  profileCacheDirs = name: cacheDirsFor (profileDir name);
  defaultManagedDirs = unique (
    defaultCacheDirs
    ++ [
      defaultOutputDir
      "${hermesHomeDir}/scripts"
      defaultDockerHomeDir
      defaultProfilesMaskDir
      defaultWorkspaceDir
    ]
    ++ optional (profileHasSsh effectiveDefaultProfile) "${defaultDockerHomeDir}/.ssh"
  );
  profileManagedDirs =
    name: profile:
    unique (
      profileCacheDirs name
      ++ [
        (profileOutputDir name profile)
        "${profileDir name}/scripts"
        (profileDockerHomeDir name)
        (profileWorkspaceDir name)
      ]
      ++ optional (profileHasSsh profile) "${profileDockerHomeDir name}/.ssh"
    );
  yamlFormat = pkgs.formats.yaml { };
  jsonFormat = pkgs.formats.json { };
  nativeCronReconciler = pkgs.callPackage ../../../../pkgs/common/hermes-native-cron-reconciler { };

  profileAllowNetwork =
    profile: if profile.allowNetwork != null then profile.allowNetwork else cfg.allowNetwork;

  profileBaseToolsets =
    profile:
    optional profile.telegram.enable "hermes-telegram"
    ++ [
      "hermes-cron"
    ];

  profileSettingsWithExtraToolsets =
    profile:
    let
      closure = profileCapabilityClosure profile;
      mergedSettings = lib.recursiveUpdate closure.hermesConfig profile.settings;
      extraToolsets = lib.unique (closure.toolsets ++ profile.extraToolsets);
    in
    if extraToolsets == [ ] then
      mergedSettings
    else
      mergedSettings
      // {
        toolsets = lib.unique ((mergedSettings.toolsets or (profileBaseToolsets profile)) ++ extraToolsets);
      };

  profileHasDeclarativeSystemPrompt =
    profile:
    profile.settings ? agent
    && builtins.isAttrs profile.settings.agent
    && profile.settings.agent ? system_prompt;

  profileHasDeclarativeSoul = profile: profile ? soulContent && profile.soulContent != null;

  profileSoulFile = name: profile: pkgs.writeText "hermes-${name}-SOUL.md" profile.soulContent;

  sandboxLib = import ./capabilities/sandbox.nix { inherit lib pkgs; };
  profileSandbox =
    name: profile:
    sandboxLib {
      inherit name;
      skills = profilePublishedSkills name profile;
      extraStorePaths = optional (profileHasDeclarativeSkills profile) (
        profileManagedSkillManifest name profile
      );
      inherit (profileCapabilityClosure profile) runtimePackages;
    };

  profileHasDeclarativeSkills = profile: profileEffectiveDeclarativeSkills profile != { };

  profileDeclarativeSkillFile =
    _profileName: skillName: skill:
    if skill.source != null then
      pkgs.linkFarm "hermes-skill-${skillName}" [
        {
          name = "${skill.category}/${skillName}";
          path = skill.source;
        }
      ]
    else
      pkgs.writeTextDir "${skill.category}/${skillName}/SKILL.md" skill.content;

  profileDeclarativeSkillsDir =
    name: profile:
    pkgs.symlinkJoin {
      name = "hermes-${name}-declarative-skills";
      paths = mapAttrsToList (profileDeclarativeSkillFile name) (profilePublishedSkills name profile);
    };

  profileDeclarativeSkillCollisionCheck =
    name: profile:
    optionalString (profileHasDeclarativeSkills profile) ''
      profile_skills_dir=${lib.escapeShellArg "${profileDir name}/skills"}
      ${concatStringsSep "\n" (
        mapAttrsToList (skillName: _skill: ''
          for candidate in "$profile_skills_dir"/*/${lib.escapeShellArg skillName}/SKILL.md "$profile_skills_dir"/${lib.escapeShellArg skillName}/SKILL.md; do
            if [ -e "$candidate" ]; then
              echo "error: Hermes profile ${lib.escapeShellArg name} has mutable local skill ${lib.escapeShellArg skillName} at $candidate, which would shadow the repo-managed declarative skill exposed through skills.external_dirs" >&2
              exit 1
            fi
          done
        '') (profileEffectiveDeclarativeSkills profile)
      )}
    '';

  pathType = types.either types.path types.str;

  profileHasSsh = profile: profile.ssh.privateKeyFile != null;

  profileSshKeyPath = profile: toString profile.ssh.privateKeyFile;

  profileHasExternalServices = profile: profile.externalServices != { };

  # Host staging consumed by Docker terminals. Only each profile's selected
  # subtree is mounted at the canonical container path; profiles still share
  # the host Hermes user and rootless Docker control plane.
  servicesRuntimeRoot = "${runtimeDir}/hermes-credentials";

  profileServicesRuntimeRoot = name: "${servicesRuntimeRoot}/${name}";

  profileServicesRuntimeDir = name: "${profileServicesRuntimeRoot name}/services";

  reservedServicesContainerDir = "/run/hermes-credentials/services";

  volumeTargetsReservedServices =
    volume:
    let
      parts = lib.splitString ":" volume;
      target = if builtins.length parts >= 2 then builtins.elemAt parts 1 else "";
    in
    target == reservedServicesContainerDir || lib.hasPrefix "${reservedServicesContainerDir}/" target;

  volumeTargetsReservedHelpers =
    volume:
    let
      parts = lib.splitString ":" volume;
      rawTarget = if builtins.length parts >= 2 then builtins.elemAt parts 1 else volume;
      components = lib.splitString "/" rawTarget;
      target = "/" + concatStringsSep "/" (builtins.filter (part: part != "") components);
    in
    builtins.elem ".." components
    || builtins.elem "." components
    ||
      builtins.any
        (
          reserved:
          target == reserved
          || lib.hasPrefix "${reserved}/" target
          || lib.hasPrefix "${lib.removeSuffix "/" target}/" reserved
        )
        [
          "/run/hermes-capabilities"
          "/run/hermes-repository-instances"
          "/nix/store"
        ];

  profileSshRuntimeDir = name: "${runtimeDir}/hermes-ssh/${name}";

  profileSshRuntimeKeyPath = name: "${profileSshRuntimeDir name}/id_ed25519";

  profileHasTelegramToken = profile: profile.telegram.enable && profile.telegram.botTokenFile != null;

  profileCredentialSourcePaths =
    profile:
    unique (
      optional (profileHasTelegramToken profile) (profileTelegramTokenPath profile)
      ++ optional (profileHasSsh profile) (profileSshKeyPath profile)
      ++ map toString profile.environmentFiles
      ++ concatLists (
        mapAttrsToList (
          _serviceName: service: map toString (builtins.attrValues service.credentialFiles)
        ) profile.externalServices
      )
    );

  allProfileCredentialSourcePaths =
    profileCredentialSourcePaths effectiveDefaultProfile
    ++ concatLists (mapAttrsToList (_name: profileCredentialSourcePaths) enabledProfiles);

  duplicateProfileCredentialSourcePaths = unique (
    builtins.filter (
      path: lib.count (candidate: candidate == path) allProfileCredentialSourcePaths > 1
    ) allProfileCredentialSourcePaths
  );

  unapprovedSharedCredentialSourcePaths = lib.subtractLists cfg.sharedCredentialSourcePaths duplicateProfileCredentialSourcePaths;

  profileTelegramTokenPath = profile: toString profile.telegram.botTokenFile;

  profileGitSshCommand =
    name: profile:
    "${pkgs.writeShellScriptBin "hermes-${name}-git-ssh" ''
      exec ${pkgs.openssh}/bin/ssh \
        -i ${lib.escapeShellArg (profileSshKeyPath profile)} \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=accept-new \
        "$@"
    ''}/bin/hermes-${name}-git-ssh";

  addProfileSshTerminalSettings =
    name: profile: settings:
    if profileHasSsh profile then
      settings
      // {
        docker_env = (settings.docker_env or { }) // {
          HERMES_SSH_PRIVATE_KEY_FILE = profile.ssh.containerPath;
          GIT_SSH_COMMAND = "ssh -F /root/.ssh/config";
        };
        docker_volumes = (settings.docker_volumes or [ ]) ++ [
          "${profileSshRuntimeDir name}:/root/.ssh:ro"
        ];
      }
    else
      settings;

  addProfileExternalServicesTerminalSettings =
    name: profile: settings:
    if profileHasExternalServices profile then
      settings
      // {
        docker_volumes = (settings.docker_volumes or [ ]) ++ [
          "${profileServicesRuntimeDir name}:${reservedServicesContainerDir}:ro"
        ];
      }
    else
      settings;

  profileUsesRepositoryInstance =
    name:
    builtins.any (
      instance:
      builtins.elem name [
        instance.roles.implementer
        instance.roles.reviewer
      ]
    ) (builtins.attrValues cfg.repositoryInstances);

  addProfileHelpersTerminalSettings =
    name: profile: settings:
    settings
    // {
      docker_volumes =
        (settings.docker_volumes or [ ])
        ++ (profileSandbox name profile).volumes
        ++ lib.optional (profileUsesRepositoryInstance name) "${cfg.repositoryInstancesDirectory}:/run/hermes-repository-instances:ro";
      docker_env =
        (settings.docker_env or { })
        // lib.optionalAttrs (profileUsesRepositoryInstance name) {
          HERMES_REPOSITORY_INSTANCES = "/run/hermes-repository-instances";
        };
    };

  addDefaultProfileMask =
    name: settings:
    if name == "default" then
      settings
      // {
        docker_volumes = (settings.docker_volumes or [ ]) ++ [
          "${defaultProfilesMaskDir}:${hermesHomeDir}/profiles:ro"
        ];
      }
    else
      settings;

  baseTerminalSettings =
    {
      outputDir,
      workspaceDir,
      profileHomeDir,
      allowNetwork,
    }:
    {
      backend = "docker";
      cwd = cfg.workingDirectory;
      env_passthrough = [ "DOCKER_HOST" ];
      docker_image = "nikolaik/python-nodejs:python3.11-nodejs20";
      docker_mount_cwd_to_workspace = false;
      docker_run_as_host_user = false;
      docker_forward_env = [ ];
      docker_env = {
        HERMES_OUTPUT_DIR = cfg.outputContainerPath;
        HERMES_HOST_OUTPUT_DIR = outputDir;
        HERMES_MEDIA_OUTPUT_DIR = outputDir;
        HERMES_HOME = profileHomeDir;
      };
      docker_volumes = [
        "${profileHomeDir}:${profileHomeDir}:rw"
        "${profileHomeDir}/scripts:/scripts:rw"
        "${workspaceDir}:/workspace:rw"
        "${outputDir}:${cfg.outputContainerPath}:rw"
        "${outputDir}:${outputDir}:rw"
      ];
      docker_extra_args = optional (!allowNetwork) "--network=none";
      docker_orphan_reaper = true;
      docker_persist_across_processes = false;
      container_persistent = true;
      container_cpu = 2;
      container_memory = 2048;
      container_disk = 0;
      timeout = 180;
      lifetime_seconds = 3600;
    };

  terminalBaseSettingsFor =
    {
      name ? "default",
      outputDir,
      workspaceDir,
      allowNetwork,
      profile ? { },
    }:
    addProfileHelpersTerminalSettings name profile (
      addProfileExtraDockerVolumes profile (
        addDefaultProfileMask name (
          addProfileExternalServicesTerminalSettings name profile (
            addProfileSshTerminalSettings name profile (
              lib.recursiveUpdate (baseTerminalSettings {
                inherit outputDir workspaceDir allowNetwork;
                profileHomeDir = if name == "default" then hermesHomeDir else profileDir name;
              }) (profileUserTerminalSettings profile)
            )
          )
        )
      )
    );

  # Integration-owned mounts extend the resolved terminal configuration rather
  # than replacing its profile, workspace and output mounts through an override.
  addProfileExtraDockerVolumes =
    profile: settings:
    settings
    // {
      docker_volumes = (settings.docker_volumes or [ ]) ++ (profile.extraDockerVolumes or [ ]);
    };

  # Consume every supported override before adding managed mounts and hashing.
  profileUserTerminalSettings =
    profile:
    lib.recursiveUpdate (lib.recursiveUpdate cfg.terminalSettings (profile.terminalSettings or { })) (
      (profileSettingsWithExtraToolsets profile).terminal or { }
    );

  profileDockerConfigHash =
    name: settings:
    builtins.substring 0 32 (
      builtins.hashString "sha256" (
        builtins.toJSON {
          inherit name settings;
        }
      )
    );

  addProfileDockerConfigLabel =
    name: _profile: settings:
    let
      configHash = profileDockerConfigHash name settings;
    in
    settings
    // {
      docker_extra_args = (settings.docker_extra_args or [ ]) ++ [
        "--label"
        "hermes-config-hash=${configHash}"
      ];
    };

  terminalSettingsFor =
    args@{
      name ? "default",
      profile ? { },
      ...
    }:
    addProfileDockerConfigLabel name profile (terminalBaseSettingsFor args);

  outputEnvironmentHint =
    { outputDir, workspaceDir }:
    ''
      File delivery from this Docker-backed terminal uses profile-scoped host bind mounts:
      - /workspace is the normal persistent workspace and is backed by ${workspaceDir}.
      - ${cfg.outputContainerPath} and ${outputDir} are backed by the profile's document output directory.
      - Profile identity lives at $HERMES_HOME/SOUL.md. Persistent memory is managed through the memory tool/skill, not by editing /root/.hermes.
      - To send a generated file through Telegram or another messaging platform, include MEDIA:/workspace/filename.ext, MEDIA:${cfg.outputContainerPath}/filename.ext, or MEDIA:${outputDir}/filename.ext in your final reply.
      - Do not use paths outside /workspace, ${cfg.outputContainerPath}, or ${outputDir} for MEDIA attachments unless the user explicitly gave you that file path.
    '';

  profileSettings =
    {
      name ? "default",
      profile,
      named ? true,
      outputDir,
      workspaceDir,
    }:
    let
      mergedSettings =
        lib.recursiveUpdate
          (
            {
              # Preserve the previous effective effort when upgrading the flagship model.
              agent.reasoning_effort = "medium";
              agent.environment_hint =
                (outputEnvironmentHint {
                  inherit outputDir workspaceDir;
                })
                + optionalString (named && profileNativeJobs profile != { }) ''
                  Native scheduled jobs belong to this profile, never to individual chat users.
                  Read $HERMES_HOME/native-cron-contract.json for declared job names and constraints.
                  Keep one private (0600) $HERMES_HOME/native-cron-policy.json for this profile.
                  Use {"schema_version":2,"jobs":{"<job-name>":{"status":"unconfigured"}}}.
                  Each job has status unconfigured (inactive, needs setup), disabled (inactive,
                  no setup reminder for this job), or enabled (requires valid schedule and deliver).
                  Bootstrap fills missing declared entries as unconfigured; deleting an entry resets
                  only that job, deleting the policy resets all declared jobs. Never use a global mute.
                  Keep dormant entries when declarations disappear. Invalid enabled entries stay
                  enabled in policy but inactive with an error; repair them without inventing choices.
                  Schedules must be recurring
                  "every <duration>" or cron expressions. Telegram destinations are explicit
                  "telegram:<chat-id>[:<topic-id>]" values; local-only jobs use "local".
                  Ask for missing schedules, destinations, and workflow preferences. Chat access
                  does not imply scheduling ownership or a delivery destination. Family profiles
                  also have one job set, using individual preferences as agent-managed inputs.
                  Store workflow config in $HERMES_HOME/job-config/ and state in $HERMES_HOME/state/.
                  Do not create per-user cron policies or copy settings between users automatically.
                  Coordinate every read-modify-write with bootstrap: open the stable private
                  $HERMES_HOME/native-cron-policy.json.lock (0600, never delete it), take an exclusive
                  flock before reading the latest policy, preserve other entries, fsync a same-directory
                  0600 temporary file and atomically replace the policy before releasing the lock.
                  The host watcher applies it without restarting this assistant. Bootstrap migrates
                  schema 1 once, preserving existing choices; do not create new legacy policies.
                  Check native-cron-status.json and native cron readback before claiming success.
                  needs-configuration can coexist with active valid enabled jobs; reminders list only
                  unconfigured jobs. Job names are the base names in the contract.
                '';
              gateway.media_delivery_allow_dirs = [
                "/workspace"
                cfg.outputContainerPath
                outputDir
                workspaceDir
              ];
              model =
                if profile ? modelSettings && profile.modelSettings != null then
                  profile.modelSettings
                else
                  cfg.modelSettings;
              terminal = terminalSettingsFor {
                inherit
                  name
                  outputDir
                  workspaceDir
                  profile
                  ;
                allowNetwork = profileAllowNetwork profile;
              };
            }
            // optionalAttrs (cfg.web != { }) { inherit (cfg) web; }
            // optionalAttrs named {
              kanban.dispatch_in_gateway = false;
            }
          )
          (
            optionalAttrs profile.telegram.enable {
              telegram = {
                allow_from = profile.telegram.allowFrom;
              }
              // optionalAttrs named {
                enabled = true;
              };
            }
            // optionalAttrs (cfg.honcho.enable && profile.honcho.enable) {
              memory.provider = "honcho";
            }
            // builtins.removeAttrs (profileSettingsWithExtraToolsets profile) [ "terminal" ]
          );
    in
    if profileHasDeclarativeSkills profile then
      lib.recursiveUpdate mergedSettings {
        skills.external_dirs = (mergedSettings.skills.external_dirs or [ ]) ++ [
          (toString (profileDeclarativeSkillsDir name profile))
        ];
      }
    else
      mergedSettings;

  profileConfig =
    name: profile:
    yamlFormat.generate "hermes-${name}-config.yaml" (profileSettings {
      inherit name profile;
      outputDir = profileOutputDir name profile;
      workspaceDir = profileWorkspaceDir name;
    });

  defaultProfileConfig = yamlFormat.generate "hermes-default-config.yaml" (profileSettings {
    profile = effectiveDefaultProfile;
    named = false;
    outputDir = defaultOutputDir;
    workspaceDir = defaultWorkspaceDir;
  });

  sharedCodexAuthMarker = pkgs.writeText "hermes-shared-codex-auth" ''
    Managed named profiles share ${hermesHomeDir}/auth.json and ${hermesHomeDir}/auth.lock.
  '';

  profileHermesCronCli =
    name:
    pkgs.writeShellScript "hermes-${name}-cron-cli" ''
      exec ${config.services.hermes-agent.package}/bin/hermes -p ${lib.escapeShellArg name} "$@"
    '';

  profileNativeScriptsDir = name: "${profileDir name}/scripts/profile/.nix-managed";

  profileNativeCronContract =
    name: profile:
    jsonFormat.generate "hermes-${name}-required-native-jobs.json" {
      schema_version = 1;
      managed_script_dir = profileNativeScriptsDir name;
      jobs = mapAttrsToList (jobName: job: {
        name = jobName;
        inherit (job) skills;
        script = "${profileNativeScriptsDir name}/${jobName}.sh";
        no_agent = job.noAgent;
        deliver_prefix = if job.deliver != null then job.deliver else job.deliverPrefix;
      }) (profileNativeJobs profile);
    };

  profileNativeCronReconcile =
    name: profile:
    pkgs.writeShellScript "hermes-${name}-native-cron-reconcile" ''
      set -euo pipefail
      exec ${config.services.hermes-agent.package.hermesVenv}/bin/python3 \
        ${nativeCronReconciler}/${pkgs.python3.sitePackages}/hermes_native_cron_reconciler.py \
        --native-contract \
        --bootstrap \
        --contract ${profileNativeCronContract name profile} \
        --policy ${lib.escapeShellArg "${profileDir name}/native-cron-policy.json"} \
        --status-file ${lib.escapeShellArg "${profileDir name}/native-cron-status.json"} \
        --hermes ${profileHermesCronCli name}
    '';

  profileRepositoryRegistry =
    name: profile:
    let
      selection = profile.serviceIntegrations.repository-sync or false;
      selectedJobs = builtins.attrNames (
        filterAttrs (
          job: _:
          (profileCapabilityClosure profile).provenance."cron:${job}" == "serviceIntegration:repository-sync"
        ) (profileNativeJobs profile)
      );
    in
    import ./capabilities/repository-sync-registry.nix {
      inherit
        lib
        pkgs
        name
        selectedJobs
        ;
      settings =
        if builtins.isBool selection || !integrationEnabled selection || selectedJobs == [ ] then
          { }
        else
          selection.settings;
      profileHome = profileDir name;
      workspaceDir = profileWorkspaceDir name;
      identityFile = if profileHasSsh profile then profileSshRuntimeKeyPath name else null;
    };

  profileNativeScriptsActivation =
    name: profile:
    let
      scriptsDir = profileNativeScriptsDir name;
    in
    ''
      # Upstream requires resolved cron scripts inside HERMES_HOME/scripts.
      # Publish regular files, not store symlinks; retain old settings and files.
      for directory in ${profileDir name}/job-config ${profileDir name}/state ${profileDir name}/scripts/profile ${scriptsDir}; do
        if [ -L "$directory" ]; then
          echo "error: refusing symlink in profile job directory: $directory" >&2
          exit 1
        fi
        ${pkgs.coreutils}/bin/install -d -o ${cfg.user} -g ${cfg.group} -m 0700 "$directory"
      done
      ${concatStringsSep "\n" (
        mapAttrsToList (scriptName: job: ''
          ${pkgs.coreutils}/bin/install -T -o ${cfg.user} -g ${cfg.group} -m 0500 \
            ${job.script} ${scriptsDir}/${scriptName}.sh
        '') (profileNativeJobs profile)
      )}
    '';

  profileGatewayServices = mapAttrs' (
    name: profile:
    nameValuePair "hermes-agent-${name}" {
      description = "Hermes Agent Gateway (${name} profile)";
      wantedBy = [ "multi-user.target" ];
      requires = [ "hermes-rootless-docker-ready.service" ] ++ cfg.gatewayServiceDependencies;
      wants = [ "network-online.target" ];
      after = [
        "network-online.target"
        "hermes-rootless-docker-ready.service"
      ]
      ++ cfg.gatewayServiceDependencies;
      path =
        gatewayRuntimePackages
        ++ (profileCapabilityClosure profile).runtimePackages
        ++ [ nativeCronReconciler ];
      environment = {
        DOCKER_HOST = "unix://${dockerSocket}";
        HOME = cfg.stateDir;
        HERMES_HOME = hermesHomeDir;
        HERMES_CWD = cfg.workingDirectory;
        HERMES_MANAGED = "true";
        XDG_RUNTIME_DIR = runtimeDir;
      }
      // profileServiceEnvironment name profile
      // {
        HERMES_CAPABILITY_MANIFEST = toString (profileCapabilityManifestFile name profile);
      }
      // {
        HERMES_NATIVE_CRON_CONTRACT = toString (profileNativeCronContract name profile);
        HERMES_NATIVE_CRON_POLICY = "${profileDir name}/native-cron-policy.json";
        HERMES_NATIVE_CRON_CLI = toString (profileHermesCronCli name);
      };
      preStart = profileDockerCleanupScript name profile (profileOutputDir name profile) (
        profileWorkspaceDir name
      );
      serviceConfig = {
        User = config.services.hermes-agent.user;
        Group = config.services.hermes-agent.group;
        WorkingDirectory = cfg.workingDirectory;
        ExecStart = profileGatewayStartScript name profile;
        ExecStartPre = [ (profileNativeCronReconcile name profile) ];
        Restart = config.services.hermes-agent.restart;
        RestartSec = config.services.hermes-agent.restartSec;
        UMask = "0007";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = false;
        ReadWritePaths = [
          cfg.stateDir
          cfg.workingDirectory
        ];
        BindPaths = [
          "${profileWorkspaceDir name}:/workspace"
          "${profileOutputDir name profile}:${cfg.outputContainerPath}"
        ];
        PrivateTmp = true;
      }
      // optionalAttrs (profile.environmentFiles != [ ]) {
        EnvironmentFile = map toString profile.environmentFiles;
      };
      restartTriggers = [
        (profileConfig name profile)
      ]
      ++ optional cfg.sharedCodexAuth.enable sharedCodexAuthMarker
      ++ profileRestartTriggers name profile;
    }
  ) enabledProfiles;

  profileNativeCronServices = mapAttrs' (
    name: profile:
    let
      gateway = profileGatewayServices."hermes-agent-${name}";
    in
    nameValuePair "hermes-native-cron-${name}" {
      description = "Apply Hermes native job policy (${name} profile)";
      requires = [ "hermes-agent-${name}.service" ];
      after = [ "hermes-agent-${name}.service" ];
      inherit (gateway) environment path;
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.workingDirectory;
        ExecStart = profileNativeCronReconcile name profile;
        UMask = "0007";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = false;
        ReadWritePaths = [
          cfg.stateDir
          cfg.workingDirectory
        ];
        PrivateTmp = true;
      }
      // optionalAttrs (profile.environmentFiles != [ ]) {
        EnvironmentFile = map toString profile.environmentFiles;
      };
    }
  ) (filterAttrs (_: profile: profileNativeJobs profile != { }) enabledProfiles);

  profileNativeCronPaths = mapAttrs' (
    name: _profile:
    nameValuePair "hermes-native-cron-${name}" {
      description = "Watch Hermes native job policy (${name} profile)";
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        PathChanged = "${profileDir name}/native-cron-policy.json";
        Unit = "hermes-native-cron-${name}.service";
      };
    }
  ) (filterAttrs (_: profile: profileNativeJobs profile != { }) enabledProfiles);

  jobReminderProfiles = filterAttrs (
    _: profile:
    profile.jobSetupReminders.enable
    && profileHasTelegramToken profile
    && profileNativeJobs profile != { }
  ) enabledProfiles;

  profileNativeCronReminderServices = mapAttrs' (
    name: profile:
    let
      base = profileNativeCronServices."hermes-native-cron-${name}";
    in
    nameValuePair "hermes-job-setup-reminder-${name}" (
      base
      // {
        description = "Remind about unconfigured Hermes jobs (${name} profile)";
        # Telegram delivery works even if the gateway itself is unavailable.
        requires = [ ];
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        serviceConfig = base.serviceConfig // {
          TimeoutStartSec = "2min";
          ExecStart = pkgs.writeShellScript "hermes-job-setup-reminder-${name}" ''
            set -euo pipefail
            ${telegramTokenLoad profile}
            exec ${nativeCronReconciler}/bin/hermes-native-cron-reconciler \
              --remind --profile ${lib.escapeShellArg name} \
              ${
                concatStringsSep " " (
                  map (target: "--reminder-target ${lib.escapeShellArg target}") (
                    lib.unique profile.jobSetupReminders.targets
                  )
                )
              } \
              --contract ${profileNativeCronContract name profile} \
              --policy ${lib.escapeShellArg "${profileDir name}/native-cron-policy.json"} \
              --status-file ${lib.escapeShellArg "${profileDir name}/native-cron-status.json"} \
              --hermes ${profileHermesCronCli name}
          '';
        };
      }
    )
  ) jobReminderProfiles;

  profileNativeCronReminderTimers = mapAttrs' (
    name: _profile:
    nameValuePair "hermes-job-setup-reminder-${name}" {
      description = "Daily Hermes job setup reminder (${name} profile)";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "daily";
        RandomizedDelaySec = "2min";
        Persistent = false;
        Unit = "hermes-job-setup-reminder-${name}.service";
      };
    }
  ) jobReminderProfiles;

  profileIdentityFileSetup = profileHome: dockerHome: ''
    canonical=${lib.escapeShellArg "${profileHome}/SOUL.md"}
    mirror=${lib.escapeShellArg "${dockerHome}/.hermes/SOUL.md"}
    if [ -L "$canonical" ] && [ -e "$canonical" ]; then
      tmp="$(${pkgs.coreutils}/bin/mktemp "$canonical.tmp.XXXXXX")"
      ${pkgs.coreutils}/bin/cp -L "$canonical" "$tmp"
      ${pkgs.coreutils}/bin/rm -f "$canonical"
      ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0660 "$tmp" "$canonical"
      ${pkgs.coreutils}/bin/rm -f "$tmp"
    elif [ ! -e "$canonical" ] && [ -f "$mirror" ]; then
      ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0660 "$mirror" "$canonical"
    elif [ ! -e "$canonical" ]; then
      ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0660 /dev/null "$canonical"
    fi

    for name in USER.md MEMORY.md; do
      canonical=${lib.escapeShellArg "${profileHome}/memories"}/"$name"
      legacy=${lib.escapeShellArg profileHome}/"$name"
      mirror=${lib.escapeShellArg "${dockerHome}/.hermes"}/"$name"
      if [ -L "$canonical" ] && [ -e "$canonical" ]; then
        tmp="$(${pkgs.coreutils}/bin/mktemp "$canonical.tmp.XXXXXX")"
        ${pkgs.coreutils}/bin/cp -L "$canonical" "$tmp"
        ${pkgs.coreutils}/bin/rm -f "$canonical"
        ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0660 "$tmp" "$canonical"
        ${pkgs.coreutils}/bin/rm -f "$tmp"
      elif [ ! -e "$canonical" ] && [ -f "$mirror" ]; then
        ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0660 "$mirror" "$canonical"
      fi
      if [ -L "$legacy" ]; then
        ${pkgs.coreutils}/bin/rm -f "$legacy"
      fi
    done
  '';

  profileCapabilityManifestFile =
    name: profile:
    let
      closure = profileCapabilityClosure profile;
      nativeJobs = profileNativeJobs profile;
      provenance = filterAttrs (
        artifact: _provider:
        !lib.hasPrefix "cron:" artifact
        || !builtins.hasAttr (lib.removePrefix "cron:" artifact) closure.nativeJobs
        || builtins.hasAttr (lib.removePrefix "cron:" artifact) nativeJobs
      ) closure.provenance;
    in
    jsonFormat.generate "hermes-${name}-capabilities.json" {
      schemaVersion = 1;
      profile = name;
      explicitRoots = profileCapabilityRoots profile;
      closure = closure.orderedIds;
      skills = builtins.attrNames closure.skills;
      inherit (closure) toolsets;
      nativeJobs = builtins.attrNames nativeJobs;
      runtimePackages = map toString closure.runtimePackages;
      hermesConfigKeys = builtins.attrNames closure.hermesConfig;
      inherit (closure) requiredExternalServices;
      inherit (closure) requiredHermesCapabilities;
      inherit (closure) safety;
      inherit provenance;
    };

  profileActivation = name: profile: ''
    ${pkgs.coreutils}/bin/install -d -o ${cfg.user} -g ${cfg.group} -m 0750 ${profileDir name}
    ${pkgs.coreutils}/bin/install -d -o ${cfg.user} -g ${cfg.group} -m 0770 \
      ${profileDir name}/cron \
      ${profileDir name}/sessions \
      ${profileDir name}/logs \
      ${profileDir name}/memories \
      ${profileDir name}/plugins \
      ${profileDir name}/scripts \
      ${concatStringsSep " " (profileManagedDirs name profile)} \
      ${profileOutputDir name profile}
    ${optionalString (profileHasDeclarativeSoul profile) ''
      ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0660 \
        ${profileSoulFile name profile} ${profileDir name}/SOUL.md
    ''}
    ${profileIdentityFileSetup (profileDir name) (profileDockerHomeDir name)}
    ${profileDeclarativeSkillCollisionCheck name profile}
    ${profileNativeScriptsActivation name profile}
    ${(profileRepositoryRegistry name profile).activation}
    ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0640 \
      ${profileNativeCronContract name profile} ${profileDir name}/native-cron-contract.json
    ${optionalString (profileHasSsh profile) ''
      ${pkgs.coreutils}/bin/install -d -o ${cfg.user} -g ${cfg.group} -m 0700 ${profileDir name}/.ssh
      ${pkgs.coreutils}/bin/ln -sfn ${profileSshKeyPath profile} ${profileDir name}/.ssh/id_ed25519
      ${pkgs.coreutils}/bin/chown -h ${cfg.user}:${cfg.group} ${profileDir name}/.ssh/id_ed25519
    ''}
    ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0640 \
      ${profileConfig name profile} ${profileDir name}/config.yaml
    ${optionalString cfg.sharedCodexAuth.enable ''
      # Named profiles share the writable OpenAI Codex store. Otherwise a token
      # refresh is written into one profile and shadows the global token.
      profile_auth=${profileDir name}/auth.json
      if [ ! -L "$profile_auth" ] && [ -s "$profile_auth" ]; then
        backup=${profileDir name}/auth.json.pre-shared-codex
        if [ ! -e "$backup" ]; then
          ${pkgs.coreutils}/bin/cp -p "$profile_auth" "$backup"
        fi
      fi
      profile_auth_tmp="$profile_auth.tmp.$$"
      ${pkgs.coreutils}/bin/rm -f "$profile_auth_tmp"
      ${pkgs.coreutils}/bin/ln -s ../../auth.json "$profile_auth_tmp"
      ${pkgs.coreutils}/bin/chown -h ${cfg.user}:${cfg.group} "$profile_auth_tmp"
      ${pkgs.coreutils}/bin/mv -Tf "$profile_auth_tmp" "$profile_auth"

      # Share the lock so read-refresh-write is serialized across gateways.
      if [ ! -e ${hermesHomeDir}/auth.lock ]; then
        ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0600 \
          /dev/null ${hermesHomeDir}/auth.lock
      fi
      profile_auth_lock=${profileDir name}/auth.lock
      profile_auth_lock_tmp="$profile_auth_lock.tmp.$$"
      ${pkgs.coreutils}/bin/rm -f "$profile_auth_lock_tmp"
      ${pkgs.coreutils}/bin/ln -s ../../auth.lock "$profile_auth_lock_tmp"
      ${pkgs.coreutils}/bin/chown -h ${cfg.user}:${cfg.group} "$profile_auth_lock_tmp"
      ${pkgs.coreutils}/bin/mv -Tf "$profile_auth_lock_tmp" "$profile_auth_lock"
    ''}
  '';

  defaultProfileActivation = ''
    ${pkgs.coreutils}/bin/install -d -o ${cfg.user} -g ${cfg.group} -m 0770 \
      ${concatStringsSep " " defaultManagedDirs}
    ${profileIdentityFileSetup hermesHomeDir defaultDockerHomeDir}
    ${optionalString (profileHasSsh effectiveDefaultProfile) ''
      ${pkgs.coreutils}/bin/install -d -o ${cfg.user} -g ${cfg.group} -m 0700 ${hermesHomeDir}/.ssh
      ${pkgs.coreutils}/bin/ln -sfn ${profileSshKeyPath effectiveDefaultProfile} ${hermesHomeDir}/.ssh/id_ed25519
      ${pkgs.coreutils}/bin/chown -h ${cfg.user}:${cfg.group} ${hermesHomeDir}/.ssh/id_ed25519
    ''}
    ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0640 \
      ${defaultProfileConfig} ${hermesHomeDir}/config.yaml
  '';

  profileServiceEnvironment =
    name: profile:
    cfg.environment
    // profile.environment
    // optionalAttrs (profileHasSsh profile) {
      HERMES_SSH_PRIVATE_KEY_FILE = profileSshKeyPath profile;
      GIT_SSH_COMMAND = profileGitSshCommand name profile;
    };

  telegramTokenLoad =
    profile:
    optionalString (profileHasTelegramToken profile) ''
      TELEGRAM_BOT_TOKEN=
      if ! IFS= read -r TELEGRAM_BOT_TOKEN < ${lib.escapeShellArg (profileTelegramTokenPath profile)}; then
        if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
          echo "Telegram bot token file is empty or unreadable." >&2
          exit 1
        fi
      fi
      if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
        echo "Telegram bot token file is empty or unreadable." >&2
        exit 1
      fi
      export TELEGRAM_BOT_TOKEN
    '';

  profileGatewayStartScript =
    name: profile:
    pkgs.writeShellScript "hermes-agent-${name}-start" ''
      set -euo pipefail
      ${telegramTokenLoad profile}
      exec ${config.services.hermes-agent.package}/bin/hermes -p ${lib.escapeShellArg name} gateway run --replace ${lib.escapeShellArgs config.services.hermes-agent.extraArgs}
    '';

  defaultGatewayStartScript = pkgs.writeShellScript "hermes-agent-default-start" ''
    set -euo pipefail
    ${telegramTokenLoad effectiveDefaultProfile}
    exec ${config.services.hermes-agent.package}/bin/hermes gateway ${lib.escapeShellArgs config.services.hermes-agent.extraArgs}
  '';

  profileRestartTriggers =
    name: profile:
    optional (profileHasTelegramToken profile) (profileTelegramTokenPath profile)
    ++ optional (profileHasSsh profile) (profileSshKeyPath profile)
    ++ optional (profileHasDeclarativeSoul profile) (profileSoulFile name profile)
    ++ optional (profileHasDeclarativeSkills profile) (profileDeclarativeSkillsDir name profile)
    ++ optional (profileHasDeclarativeSkills profile) (profileManagedSkillManifest name profile)
    ++ [ (profileCapabilityManifestFile name profile) ]
    ++ map toString profile.environmentFiles;

  profileDockerCleanupScript = name: profile: outputDir: workspaceDir: ''
    ${optionalString (profileHasExternalServices profile) ''
      runtime_services_dir=${lib.escapeShellArg (profileServicesRuntimeDir name)}
      ${pkgs.coreutils}/bin/install -d -m 0700 "$runtime_services_dir"
      declared_services=${lib.escapeShellArg (concatStringsSep " " (builtins.attrNames profile.externalServices))}
      for stale_service_dir in "$runtime_services_dir"/*; do
        [ -d "$stale_service_dir" ] || continue
        stale_service_name="''${stale_service_dir##*/}"
        case " $declared_services " in
          *" $stale_service_name "*) ;;
          *) ${pkgs.coreutils}/bin/rm -rf -- "$stale_service_dir" ;;
        esac
      done
      ${concatStringsSep "\n" (
        mapAttrsToList (serviceName: service: ''
          runtime_service_dir="$runtime_services_dir/${serviceName}"
          runtime_endpoint="$runtime_service_dir/endpoint"
          runtime_endpoint_tmp="$runtime_endpoint.$$"
          ${pkgs.coreutils}/bin/install -d -m 0700 "$runtime_service_dir"
          declared_fields=${
            lib.escapeShellArg (
              concatStringsSep " " (
                [
                  "endpoint"
                ]
                ++ builtins.attrNames service.textFiles
                ++ builtins.attrNames service.credentialFiles
              )
            )
          }
          for stale_field_path in "$runtime_service_dir"/*; do
            [ -f "$stale_field_path" ] || continue
            stale_field_name="''${stale_field_path##*/}"
            case " $declared_fields " in
              *" $stale_field_name "*) ;;
              *) ${pkgs.coreutils}/bin/rm -f -- "$stale_field_path" ;;
            esac
          done
          ${pkgs.coreutils}/bin/printf '%s\n' ${lib.escapeShellArg service.endpoint} > "$runtime_endpoint_tmp"
          ${pkgs.coreutils}/bin/chmod 0400 "$runtime_endpoint_tmp"
          ${pkgs.coreutils}/bin/mv -f "$runtime_endpoint_tmp" "$runtime_endpoint"
          ${concatStringsSep "\n" (
            mapAttrsToList (fieldName: value: ''
              runtime_text_field="$runtime_service_dir/${fieldName}"
              runtime_text_field_tmp="$runtime_text_field.$$"
              ${pkgs.coreutils}/bin/printf '%s\n' ${lib.escapeShellArg value} > "$runtime_text_field_tmp"
              ${pkgs.coreutils}/bin/chmod 0400 "$runtime_text_field_tmp"
              ${pkgs.coreutils}/bin/mv -f "$runtime_text_field_tmp" "$runtime_text_field"
            '') service.textFiles
          )}
          ${concatStringsSep "\n" (
            mapAttrsToList (fieldName: source: ''
              runtime_credential_field="$runtime_service_dir/${fieldName}"
              runtime_credential_field_tmp="$runtime_credential_field.$$"
              ${pkgs.coreutils}/bin/install -m 0400 ${lib.escapeShellArg (toString source)} "$runtime_credential_field_tmp"
              ${pkgs.coreutils}/bin/mv -f "$runtime_credential_field_tmp" "$runtime_credential_field"
            '') service.credentialFiles
          )}
        '') profile.externalServices
      )}
    ''}
      ${optionalString (profileHasSsh profile) ''
        runtime_ssh_dir=${lib.escapeShellArg (profileSshRuntimeDir name)}
        runtime_ssh_key=${lib.escapeShellArg (profileSshRuntimeKeyPath name)}
        runtime_ssh_config="$runtime_ssh_dir/config"
        runtime_ssh_config_tmp="$runtime_ssh_dir/config.$$"
        ${pkgs.coreutils}/bin/install -d -m 0700 "$runtime_ssh_dir"
        ${pkgs.coreutils}/bin/install -m 0400 ${lib.escapeShellArg (profileSshKeyPath profile)} "$runtime_ssh_key"
        ${pkgs.coreutils}/bin/cat > "$runtime_ssh_config_tmp" <<'EOF'
        Host *
          IdentityFile /root/.ssh/id_ed25519
          IdentitiesOnly yes
          StrictHostKeyChecking accept-new
          UserKnownHostsFile /tmp/hermes-known_hosts
        EOF
        ${pkgs.coreutils}/bin/chmod 0400 "$runtime_ssh_config_tmp"
        ${pkgs.coreutils}/bin/mv -f "$runtime_ssh_config_tmp" "$runtime_ssh_config"
      ''}

      expected_config_hash=${
        lib.escapeShellArg (
          profileDockerConfigHash name (terminalBaseSettingsFor {
            inherit
              name
              outputDir
              workspaceDir
              profile
              ;
            allowNetwork = profileAllowNetwork profile;
          })
        )
      }
      fingerprint_dir=${lib.escapeShellArg "${if name == "default" then hermesHomeDir else profileDir name}/docker-fingerprints"}
      fingerprint_file=${lib.escapeShellArg "${if name == "default" then hermesHomeDir else profileDir name}/docker-fingerprints/${name}.fingerprint"}
      secret_fingerprint="${optionalString (profileHasSsh profile) "$(${pkgs.coreutils}/bin/stat -Lc '%d:%i:%Y:%s' ${lib.escapeShellArg (profileSshKeyPath profile)} 2>/dev/null || true)"}"
      current_fingerprint="$expected_config_hash:$secret_fingerprint"
      previous_fingerprint="$(
        ${pkgs.coreutils}/bin/cat "$fingerprint_file" 2>/dev/null || true
      )"
      # A fresh container, without credentials or workspace copies, proves the
      # actual interpreter, imports and native dependencies before gateway start.
      ${pkgs.docker}/bin/docker run --rm --network=none --read-only \
        --entrypoint ${lib.escapeShellArg (builtins.head (profileSandbox name profile).readiness)} \
        ${
          lib.concatMapStringsSep " " (
            volume: "-v ${lib.escapeShellArg volume}"
          ) (profileSandbox name profile).volumes
        } \
        ${
          lib.escapeShellArg
            (terminalBaseSettingsFor {
              inherit
                name
                profile
                outputDir
                workspaceDir
                ;
              allowNetwork = profileAllowNetwork profile;
            }).docker_image
        } \
        ${lib.escapeShellArgs (builtins.tail (profileSandbox name profile).readiness)}
      containers="$(
        ${pkgs.docker}/bin/docker ps -aq \
          --filter ${lib.escapeShellArg "label=hermes-agent=1"} \
          --filter ${lib.escapeShellArg "label=hermes-profile=${name}"}
      )"
      for container in $containers; do
        container_config_hash="$(
          ${pkgs.docker}/bin/docker inspect \
            --format ${lib.escapeShellArg ''{{ index .Config.Labels "hermes-config-hash" }}''} \
            "$container"
        )"
        if [ "$container_config_hash" != "$expected_config_hash" ] || [ "$current_fingerprint" != "$previous_fingerprint" ]; then
          ${pkgs.docker}/bin/docker rm -f "$container" >/dev/null
        fi
      done
      ${pkgs.coreutils}/bin/install -d -m 0700 "$fingerprint_dir"
      printf '%s\n' "$current_fingerprint" > "$fingerprint_file"
  '';

  dashboardGeneratedEnvContent =
    let
      lines =
        optional (cfg.dashboard.publicUrl != null) "HERMES_DASHBOARD_PUBLIC_URL=${cfg.dashboard.publicUrl}"
        ++ optional cfg.dashboard.oidc.enable "HERMES_DASHBOARD_OIDC_ISSUER=${cfg.dashboard.oidc.issuer}"
        ++ optional cfg.dashboard.oidc.enable "HERMES_DASHBOARD_OIDC_SCOPES=${cfg.dashboard.oidc.scopes}"
        ++ mapAttrsToList (name: value: "${name}=${value}") cfg.dashboard.environment;
    in
    concatStringsSep "\n" lines + optionalString (lines != [ ]) "\n";

  dashboardHasGeneratedEnv =
    cfg.dashboard.publicUrl != null || cfg.dashboard.oidc.enable || cfg.dashboard.environment != { };

  dashboardGeneratedEnvFile = pkgs.writeText "hermes-dashboard-env" dashboardGeneratedEnvContent;

  dashboardEnvironmentFiles = map toString (
    optional dashboardHasGeneratedEnv dashboardGeneratedEnvFile ++ cfg.dashboard.environmentFiles
  );

  dashboardHasEnv = dashboardEnvironmentFiles != [ ];

  telegramOptions = {
    enable = mkOption {
      type = types.bool;
      default = true;
      description = "Whether this Hermes profile should configure Telegram gateway settings.";
    };

    allowFrom = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Telegram user IDs allowed to talk to this profile.";
      example = [
        "243945372"
        "248449978"
      ];
    };

    botTokenFile = mkOption {
      type = types.nullOr pathType;
      default = null;
      description = "Path to the Telegram bot token file for this profile.";
      example = "/run/secrets/hermes/telegram/software-developer/credential";
    };
  };

  sshOptions = {
    privateKeyFile = mkOption {
      type = types.nullOr pathType;
      default = null;
      description = "Path to this profile's SSH private key for Git and deploy operations.";
      example = "/run/secrets/hermes/ssh/software-developer/private-key";
    };

    containerPath = mkOption {
      type = types.str;
      default = "/root/.ssh/id_ed25519";
      description = "Path where the SSH private key is mounted inside this profile's terminal sandboxes.";
    };
  };

  skillArtifactOptions = {
    category = mkOption {
      type = types.str;
      default = "repo-managed";
      description = "Single safe category directory for this repo-managed Hermes skill.";
    };

    content = mkOption {
      type = types.nullOr types.lines;
      default = null;
      description = "Inline SKILL.md content. Exactly one of content or source is required.";
    };

    source = mkOption {
      type = types.nullOr pathType;
      default = null;
      description = "Canonical skill directory containing SKILL.md and optional supporting files. Exactly one of source or content is required.";
    };
  };

  skillArtifactType = types.submodule (
    { options, ... }: {
      options = skillArtifactOptions // {
        declarationFiles = mkOption {
          type = types.listOf types.str;
          readOnly = true;
          internal = true;
          description = "Effective Nix definitions of this artifact, not its installation path.";
        };
      };
      config.declarationFiles = unique (
        map (definition: definition.file) (
          options.source.definitionsWithLocations ++ options.content.definitionsWithLocations
        )
      );
    }
  );

  serviceIntegrationSelectionType = types.either types.bool (
    types.submodule {
      options = {
        enable = mkOption {
          type = types.bool;
          default = true;
          description = "Whether this explicit directional integration selection is active.";
        };
        instances = mkOption {
          type = types.attrsOf types.bool;
          default = { };
          description = "Optional provider-owned native job instances selected for this integration.";
        };
        settings = mkOption {
          type = types.attrsOf types.anything;
          default = { };
          description = "Bounded provider-owned interaction policy; endpoints, credentials, packages, paths, and commands are forbidden here.";
        };
      };
    }
  );

  nativeJobArtifactType = types.submodule (
    { options, ... }: {
      options = {
        declarationFiles = mkOption {
          type = types.listOf types.str;
          readOnly = true;
          internal = true;
          description = "Effective Nix definitions of the immutable job launcher.";
        };
        script = mkOption {
          type = types.package;
          description = "Package-owned stateless native Hermes job launcher.";
        };
        noAgent = mkOption {
          type = types.bool;
          description = "Whether Hermes executes the launcher without starting an agent session.";
        };
        skills = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = "Skills loaded by the native Hermes job when an agent is used.";
        };
        deliver = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Exact native Hermes delivery target required by this job.";
        };
        deliverPrefix = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Allowed native Hermes delivery-target prefix required by this job.";
        };
      };
      config.declarationFiles = unique (
        map (definition: definition.file) options.script.definitionsWithLocations
      );
    }
  );

  externalServiceOptions = {
    endpoint = mkOption {
      type = types.str;
      description = "Service endpoint written to the profile-scoped command-time endpoint file.";
      example = "https://service.example.com";
    };

    textFiles = mkOption {
      type = types.attrsOf types.str;
      default = { };
      description = "Public non-secret values written to named profile-scoped command-time files. The field name endpoint is reserved.";
      example = {
        username = "service-user";
        topics = "alerts,events";
      };
    };

    credentialFiles = mkOption {
      type = types.attrsOf pathType;
      default = { };
      description = "Secret source paths copied to named profile-scoped command-time files. Use credentialFiles.credential for the conventional credential file; the field name endpoint is reserved.";
      example = {
        credential = "/run/secrets/service/credential";
        username = "/run/secrets/service/username";
      };
    };

    capabilities = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Explicit role-oriented capabilities selected for this configured service instance. Connectivity alone starts no behavior.";
    };

    scope = mkOption {
      type = types.submodule {
        options = {
          include = mkOption {
            type = types.nullOr (types.listOf types.str);
            default = null;
            description = "Positive resource allowlist. Mutating capabilities should use this mode.";
          };
          exclude = mkOption {
            type = types.nullOr (types.listOf types.str);
            default = null;
            description = "Deliberately broad resource selector excluding named resources.";
          };
          acknowledgeFutureResources = mkOption {
            type = types.bool;
            default = false;
            description = "Required acknowledgement that exclude mode automatically includes future resources.";
          };
        };
      };
      default = { };
      description = "Mutually exclusive include/exclude resource selector for this service instance.";
    };
  };

  externalServicesType = types.attrsOf (types.submodule { options = externalServiceOptions; });

  externalServiceFieldsValid =
    service:
    let
      textNames = builtins.attrNames service.textFiles;
      credentialNames = builtins.attrNames service.credentialFiles;
      allNames = textNames ++ credentialNames;
    in
    builtins.all (fieldName: builtins.match "^[a-z0-9][a-z0-9._-]*$" fieldName != null) allNames
    && builtins.all (fieldName: fieldName != "endpoint") allNames
    && builtins.all (fieldName: !(builtins.elem fieldName credentialNames)) textNames;

  capabilityPackageType = types.submodule {
    options = {
      id = mkOption {
        type = types.str;
        description = "Canonical package identity generated by a capability constructor.";
      };
      kind = mkOption {
        type = types.enum [
          "agentSkill"
          "externalService"
          "serviceIntegration"
        ];
        description = "Capability package class.";
      };
      name = mkOption {
        type = types.str;
        description = "Safe logical capability name.";
      };
      requires = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Canonical package identities required transitively by this package.";
      };
      requiredExternalServices = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Configured profile-local external service instances required by this package.";
      };
      requiredHermesCapabilities = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Native Hermes facilities required by this package; these are validation requirements, not duplicate implementations.";
      };
      conflicts = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Package identities that form an unsupported combination with this package.";
      };
      targets = mkOption {
        type = types.listOf (
          types.enum [
            "hermes"
            "codex"
          ]
        );
        default = [ "hermes" ];
        description = "Harness targets supported by this package.";
      };
      allowedSettings = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Exact bounded interaction-policy keys accepted by this service-integration provider.";
      };
      safety = {
        credentialRequirements = mkOption {
          type = types.listOf (
            types.submodule {
              options = {
                service = mkOption { type = types.str; };
                field = mkOption { type = types.str; };
              };
            }
          );
          default = [ ];
          description = "Exact profile-local service credential fields required at command time.";
        };
        pathRequirements = mkOption {
          type = types.listOf (
            types.submodule {
              options = {
                path = mkOption { type = types.str; };
                access = mkOption {
                  type = types.enum [
                    "read"
                    "read-write"
                  ];
                };
              };
            }
          );
          default = [ ];
          description = "Provider-owned absolute or HERMES_HOME-relative paths and required access mode.";
        };
        writeScope = mkOption {
          type = types.enum [
            "none"
            "local-state"
            "external-system"
          ];
          default = "none";
        };
        confirmation = mkOption {
          type = types.enum [
            "not-required"
            "operator-required"
            "policy-preauthorized"
          ];
          default = "not-required";
        };
        verification = mkOption {
          type = types.enum [
            "not-required"
            "durable-readback-required"
            "external-readback-required"
          ];
          default = "not-required";
        };
      };
      safetyDeclared = mkOption {
        type = types.bool;
        internal = true;
        default = false;
      };
      provides = {
        skills = mkOption {
          type = types.attrsOf skillArtifactType;
          default = { };
          description = "Canonical managed skill directories or inline SKILL.md artifacts published by this package.";
        };
        toolsets = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = "Native Hermes toolsets added through the existing profile config interface.";
        };
        hermesConfig = mkOption {
          type = types.attrsOf types.anything;
          default = { };
          description = "Top-level Hermes config fragments. Multiple providers for one top-level key fail closure.";
        };
        managedArtifacts = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = "Additional canonical managed provider identities such as unit:name, timer:name, or cron:name.";
        };
        nativeJobs = mkOption {
          type = types.attrsOf nativeJobArtifactType;
          default = { };
          description = "Package-owned native Hermes job launchers and their immutable execution contracts.";
        };
        runtimePackages = mkOption {
          type = types.listOf types.package;
          default = [ ];
          description = "Exact executable packages added only to profiles selecting this capability.";
        };
      };
    };
  };

  skillArtifactValid =
    skill: (skill.content != null) != (skill.source != null) && capabilityLib.safeName skill.category;

  capabilityIdValid =
    id:
    builtins.match "^(agentSkill|serviceIntegration):[a-z0-9][a-z0-9._-]*$" id != null
    || builtins.match "^externalService:[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$" id != null;

  integrationSettingNameValid = name: builtins.match "^[a-z][A-Za-z0-9]*$" name != null;

  safetyMetadataValid =
    safety:
    builtins.all (
      requirement: capabilityLib.safeName requirement.service && capabilityLib.safeName requirement.field
    ) safety.credentialRequirements
    && builtins.all (
      requirement:
      (
        lib.hasPrefix "/" requirement.path || lib.hasPrefix "$HERMES_HOME/" requirement.path

      )
      && !(lib.hasInfix ".." requirement.path)
    ) safety.pathRequirements
    && (
      safety.writeScope == "none"
      || (safety.confirmation != "not-required" && safety.verification != "not-required")
    )
    && (safety.writeScope != "external-system" || safety.verification == "external-readback-required");

  capabilityPackageValid =
    id: package:
    package.id == id
    && capabilityIdValid id
    && capabilityLib.safeName package.name
    && builtins.all capabilityIdValid (package.requires ++ package.conflicts)
    && builtins.all capabilityLib.safeName package.requiredExternalServices
    && (package.kind == "serviceIntegration" || package.allowedSettings == [ ])
    && builtins.length package.allowedSettings == builtins.length (unique package.allowedSettings)
    && builtins.all integrationSettingNameValid package.allowedSettings
    && (package.kind != "serviceIntegration" || package.safetyDeclared)
    && safetyMetadataValid package.safety
    && builtins.all (
      requirement: builtins.elem requirement.service package.requiredExternalServices
    ) package.safety.credentialRequirements
    && builtins.all (
      capability: builtins.elem capability nativeHermesCapabilities
    ) package.requiredHermesCapabilities
    && builtins.all sandboxSkillNameValid (builtins.attrNames package.provides.skills)
    && builtins.all skillArtifactValid (builtins.attrValues package.provides.skills)
    && builtins.all capabilityLib.safeName package.provides.toolsets
    && builtins.all capabilityLib.safeName (builtins.attrNames package.provides.hermesConfig)
    && builtins.all capabilityLib.managedArtifactValid package.provides.managedArtifacts
    && builtins.all capabilityLib.safeName (builtins.attrNames package.provides.nativeJobs)
    && builtins.all (
      job:
      (job.deliver != null) != (job.deliverPrefix != null)
      && (job.deliver == "local" || job.deliverPrefix == "telegram:")
      && builtins.all capabilityLib.safeName job.skills
      && (!job.noAgent || job.skills == [ ])
    ) (builtins.attrValues package.provides.nativeJobs)
    && builtins.all lib.isDerivation package.provides.runtimePackages
    && (
      (package.kind == "agentSkill" && id == "agentSkill:${package.name}")
      || (package.kind == "serviceIntegration" && id == "serviceIntegration:${package.name}")
      || (
        package.kind == "externalService"
        && builtins.match "^externalService:[a-z0-9][a-z0-9._-]*:${package.name}$" id != null
      )
    );

  sandboxSkillNameValid =
    name:
    capabilityLib.safeName name
    && !(builtins.elem name [
      "bin"
      "checks.json"
      "readiness.py"
    ]);

  profileCapabilityContractValid =
    profile:
    let
      closure = profileCapabilityClosure profile;
      directSkills = builtins.attrNames profile.declarativeSkills;
      closureSkills = builtins.attrNames closure.skills;
      directConfig = builtins.attrNames profile.settings;
      closureConfig = builtins.attrNames closure.hermesConfig;
      services = builtins.attrNames profile.externalServices;
      nativeJobs = profileNativeJobs profile;
      nativeJobNames = builtins.attrNames nativeJobs;
      requestedNativeJobNames = concatLists (
        mapAttrsToList (_name: integrationNativeJobs) profile.serviceIntegrations
      );
      integrationInstanceNames = concatLists (
        mapAttrsToList (_name: integrationNativeJobs) profile.serviceIntegrations
      );
      integrationSelectionsValid = builtins.all (
        integrationName:
        let
          selection = profile.serviceIntegrations.${integrationName};
          providerId = "serviceIntegration:${integrationName}";
        in
        builtins.isBool selection
        || (
          builtins.hasAttr providerId cfg.capabilityPackages
          && builtins.all (
            settingName: builtins.elem settingName cfg.capabilityPackages.${providerId}.allowedSettings
          ) (builtins.attrNames selection.settings)
          && (
            !selection.enable
            || (
              let
                providerJobs = cfg.capabilityPackages.${providerId}.provides.nativeJobs;
                selectedInstances = builtins.attrNames selection.instances;
              in
              if providerJobs == { } then
                selectedInstances == [ ]
              else
                builtins.all (instanceName: builtins.hasAttr instanceName providerJobs) selectedInstances
            )
          )
        )
      ) (builtins.attrNames profile.serviceIntegrations);
    in
    builtins.length closure.orderedIds >= 0
    && builtins.all capabilityLib.safeName (builtins.attrNames profile.agentSkills)
    && builtins.all capabilityLib.safeName (builtins.attrNames profile.serviceIntegrations)
    && builtins.all capabilityLib.safeName services
    && builtins.all capabilityLib.safeName nativeJobNames
    && builtins.all capabilityLib.safeName integrationInstanceNames
    && integrationSelectionsValid
    && builtins.all (jobName: builtins.hasAttr jobName closure.nativeJobs) requestedNativeJobNames
    && builtins.all (service: builtins.elem service services) closure.requiredExternalServices
    && builtins.all (
      capability: builtins.elem capability nativeHermesCapabilities
    ) closure.requiredHermesCapabilities
    && builtins.all (name: !(builtins.elem name directSkills)) closureSkills
    && builtins.all sandboxSkillNameValid directSkills
    && builtins.all (name: !(builtins.elem name directConfig)) closureConfig
    && builtins.all skillArtifactValid (builtins.attrValues profile.declarativeSkills)
    && builtins.all (
      service:
      builtins.all capabilityLib.safeName service.capabilities
      && !(service.scope.include != null && service.scope.exclude != null)
      && (service.scope.exclude == null || service.scope.acknowledgeFutureResources)
    ) (builtins.attrValues profile.externalServices);

  honchoProfileOptions =
    { defaultAiPeer, defaultSessionStrategy }:
    {
      enable = mkOption {
        type = types.bool;
        default = false;
        description = "Whether this Hermes profile should use Honcho as active memory provider.";
      };

      aiPeer = mkOption {
        type = types.str;
        default = defaultAiPeer;
        description = "Honcho AI peer name for this Hermes profile.";
      };

      sessionStrategy = mkOption {
        type = types.enum [
          "per-session"
          "per-directory"
          "per-repo"
          "global"
        ];
        default = defaultSessionStrategy;
        description = "Honcho session resolution strategy for this Hermes profile.";
      };

      extraHostConfig = mkOption {
        type = types.attrsOf types.anything;
        default = { };
        description = "Additional host-level keys merged into this profile's Honcho host block.";
      };
    };

  invalidCapabilityPackageIds = builtins.filter (
    id: !(capabilityPackageValid id cfg.capabilityPackages.${id})
  ) (builtins.attrNames cfg.capabilityPackages);

  capabilityProfileOptions = types.submodule (
    { options, ... }: {
      options = {
        selectionDeclarationFiles = mkOption {
          type = types.listOf types.str;
          internal = true;
          readOnly = true;
          description = "Effective source declarations selecting this profile’s capabilities.";
        };
        agentSkills = mkOption {
          type = types.attrsOf types.bool;
          default = { };
          description = "Explicit portable skill or skill-bundle capability roots.";
        };
        externalServices = mkOption {
          type = externalServicesType;
          default = { };
          description = "Profile-scoped external-service capability roots and command-time files.";
        };
        serviceIntegrations = mkOption {
          type = types.attrsOf serviceIntegrationSelectionType;
          default = { };
          description = "Explicit directional integration capability roots.";
        };
      };
      config.selectionDeclarationFiles = unique (
        concatLists (
          map (option: map (definition: definition.file) option.definitionsWithLocations) [
            options.agentSkills
            options.externalServices
            options.serviceIntegrations
          ]
        )
      );
    }
  );

  profileOptions = types.submodule (
    { name, ... }:
    {
      options = {
        enable = mkOption {
          type = types.bool;
          default = true;
          description = "Whether to run this named Hermes profile gateway.";
        };

        telegram = mkOption {
          type = types.submodule { options = telegramOptions; };
          default = { };
          description = "Telegram gateway settings for this profile.";
        };

        jobSetupReminders.enable = mkOption {
          type = types.bool;
          default = cfg.jobSetupReminders.enable;
          description = "Remind about this profile's native job setup until configured or explicitly disabled with an empty policy.";
        };

        jobSetupReminders.targets = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = "All explicit setup-reminder chats as telegram:<chat-id>[:<topic-id>]. Shared profile readiness controls all recipients; these do not create per-user jobs.";
        };

        ssh = mkOption {
          type = types.submodule { options = sshOptions; };
          default = { };
          description = "SSH key settings for Git and deploy operations from this profile.";
        };

        modelSettings = mkOption {
          type = types.nullOr (types.attrsOf types.anything);
          default = null;
          description = "Optional model settings for this profile. Defaults to custom.services.hermes.modelSettings.";
        };

        terminalSettings = mkOption {
          type = types.attrsOf types.anything;
          default = { };
          description = "Terminal sandbox settings merged over custom.services.hermes.terminalSettings.";
        };

        extraDockerVolumes = mkOption {
          type = types.listOf types.str;
          default = [ ];
          internal = true;
          description = "Integration-owned Docker mounts appended after terminal overrides and before configuration hashing.";
        };

        allowNetwork = mkOption {
          type = types.nullOr types.bool;
          default = null;
          description = "Whether this profile's terminal Docker sandboxes may use outbound networking. Defaults to custom.services.hermes.allowNetwork.";
        };

        outputDir = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "Host directory mounted into this profile's terminal sandboxes for generated output.";
        };

        settings = mkOption {
          type = types.attrsOf types.anything;
          default = { };
          description = ''
            Additional raw Hermes config merged into this profile's config.yaml.
            Do not set agent.system_prompt unless the user explicitly requests
            it; system prompts are not inferred from profile roles.
          '';
        };

        systemPromptRationale = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = ''
            Required code-level rationale when settings.agent.system_prompt is
            set. Explain why an explicitly requested declarative system prompt
            is necessary instead of the profile's independently managed SOUL.md.
          '';
        };

        soulContent = mkOption {
          type = types.nullOr types.lines;
          default = null;
          description = ''
            Optional declarative content for this profile's SOUL.md. Use this
            only for narrow, repo-owned service profiles whose identity and
            operating boundary must be reproducible with the Nix configuration.
            When set, activation overwrites $HERMES_HOME/SOUL.md for that
            profile on every rebuild. Leave this unset for normal assistant
            profiles whose SOUL.md should evolve through profile state/manual
            edits.
          '';
        };

        declarativeSkills = mkOption {
          type = types.attrsOf skillArtifactType;
          default = { };
          description = ''
            Legacy direct repo-managed skill declarations exposed through a
            read-only external skill directory. New reusable capability roots
            should use agentSkills and package metadata. Activation fails if a
            mutable local skill has the same name, or if this map collides with
            a capability-provided skill.
          '';
          example = {
            nix-config-review = {
              category = "repo-managed";
              content = ''
                ---
                name: nix-config-review
                description: Review nix-config pull requests.
                ---

                # nix-config Review
              '';
            };
          };
        };

        extraToolsets = mkOption {
          type = types.listOf types.str;
          default = [ ];
          description = ''
            Additional Hermes toolsets appended to this profile's default toolsets.
            If settings.toolsets is set, these values append to that explicit list;
            otherwise they append to the module's gateway platform defaults.
          '';
          example = [ "kanban" ];
        };

        environment = mkOption {
          type = types.attrsOf types.str;
          default = { };
          description = "Non-secret environment variables rendered into this profile's Hermes .env file.";
          example = {
            MEALIE_BASE_URL = "https://recipes.example.com";
          };
        };

        environmentFiles = mkOption {
          type = types.listOf pathType;
          default = [ ];
          description = "Environment files loaded by this profile's systemd service and copied into its managed .env file.";
          example = [
            "/run/secrets/rendered/hermes-chef-env"
          ];
        };

        honcho = honchoProfileOptions {
          defaultAiPeer = name;
          defaultSessionStrategy = "per-directory";
        };
      };
    }
  );

  honchoDefaultHostKey = "hermes";
  honchoProfileHostKey = name: "hermes_${name}";

  enabledHonchoProfiles = filterAttrs (
    _name: profile: profile.enable && cfg.honcho.enable && profile.honcho.enable
  ) cfg.profiles;

  defaultHonchoEnabled = cfg.honcho.enable && effectiveDefaultProfile.honcho.enable;

  honchoHostBlock =
    profileHoncho:
    {
      enabled = true;
      inherit (profileHoncho) aiPeer sessionStrategy;
    }
    // profileHoncho.extraHostConfig;

  honchoHosts =
    optionalAttrs defaultHonchoEnabled {
      ${honchoDefaultHostKey} = honchoHostBlock effectiveDefaultProfile.honcho;
    }
    // mapAttrs' (
      name: profile: nameValuePair (honchoProfileHostKey name) (honchoHostBlock profile.honcho)
    ) enabledHonchoProfiles;

  honchoBaseConfig = {
    enabled = true;
    inherit (cfg.honcho)
      baseUrl
      workspace
      peerName
      pinUserPeer
      userPeerAliases
      recallMode
      ;
    hosts = honchoHosts;
  }
  // cfg.honcho.extraConfig;

  honchoBaseConfigFile = jsonFormat.generate "hermes-honcho-base.json" honchoBaseConfig;

  honchoConfigInstallCommands =
    let
      targetPaths =
        optional defaultHonchoEnabled hermesHomeDir
        ++ mapAttrsToList (name: _profile: profileDir name) enabledHonchoProfiles;
      uniqueTargetPaths = lib.unique targetPaths;
      hasHonchoTargets = uniqueTargetPaths != [ ];
      authPath = cfg.honcho.authJwtSecretPath;
      authArg = if authPath != null then toString authPath else "";
    in
    optionalString hasHonchoTargets ''
      honcho_tmp="$(${pkgs.coreutils}/bin/mktemp)"
      ${pkgs.python3}/bin/python3 - ${honchoBaseConfigFile} "$honcho_tmp" ${lib.escapeShellArg authArg} <<'PY'
      import base64
      import hmac
      import hashlib
      import json
      import sys

      source, target, secret_path = sys.argv[1], sys.argv[2], sys.argv[3]

      with open(source, encoding="utf-8") as f:
          config = json.load(f)

      def b64url(data: bytes) -> str:
          return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

      if secret_path:
          with open(secret_path, encoding="utf-8") as f:
              secret = f.read().strip().encode("utf-8")
          header = {"alg": "HS256", "typ": "JWT"}
          payload = {"t": "", "ad": True}
          signing_input = f"{b64url(json.dumps(header, separators=(',', ':')).encode())}.{b64url(json.dumps(payload, separators=(',', ':')).encode())}"
          signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
          token = f"{signing_input}.{b64url(signature)}"
          for host in config.get("hosts", {}).values():
              host["apiKey"] = token

      with open(target, "w", encoding="utf-8") as f:
          json.dump(config, f, indent=2, sort_keys=True)
          f.write("\n")
      PY
      chmod 600 "$honcho_tmp"
      ${concatStringsSep "\n" (
        map (targetDir: ''
          ${pkgs.coreutils}/bin/install -d -o ${cfg.user} -g ${cfg.group} -m 0750 ${targetDir}
          ${pkgs.coreutils}/bin/install -o ${cfg.user} -g ${cfg.group} -m 0600 "$honcho_tmp" ${targetDir}/honcho.json
        '') uniqueTargetPaths
      )}
      rm -f "$honcho_tmp"
    '';
in
{
  imports = [
  ];

  options.services.hermes-agent.profiles = mkOption {
    type = types.attrsOf capabilityProfileOptions;
    default = { };
    description = "Stable declarative Hermes profile capability roots.";
  };

  options.custom.services.hermes = {
    managedSourceRepositories = mkOption {
      type = types.attrsOf (
        types.submodule {
          options = {
            root = mkOption {
              type = types.path;
              description = "Pinned source root used to attribute declarations.";
            };
            mode = mkOption {
              type = types.enum [
                "kanban"
                "manual"
              ];
              default = "kanban";
            };
          }
          // lib.genAttrs [ "repository" "board" "assignee" ] (_: mkOption { type = types.str; });
        }
      );
      default = { };
      description = "Source roots and explicit change routes for each owning repository.";
    };
    jobSetupReminders.enable = mkEnableOption "daily Telegram reminders for unconfigured profile-owned native jobs";

    enable = mkEnableOption "repo-managed Hermes agent profiles";

    capabilityPackages = mkOption {
      type = types.attrsOf capabilityPackageType;
      default = { };
      internal = true;
      description = ''
        Canonical capability package registry assembled by repository modules.
        Profiles select only agentSkills, externalServices capabilities, and
        serviceIntegrations roots; this registry supplies dependency metadata.
      '';
    };

    user = mkOption {
      type = types.str;
      default = "hermes";
      description = "System user running Hermes gateways.";
    };

    group = mkOption {
      type = types.str;
      default = "hermes";
      description = "System group running Hermes gateways.";
    };

    uid = mkOption {
      type = types.int;
      description = "Stable UID for the Hermes service identity.";
    };

    gid = mkOption {
      type = types.int;
      description = "Stable GID for the Hermes service identity.";
    };

    stateDir = mkOption {
      type = types.str;
      default = "/var/lib/hermes";
      description = "Hermes state directory. HERMES_HOME is stateDir/.hermes.";
    };

    workingDirectory = mkOption {
      type = types.str;
      default = "${cfg.stateDir}/workspace";
      defaultText = ''"\${config.custom.services.hermes.stateDir}/workspace"'';
      description = "Working directory used by Hermes gateways and helper wrappers.";
    };

    modelSettings = mkOption {
      type = types.attrsOf types.anything;
      default = {
        provider = "openai-codex";
        default = "gpt-6-astra";
      };
      description = "Default Hermes model settings for the default and named profiles.";
    };

    terminalSettings = mkOption {
      type = types.attrsOf types.anything;
      default = { };
      description = "Terminal sandbox settings merged over the module's Docker sandbox defaults.";
    };

    web = mkOption {
      type = types.attrsOf types.anything;
      default = { };
      description = "Raw Hermes web settings merged into every rendered profile before per-profile settings.";
      example = {
        backend = "firecrawl";
      };
    };

    environment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      description = "Non-secret environment variables shared by the default and named Hermes gateway services.";
      example = {
        FIRECRAWL_API_URL = "http://127.0.0.1:3002";
      };
    };

    gatewayServiceDependencies = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Extra systemd services every Hermes gateway should require and start after.";
      example = [ "docker-firecrawl-api.service" ];
    };

    allowNetwork = mkOption {
      type = types.bool;
      default = true;
      description = "Whether Hermes terminal Docker sandboxes may use outbound networking by default. Profiles can override this.";
    };

    outputDir = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = ''
        Host directory mounted into the default profile's terminal sandboxes for generated output.
        Named profiles default to their own profile cache documents directory unless their outputDir is set.
      '';
    };

    outputContainerPath = mkOption {
      type = types.str;
      default = "/output";
      description = "Container path for sandbox-generated artifacts.";
    };

    extraDependencyGroups = mkOption {
      type = types.listOf types.str;
      default = [
        "messaging"
        "voice"
      ];
      description = "Hermes dependency groups added to the upstream services.hermes-agent module.";
    };

    addToSystemPackages = mkOption {
      type = types.bool;
      default = true;
      description = "Install Hermes on the host PATH through the upstream module.";
    };

    installServiceWrappers = mkOption {
      type = types.bool;
      default = true;
      description = "Install hermes-service and hermes-tui helper wrappers.";
    };

    multiplexProfiles.enable = mkEnableOption "a single default Hermes gateway serving named profiles";

    sharedCodexAuth = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Symlink every managed named profile to the global Hermes auth store
          and lock. This gives every gateway one writable OpenAI Codex
          subscription and serializes single-use refresh-token rotation while
          keeping profile configs, sessions, memories, and workspaces separate.
          Because Hermes stores all providers in one auth.json, other
          auth-store credentials are shared as well; this deployment therefore
          uses that store only for OpenAI Codex authentication.
        '';
      };
    };

    defaultProfile = {
      telegram = mkOption {
        type = types.submodule { options = telegramOptions; };
        default = { };
        description = "Telegram settings for the default Hermes profile.";
      };

      ssh = mkOption {
        type = types.submodule { options = sshOptions; };
        default = { };
        description = "SSH key settings for Git and deploy operations from the default Hermes profile.";
      };

      declarativeSkills = mkOption {
        type = types.attrsOf skillArtifactType;
        default = { };
        description = "Legacy direct repo-managed skills for the default profile; reusable roots should use agentSkills.";
      };

      settings = mkOption {
        type = types.attrsOf types.anything;
        default = { };
        description = ''
          Additional raw Hermes config merged into the default profile config.
          Do not set agent.system_prompt unless the user explicitly requests
          it; system prompts are not inferred from profile roles.
        '';
      };

      systemPromptRationale = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Required code-level rationale when
          defaultProfile.settings.agent.system_prompt is set. Explain why an
          explicitly requested declarative system prompt is necessary instead
          of the profile's independently managed SOUL.md.
        '';
      };

      extraToolsets = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = ''
          Additional Hermes toolsets appended to the default profile's default toolsets.
          If defaultProfile.settings.toolsets is set, these values append to that
          explicit list; otherwise they append to the module's gateway platform defaults.
        '';
        example = [ "kanban" ];
      };

      allowNetwork = mkOption {
        type = types.nullOr types.bool;
        default = null;
        description = "Whether the default profile's terminal Docker sandboxes may use outbound networking. Defaults to custom.services.hermes.allowNetwork.";
      };

      environment = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = "Non-secret environment variables rendered into the default Hermes profile env file.";
      };

      environmentFiles = mkOption {
        type = types.listOf pathType;
        default = [ ];
        description = "Environment files loaded by the default Hermes profile service and copied into its managed .env file.";
      };

      honcho = mkOption {
        type = types.submodule {
          options = honchoProfileOptions {
            defaultAiPeer = "hermes";
            defaultSessionStrategy = "per-directory";
          };
        };
        default = { };
        description = "Honcho memory provider settings for the default Hermes profile.";
      };
    };

    profiles = mkOption {
      type = types.attrsOf profileOptions;
      default = { };
      description = "Named Hermes profiles, each rendered as its own systemd gateway service.";
      example = {
        chef.telegram = {
          allowFrom = [
            "243945372"
            "248449978"
          ];
        };
      };
    };

    sharedCredentialSourcePaths = mkOption {
      type = types.listOf pathType;
      default = [ ];
      description = ''
        Explicitly reviewed source credential files that more than one enabled
        profile may read. Duplicate profile credential sources fail evaluation
        unless listed here; independent profile credentials remain the default.
      '';
    };

    rootlessDocker = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Enable Hermes-owned rootless Docker as terminal sandbox backend.";
      };

      subUidStart = mkOption {
        type = types.int;
        default = 231072;
        description = "Start of the subordinate UID range for the Hermes user.";
      };

      subGidStart = mkOption {
        type = types.int;
        default = 231072;
        description = "Start of the subordinate GID range for the Hermes user.";
      };

      subIdCount = mkOption {
        type = types.int;
        default = 65536;
        description = "Size of the subordinate UID/GID ranges for rootless Docker.";
      };

      waitTimeoutSeconds = mkOption {
        type = types.int;
        default = 60;
        description = "Seconds to wait for the Hermes rootless Docker socket before gateway startup.";
      };
    };

    housekeeping = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Enable periodic cleanup of unused Hermes rootless Docker resources.";
      };

      schedule = mkOption {
        type = types.str;
        default = "weekly";
        description = "systemd OnCalendar value for Hermes Docker housekeeping.";
      };

      randomizedDelay = mkOption {
        type = types.str;
        default = "1h";
        description = "RandomizedDelaySec for Hermes Docker housekeeping.";
      };

      pruneImages = mkOption {
        type = types.bool;
        default = true;
        description = "Prune unused Hermes sandbox images.";
      };

      pruneVolumes = mkOption {
        type = types.bool;
        default = false;
        description = "Prune unused Hermes Docker volumes. Disabled by default to avoid data loss.";
      };
    };

    dashboard = {
      enable = mkEnableOption "Hermes browser dashboard service";

      host = mkOption {
        type = types.str;
        default = "127.0.0.1";
        description = "Host address passed to `hermes dashboard --host`.";
      };

      port = mkOption {
        type = types.port;
        default = 9119;
        description = "Port passed to `hermes dashboard --port`.";
      };

      publicUrl = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Public URL used by Hermes to reconstruct dashboard auth callbacks
          behind a reverse proxy.
        '';
        example = "https://hermes.example.com";
      };

      oidc = {
        enable = mkEnableOption "Hermes dashboard self-hosted OIDC auth provider";

        issuer = mkOption {
          type = types.str;
          default = "";
          description = "OIDC issuer URL for the Hermes dashboard self-hosted provider.";
          example = "https://auth.example.com/application/o/hermes/";
        };

        scopes = mkOption {
          type = types.str;
          default = "openid profile email";
          description = "OIDC scopes requested by the Hermes dashboard.";
        };

      };

      environment = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = "Additional non-secret dashboard environment variables.";
      };

      environmentFiles = mkOption {
        type = types.listOf pathType;
        default = [ ];
        description = "Additional dashboard environment files.";
      };
    };

    honcho = {
      enable = mkEnableOption "declarative Honcho memory provider configuration";

      baseUrl = mkOption {
        type = types.str;
        default = "http://127.0.0.1:8000";
        description = "Base URL for the Honcho API.";
      };

      workspace = mkOption {
        type = types.str;
        default = "hermes";
        description = "Honcho workspace shared by enabled Hermes profiles.";
      };

      peerName = mkOption {
        type = types.str;
        default = cfg.user;
        description = "Default Honcho user peer name.";
      };

      pinUserPeer = mkOption {
        type = types.bool;
        default = false;
        description = "Whether to pin gateway runtime users to peerName.";
      };

      userPeerAliases = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = "Gateway runtime user IDs mapped to stable Honcho user peer names.";
      };

      recallMode = mkOption {
        type = types.enum [
          "hybrid"
          "context"
          "tools"
        ];
        default = "hybrid";
        description = "Honcho memory recall mode.";
      };

      authJwtSecretPath = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          Optional path to Honcho AUTH_JWT_SECRET. When set, activation mints a
          local admin JWT and stores it in managed honcho.json host blocks.
        '';
      };

      extraConfig = mkOption {
        type = types.attrsOf types.anything;
        default = { };
        description = "Additional root-level keys merged into managed honcho.json.";
      };
    };
  };

  config = mkIf cfg.enable (mkMerge [
    {
      assertions = [
        {
          assertion =
            !(lib.any profileHasDeclarativeSkills (
              builtins.attrValues (enabledProfiles // { default = effectiveDefaultProfile; })
            ))
            || cfg.managedSourceRepositories != { };
          message = "Managed Hermes resources require a change destination; select the existing Forgejo-to-Kanban workflow or configure managedSourceRepositories.";
        }
        {
          assertion =
            !effectiveDefaultProfile.telegram.enable
            || (
              effectiveDefaultProfile.telegram.botTokenFile != null
              && effectiveDefaultProfile.telegram.allowFrom != [ ]
            );
          message = "custom.services.hermes.defaultProfile.telegram requires botTokenFile and allowFrom when enabled.";
        }
        {
          assertion = !cfg.dashboard.oidc.enable || cfg.dashboard.oidc.issuer != "";
          message = "custom.services.hermes.dashboard.oidc requires issuer when enabled.";
        }
        {
          assertion =
            !profileHasDeclarativeSystemPrompt effectiveDefaultProfile
            || effectiveDefaultProfile.systemPromptRationale != null;
          message = "custom.services.hermes.defaultProfile.settings.agent.system_prompt requires systemPromptRationale documenting the explicit request and why SOUL.md is insufficient.";
        }
        {
          assertion = builtins.all (service: service.endpoint != "") (
            builtins.attrValues effectiveDefaultProfile.externalServices
          );
          message = "services.hermes-agent.profiles.default.externalServices endpoints must be non-empty.";
        }
        {
          assertion = builtins.all (name: builtins.match "^[a-z0-9][a-z0-9._-]*$" name != null) (
            builtins.attrNames effectiveDefaultProfile.externalServices
          );
          message = "services.hermes-agent.profiles.default.externalServices keys must match ^[a-z0-9][a-z0-9._-]*$.";
        }
        {
          assertion = builtins.all externalServiceFieldsValid (
            builtins.attrValues effectiveDefaultProfile.externalServices
          );
          message = "services.hermes-agent.profiles.default.externalServices fields must use safe names, must not shadow endpoint, and must not appear in both textFiles and credentialFiles.";
        }
        {
          assertion =
            !builtins.any volumeTargetsReservedServices (cfg.terminalSettings.docker_volumes or [ ]);
          message = "custom.services.hermes.terminalSettings.docker_volumes must not target the module-owned /run/hermes-credentials/services namespace.";
        }
        {
          assertion = builtins.all (
            profile:
            let
              settings = profileUserTerminalSettings profile;
            in
            !builtins.any volumeTargetsReservedHelpers (settings.docker_volumes or [ ])
            # One mount interface avoids a second Docker CLI parser here.
            && !builtins.any (
              arg:
              lib.hasPrefix "-v" arg
              || builtins.any (flag: arg == flag || lib.hasPrefix "${flag}=" arg) [
                "--volume"
                "--mount"
                "--volumes-from"
                "--tmpfs"
              ]
            ) (settings.docker_extra_args or [ ])
          ) ([ effectiveDefaultProfile ] ++ builtins.attrValues enabledProfiles);
          message = "Hermes user mounts must use docker_volumes and must not overlap /run/hermes-capabilities or the profile-only /nix/store runtime namespace.";
        }
        {
          assertion = invalidCapabilityPackageIds == [ ];
          message = "custom.services.hermes.capabilityPackages contains invalid package contracts: ${concatStringsSep ", " invalidCapabilityPackageIds}";
        }
        {
          assertion = unapprovedSharedCredentialSourcePaths == [ ];
          message = "Hermes profile credential sources are shared without explicit custom.services.hermes.sharedCredentialSourcePaths approval: ${concatStringsSep ", " unapprovedSharedCredentialSourcePaths}";
        }
        {
          assertion = profileOutputDirsCanonical && profileOutputDirsDisjoint;
          message = "Hermes default and named profile outputDir paths must be canonical absolute paths and pairwise non-overlapping.";
        }
        {
          assertion = profileCapabilityContractValid effectiveDefaultProfile;
          message = "services.hermes-agent.profiles.default capability closure is invalid: check roots, dependencies, providers, service requirements/scopes, native requirements, and mutable/config collisions.";
        }
      ]
      ++ concatLists (
        mapAttrsToList (name: profile: [
          {
            assertion =
              !builtins.hasAttr name jobReminderProfiles
              || (
                profile.jobSetupReminders.targets != [ ]
                && builtins.all (
                  target: builtins.match "telegram:-?[0-9]{1,20}(:[0-9]{1,20})?" target != null
                ) profile.jobSetupReminders.targets
              );
            message = "Hermes profile ${name} job setup reminders require explicit Telegram targets.";
          }
          {
            assertion =
              !profile.telegram.enable
              || (profile.telegram.botTokenFile != null && profile.telegram.allowFrom != [ ]);
            message = "custom.services.hermes.profiles.${name}.telegram requires botTokenFile and allowFrom when enabled.";
          }
          {
            assertion = !profileHasDeclarativeSystemPrompt profile || profile.systemPromptRationale != null;
            message = "custom.services.hermes.profiles.${name}.settings.agent.system_prompt requires systemPromptRationale documenting the explicit request and why SOUL.md is insufficient.";
          }
          {
            assertion = builtins.all (service: service.endpoint != "") (
              builtins.attrValues profile.externalServices
            );
            message = "services.hermes-agent.profiles.${name}.externalServices endpoints must be non-empty.";
          }
          {
            assertion = builtins.all (
              serviceName: builtins.match "^[a-z0-9][a-z0-9._-]*$" serviceName != null
            ) (builtins.attrNames profile.externalServices);
            message = "services.hermes-agent.profiles.${name}.externalServices keys must match ^[a-z0-9][a-z0-9._-]*$.";
          }
          {
            assertion = builtins.all externalServiceFieldsValid (builtins.attrValues profile.externalServices);
            message = "services.hermes-agent.profiles.${name}.externalServices fields must use safe names, must not shadow endpoint, and must not appear in both textFiles and credentialFiles.";
          }
          {
            assertion =
              !builtins.any volumeTargetsReservedServices (profile.terminalSettings.docker_volumes or [ ]);
            message = "custom.services.hermes.profiles.${name}.terminalSettings.docker_volumes must not target the module-owned /run/hermes-credentials/services namespace.";
          }
          {
            assertion = profileCapabilityContractValid profile;
            message = "services.hermes-agent.profiles.${name} capability closure is invalid: check roots, dependencies, providers, service requirements/scopes, native requirements, and mutable/config collisions.";
          }
        ]) enabledProfiles
      );

      users.groups.${cfg.group}.gid = cfg.gid;
      users.users.${cfg.user} = {
        inherit (cfg) uid;
        linger = mkIf cfg.rootlessDocker.enable true;
        subUidRanges = mkIf cfg.rootlessDocker.enable [
          {
            startUid = cfg.rootlessDocker.subUidStart;
            count = cfg.rootlessDocker.subIdCount;
          }
        ];
        subGidRanges = mkIf cfg.rootlessDocker.enable [
          {
            startGid = cfg.rootlessDocker.subGidStart;
            count = cfg.rootlessDocker.subIdCount;
          }
        ];
      };

      services.hermes-agent = {
        enable = true;
        package = mkDefault pkgs.hermes-agent;
        container.enable = false;
        inherit (cfg) user group extraDependencyGroups;
        createUser = true;
        settings = profileSettings {
          profile = effectiveDefaultProfile;
          named = false;
          outputDir = defaultOutputDir;
          workspaceDir = defaultWorkspaceDir;
        };
        environment = profileServiceEnvironment "default" effectiveDefaultProfile;
        environmentFiles = map toString effectiveDefaultProfile.environmentFiles;
        inherit (cfg) stateDir workingDirectory addToSystemPackages;
      };

      system.activationScripts.hermes-managed-skills =
        lib.stringAfter
          (
            [ "users" ]
            ++ optional (enabledProfiles != { }) "hermes-profiles"
            ++ optional (profileHasSsh effectiveDefaultProfile) "hermes-default-profile"
          )
          ''
            ${pkgs.python3}/bin/python3 ${./capabilities/activate-managed-skills.py} \
              --home ${lib.escapeShellArg hermesHomeDir} --spec ${managedSkillsActivationSpec}
          '';

      systemd.tmpfiles.rules =
        map (dir: "d ${dir} 0770 ${cfg.user} ${cfg.group} -") defaultManagedDirs
        ++ optional (
          profileHasExternalServices effectiveDefaultProfile
          || builtins.any profileHasExternalServices (builtins.attrValues enabledProfiles)
        ) "d ${servicesRuntimeRoot} 0755 ${cfg.user} ${cfg.group} -"
        ++ lib.optionals (profileHasExternalServices effectiveDefaultProfile) [
          "d ${profileServicesRuntimeRoot "default"} 0755 ${cfg.user} ${cfg.group} -"
          "d ${profileServicesRuntimeDir "default"} 0700 ${cfg.user} ${cfg.group} -"
        ]
        ++ concatLists (
          mapAttrsToList (
            name: profile:
            map (dir: "d ${dir} 0770 ${cfg.user} ${cfg.group} -") (profileManagedDirs name profile)
            ++ lib.optionals (profileHasExternalServices profile) [
              "d ${profileServicesRuntimeRoot name} 0755 ${cfg.user} ${cfg.group} -"
              "d ${profileServicesRuntimeDir name} 0700 ${cfg.user} ${cfg.group} -"
            ]
          ) enabledProfiles
        );

      systemd.services = {
        hermes-agent = {
          path = gatewayRuntimePackages;
          preStart =
            profileDockerCleanupScript "default" effectiveDefaultProfile defaultOutputDir
              defaultWorkspaceDir;
          restartTriggers = [
            defaultProfileConfig
          ]
          ++ optional cfg.sharedCodexAuth.enable sharedCodexAuthMarker
          ++ profileRestartTriggers "default" effectiveDefaultProfile;
          serviceConfig = {
            ExecStart = mkForce defaultGatewayStartScript;
            BindPaths = [
              "${defaultWorkspaceDir}:/workspace"
              "${defaultOutputDir}:${cfg.outputContainerPath}"
            ];
          };
          # Preserve NixOS's generated PATH. Suppress only the obsolete variable:
          # terminal.cwd in config.yaml is bridged by Hermes to TERMINAL_CWD.
          environment = {
            HOME = cfg.stateDir;
            HERMES_CWD = cfg.workingDirectory;
            HERMES_HOME = hermesHomeDir;
            HERMES_MANAGED = "true";
          }
          // optionalAttrs cfg.rootlessDocker.enable {
            DOCKER_HOST = "unix://${dockerSocket}";
            XDG_RUNTIME_DIR = runtimeDir;
          }
          // profileServiceEnvironment "default" effectiveDefaultProfile
          // {
            MESSAGING_CWD = mkForce null;
            HERMES_CAPABILITY_MANIFEST = toString (
              profileCapabilityManifestFile "default" effectiveDefaultProfile
            );
          };
        };
      }
      // optionalAttrs cfg.dashboard.enable {
        hermes-dashboard = {
          description = "Hermes Agent Dashboard";
          wantedBy = [ "multi-user.target" ];
          wants = [ "network-online.target" ];
          after = [
            "network-online.target"
          ]
          ++ optional cfg.rootlessDocker.enable "hermes-rootless-docker-ready.service";
          requires = optional cfg.rootlessDocker.enable "hermes-rootless-docker-ready.service";
          path = [
            config.services.hermes-agent.package
            pkgs.bash
            pkgs.coreutils
            pkgs.docker
            pkgs.git
          ]
          ++ config.services.hermes-agent.extraPackages;
          environment = {
            DOCKER_HOST = "unix://${dockerSocket}";
            HOME = cfg.stateDir;
            HERMES_HOME = hermesHomeDir;
            HERMES_CWD = cfg.workingDirectory;
            HERMES_MANAGED = "true";
            XDG_RUNTIME_DIR = runtimeDir;
          };
          serviceConfig = {
            User = config.services.hermes-agent.user;
            Group = config.services.hermes-agent.group;
            WorkingDirectory = cfg.workingDirectory;
            ExecStart = concatStringsSep " " [
              "${config.services.hermes-agent.package}/bin/hermes"
              "dashboard"
              "--no-open"
              "--skip-build"
              "--host"
              cfg.dashboard.host
              "--port"
              (toString cfg.dashboard.port)
            ];
            Restart = config.services.hermes-agent.restart;
            RestartSec = config.services.hermes-agent.restartSec;
            UMask = "0007";
            NoNewPrivileges = true;
            ProtectSystem = "strict";
            ProtectHome = false;
            ReadWritePaths = [
              cfg.stateDir
              cfg.workingDirectory
            ];
            PrivateTmp = true;
          }
          // optionalAttrs dashboardHasEnv {
            EnvironmentFile = dashboardEnvironmentFiles;
          };
          restartTriggers = dashboardEnvironmentFiles;
        };
      };
    }

    (mkIf cfg.rootlessDocker.enable {
      virtualisation.docker.enable = mkDefault true;

      virtualisation.docker.rootless = {
        enable = true;
        setSocketVariable = false;
      };

      systemd.services = {
        hermes-rootless-docker-ready = {
          description = "Wait for Hermes rootless Docker socket";
          wants = [ "user@${toString cfg.uid}.service" ];
          after = [ "user@${toString cfg.uid}.service" ];
          before = [
            "hermes-agent.service"
          ]
          ++ optional cfg.dashboard.enable "hermes-dashboard.service"
          ++ mapAttrsToList (name: _profile: "hermes-agent-${name}.service") enabledProfiles;
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
          };
          script = ''
            for attempt in $(${pkgs.coreutils}/bin/seq 1 ${toString cfg.rootlessDocker.waitTimeoutSeconds}); do
              if ${pkgs.coreutils}/bin/test -S ${dockerSocket}; then
                exit 0
              fi
              ${pkgs.coreutils}/bin/sleep 1
            done

            echo "Hermes rootless Docker socket did not appear at ${dockerSocket}" >&2
            exit 1
          '';
        };

        hermes-agent = {
          requires = [ "hermes-rootless-docker-ready.service" ] ++ cfg.gatewayServiceDependencies;
          after = [ "hermes-rootless-docker-ready.service" ] ++ cfg.gatewayServiceDependencies;
          environment = {
            DOCKER_HOST = "unix://${dockerSocket}";
            XDG_RUNTIME_DIR = runtimeDir;
          };
        };
      }
      // profileGatewayServices
      // optionalAttrs cfg.housekeeping.enable {
        hermes-docker-housekeeping = {
          description = "Clean up unused Hermes rootless Docker resources";
          wants = [ "user@${toString cfg.uid}.service" ];
          after = [ "user@${toString cfg.uid}.service" ];
          path = [
            pkgs.coreutils
            pkgs.docker
          ];
          environment = {
            DOCKER_HOST = "unix://${dockerSocket}";
            XDG_RUNTIME_DIR = runtimeDir;
          };
          serviceConfig = {
            Type = "oneshot";
            User = cfg.user;
            Group = cfg.group;
          };
          script = ''
            set -euo pipefail

            docker container prune -f
            ${optionalString cfg.housekeeping.pruneImages "docker image prune -af"}
            docker network prune -f
            docker builder prune -af
            ${optionalString cfg.housekeeping.pruneVolumes "docker volume prune -f"}
            docker system df
          '';
        };
      };

      systemd.timers = optionalAttrs cfg.housekeeping.enable {
        hermes-docker-housekeeping = {
          description = "Timer for Hermes rootless Docker housekeeping";
          wantedBy = [ "timers.target" ];
          timerConfig = {
            OnCalendar = cfg.housekeeping.schedule;
            Persistent = true;
            RandomizedDelaySec = cfg.housekeeping.randomizedDelay;
          };
        };
      };
    })

    (mkIf (enabledProfiles != { }) {
      systemd.services = profileNativeCronServices // profileNativeCronReminderServices;
      systemd.paths = profileNativeCronPaths;
      systemd.timers = profileNativeCronReminderTimers;
      system.activationScripts.hermes-profiles = lib.stringAfter (
        [ "users" ] ++ optional (config.system.activationScripts ? setupSecrets) "setupSecrets"
      ) (concatStringsSep "\n" (mapAttrsToList profileActivation enabledProfiles));
    })

    (mkIf (profileHasSsh effectiveDefaultProfile) {
      system.activationScripts.hermes-default-profile = lib.stringAfter (
        [ "users" ] ++ optional (config.system.activationScripts ? setupSecrets) "setupSecrets"
      ) defaultProfileActivation;
    })

    (mkIf cfg.honcho.enable {
      assertions = [
        {
          assertion = defaultHonchoEnabled || enabledHonchoProfiles != { };
          message = "custom.services.hermes.honcho.enable requires at least one Honcho-enabled Hermes profile.";
        }
      ];

      services.hermes-agent.extraDependencyGroups = lib.mkAfter [ "honcho" ];

      system.activationScripts.hermes-honcho = lib.stringAfter (
        [ "users" ] ++ optional (config.system.activationScripts ? setupSecrets) "setupSecrets"
      ) honchoConfigInstallCommands;
    })

    (mkIf cfg.installServiceWrappers {
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hermes-service" ''
          exec /run/wrappers/bin/sudo -u ${cfg.user} -H ${pkgs.bash}/bin/bash -c \
            'cd ${cfg.workingDirectory} && exec "$@"' \
            hermes-service \
            ${pkgs.coreutils}/bin/env \
            DOCKER_HOST=unix://${dockerSocket} \
            HERMES_HOME=${hermesHomeDir} \
            HERMES_CWD=${cfg.workingDirectory} \
            XDG_RUNTIME_DIR=${runtimeDir} \
            hermes "$@"
        '')
        (pkgs.writeShellScriptBin "hermes-tui" ''
          exec /run/wrappers/bin/sudo -u ${cfg.user} -H ${pkgs.bash}/bin/bash -c \
            'cd ${cfg.workingDirectory} && exec "$@"' \
            hermes-tui \
            ${pkgs.coreutils}/bin/env \
            DOCKER_HOST=unix://${dockerSocket} \
            HERMES_HOME=${hermesHomeDir} \
            HERMES_CWD=${cfg.workingDirectory} \
            XDG_RUNTIME_DIR=${runtimeDir} \
            hermes --tui
        '')
        (pkgs.writeShellScriptBin "hermes-auth" ''
          set -euo pipefail

          restart_gateways() {
            status="$?"
            trap - EXIT HUP INT TERM
            echo "Restarting Hermes gateways..."
            if ! /run/wrappers/bin/sudo ${pkgs.systemd}/bin/systemctl start ${gatewayUnitArgs}; then
              echo "Failed to restart one or more Hermes gateways." >&2
              if [ "$status" -eq 0 ]; then
                status=1
              fi
            fi
            exit "$status"
          }

          trap restart_gateways EXIT
          trap 'exit 129' HUP
          trap 'exit 130' INT
          trap 'exit 143' TERM

          echo "Stopping Hermes gateways to protect the shared OAuth refresh token..."
          /run/wrappers/bin/sudo ${pkgs.systemd}/bin/systemctl stop ${gatewayUnitArgs}

          /run/wrappers/bin/sudo -u ${cfg.user} -H \
            ${pkgs.coreutils}/bin/env \
            DOCKER_HOST=unix://${dockerSocket} \
            HERMES_HOME=${hermesHomeDir} \
            HERMES_CWD=${cfg.workingDirectory} \
            XDG_RUNTIME_DIR=${runtimeDir} \
            ${pkgs.bash}/bin/bash -euo pipefail -c '
              cd ${cfg.workingDirectory}

              if [ "''${1:-}" = replace ]; then
                shift
                if [ "$#" -lt 1 ]; then
                  echo "Usage: hermes-auth replace PROVIDER [auth-add options]" >&2
                  exit 2
                fi

                provider="$1"
                shift
                auth_file="$HERMES_HOME/auth.json"
                if [ -s "$auth_file" ]; then
                  auth_tmp="$auth_file.replace.$$"
                  trap "${pkgs.coreutils}/bin/rm -f \"$auth_tmp\"" EXIT
                  ${pkgs.jq}/bin/jq --arg provider "$provider" "
                    del(.providers[\$provider])
                    | del(.credential_pool[\$provider])
                    | del(.suppressed_sources[\$provider])
                    | if .active_provider == \$provider then del(.active_provider) else . end
                    | if (.providers | type == \"object\" and length == 0) then del(.providers) else . end
                    | if (.credential_pool | type == \"object\" and length == 0) then del(.credential_pool) else . end
                    | if (.suppressed_sources | type == \"object\" and length == 0) then del(.suppressed_sources) else . end
                  " "$auth_file" > "$auth_tmp"
                  ${pkgs.coreutils}/bin/install -m 0600 "$auth_tmp" "$auth_file"
                  ${pkgs.coreutils}/bin/rm -f "$auth_tmp"
                  trap - EXIT
                fi

                exec hermes auth add "$provider" "$@"
              fi

              exec hermes auth "$@"
            ' \
            hermes-auth \
            "$@"
        '')
      ];
    })
  ]);
}
