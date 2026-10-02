{
  inputs,
  pkgs,
  system,
}:
let
  inherit (pkgs) lib;
  capabilityLib = import ./lib.nix { inherit lib; };
  foundation = import ./foundation-packages.nix { inherit lib; };
  packageSet =
    packages:
    builtins.listToAttrs (
      map (package: {
        name = package.id;
        value = package;
      }) packages
    );

  leaf = capabilityLib.mkAgentSkill "leaf" {
    provides = {
      skills.leaf = {
        category = "test";
        source = ./tests/leaf;
      };
      toolsets = [ "web" ];
      hermesConfig.custom_capability_test.enabled = true;
    };
  };
  bundle = capabilityLib.mkAgentSkill "bundle" {
    requires = [ leaf.id ];
    requiredHermesCapabilities = [ "toolsets" ];
  };
  issueIntake = capabilityLib.mkExternalServiceCapability "forgejo" "issue-intake" {
    provides.managedArtifacts = [ "file:forgejo/issue-intake" ];
  };
  forgejoKanban = capabilityLib.mkServiceIntegration "forgejo-to-kanban" {
    safety = {
      credentialRequirements = [
        {
          service = "forgejo";
          field = "credential";
        }
      ];
      pathRequirements = [
        {
          path = "$HERMES_HOME/state/forgejo-to-kanban";
          access = "read-write";
        }
      ];
      writeScope = "external-system";
      confirmation = "policy-preauthorized";
      verification = "external-readback-required";
    };
    requires = [ issueIntake.id ];
    requiredExternalServices = [ "forgejo" ];
    requiredHermesCapabilities = [ "kanban" ];
    provides.managedArtifacts = [ "unit:forgejo-to-kanban" ];
  };
  validPackages = packageSet (
    [
      leaf
      bundle
      issueIntake
      forgejoKanban
    ]
    ++ foundation.all
  );

  baseModule = {
    _module.args.monitoringLib = null;
    system.stateVersion = "25.11";
    fileSystems."/" = {
      device = "/dev/disk/by-label/nixos";
      fsType = "ext4";
    };
    boot.loader.grub.devices = [ "/dev/vda" ];
    users.users.tester.isSystemUser = true;
    nixpkgs.overlays = [ (import ../../../../../pkgs { inherit inputs; }) ];
    custom.services.hermes = {
      managedSourceRepositories.project = {
        root = ../../../../../.;
        repository = "nixos/hermes-config";
        board = "homelab-devops";
        assignee = "system-admin";
      };
      enable = true;
      user = "tester";
      group = "users";
      uid = 1000;
      gid = 100;
      stateDir = "/build/hermes-capability-test";
      defaultProfile.telegram.enable = false;
      capabilityPackages = validPackages;
      profiles.system-admin = {
        telegram.enable = false;
        # Generic overrides must be consumed before managed mounts and hashing.
        settings.terminal = {
          timeout = 271;
          docker_volumes = [ "/tmp/user-resource:/user-resource:ro" ];
        };
      };
      profiles.code-reviewer = {
        telegram.enable = false;
      };
      profiles.software-developer = {
        telegram.enable = false;
      };
      profiles.card-orchestrator.telegram.enable = false;
      profiles.network-architect.telegram.enable = false;
    };
    services.hermes-agent.profiles = {
      system-admin = {
        agentSkills.bundle = true;
        agentSkills.declarative-agent-capability-packaging = true;
        externalServices.forgejo = {
          endpoint = "https://git.example.test";
          credentialFiles.credential = "/test-secrets/forgejo";
          capabilities = [ "issue-intake" ];
          scope.include = [ "nixos/nix-config" ];
        };
        serviceIntegrations.forgejo-to-kanban.settings = {
          board = "homelab-devops";
          repositories.nix-config = {
            stateSubdirectory = "";
            id = "fixture";
            repository = "nixos/nix-config";
            defaultBranch = "master";
            namespace = "nix-config";
            workflowKeyPrefix = "forgejo";
            roles = {
              implementer = "system-admin";
              reviewer = "code-reviewer";
            };
            tenant = "homelab-devops";
            automationAuthors = [ ];
            repositoryJob = null;
            legacyContainerMarker = null;
            legacyGenerations = { };
          };
        };
        serviceIntegrations.repository-sync.instances.nix-config-pull = true;
      };
      code-reviewer = {
        externalServices.forgejo = {
          endpoint = "https://git.example.test";
          scope.include = [ "nixos/nix-config" ];
        };
        agentSkills.evidence-driven-change-control = true;
        agentSkills.security-sensitive-protocol-review = true;
      };
      software-developer = { };
      card-orchestrator = { };
      network-architect = { };
    };
  };

  mkEval =
    module:
    inputs.nixpkgs.lib.nixosSystem {
      inherit system;
      specialArgs = {
        inherit inputs;
        adminUser = "tester";
        hostname = "hermes-capability-eval";
        stateVersion = "25.11";
      };
      modules = [
        ../default.nix
        inputs.hermes-agent.nixosModules.default
        inputs.sops-nix.nixosModules.sops
        baseModule
        module
      ];
    };

  validEval = mkEval { };
  evalSucceeds = module: (builtins.tryEval (mkEval module).config.system.build.toplevel).success;

  missingDependency = capabilityLib.mkAgentSkill "missing" {
    requires = [ "agentSkill:not-defined" ];
  };
  cycleA = capabilityLib.mkAgentSkill "cycle-a" {
    requires = [ "agentSkill:cycle-b" ];
  };
  cycleB = capabilityLib.mkAgentSkill "cycle-b" {
    requires = [ "agentSkill:cycle-a" ];
  };
  duplicateSkill = capabilityLib.mkAgentSkill "duplicate" {
    provides.skills.leaf = {
      category = "test";
      source = ./tests/leaf;
    };
  };
  conflict = capabilityLib.mkAgentSkill "conflict" {
    conflicts = [ leaf.id ];
  };
  unsupportedTarget = capabilityLib.mkAgentSkill "codex-only" {
    targets = [ "codex" ];
  };
  reservedArtifact = capabilityLib.mkAgentSkill "reserved-artifact" {
    provides.managedArtifacts = [ "file:../../escape" ];
  };
  unsafeSafetyMetadata = capabilityLib.mkAgentSkill "unsafe-safety-metadata" {
    safety = {
      writeScope = "external-system";
      confirmation = "not-required";
      verification = "not-required";
    };
  };

  withPackages = packages: {
    custom.services.hermes.capabilityPackages = lib.mkForce (validPackages // packageSet packages);
  };

  invalidCases = {
    missing-dependency = [
      (withPackages [ missingDependency ])
      { services.hermes-agent.profiles.system-admin.agentSkills = lib.mkForce { missing = true; }; }
    ];
    cycle = [
      (withPackages [
        cycleA
        cycleB
      ])
      { services.hermes-agent.profiles.system-admin.agentSkills = lib.mkForce { cycle-a = true; }; }
    ];
    duplicate-provider = [
      (withPackages [
        leaf
        duplicateSkill
      ])
      {
        services.hermes-agent.profiles.system-admin.agentSkills = lib.mkForce {
          leaf = true;
          duplicate = true;
        };
      }
    ];
    unsafe-package-name = [
      {
        custom.services.hermes.capabilityPackages = lib.mkForce {
          "agentSkill:unsafe/name" = leaf // {
            id = "agentSkill:unsafe/name";
            name = "unsafe/name";
          };
        };
      }
    ];
    reserved-artifact = [
      (withPackages [ reservedArtifact ])
      {
        services.hermes-agent.profiles.system-admin.agentSkills = lib.mkForce {
          reserved-artifact = true;
        };
      }
    ];
    unsafe-safety-metadata = [
      (withPackages [ unsafeSafetyMetadata ])
      {
        services.hermes-agent.profiles.system-admin.agentSkills = lib.mkForce {
          unsafe-safety-metadata = true;
        };
      }
    ];
    unsupported-target = [
      (withPackages [ unsupportedTarget ])
      { services.hermes-agent.profiles.system-admin.agentSkills = lib.mkForce { codex-only = true; }; }
    ];
    unsupported-combination = [
      (withPackages [
        leaf
        conflict
      ])
      {
        services.hermes-agent.profiles.system-admin.agentSkills = lib.mkForce {
          leaf = true;
          conflict = true;
        };
      }
    ];
    hidden-reserved-mount = [
      {
        custom.services.hermes.profiles.system-admin.terminalSettings.docker_volumes = [
          "/tmp/shadow:/run/hermes-credentials/services/forgejo:ro"
        ];
      }
    ];
    hidden-helper-mount = [
      {
        custom.services.hermes.profiles.system-admin.settings.terminal.docker_volumes = lib.mkForce [
          "/tmp/shadow:/run//hermes-capabilities/mealie-api:rw"
        ];
      }
    ];
    hidden-runtime-mount = [
      {
        custom.services.hermes.profiles.system-admin.settings.terminal.docker_extra_args = [
          "--mount=type=bind,source=/tmp/shadow,target=/nix/store"
        ];
      }
    ];
    reserved-direct-skill = [
      {
        custom.services.hermes.profiles.system-admin.declarativeSkills.bin = {
          category = "test";
          content = "# reserved";
        };
      }
    ];
    manual-management-root = [
      { services.hermes-agent.profiles.system-admin.agentSkills.nix-managed-resource-changes = true; }
    ];
    direct-management-shadow = [
      {
        custom.services.hermes.profiles.system-admin.declarativeSkills.nix-managed-resource-changes.content =
          "# local shadow";
      }
    ];
    missing-resource-route = [
      { custom.services.hermes.managedSourceRepositories = lib.mkForce { }; }
    ];
    mutable-skill-shadow = [
      {
        custom.services.hermes.profiles.system-admin.declarativeSkills.leaf = {
          category = "mutable-shadow";
          content = "# duplicate";
        };
      }
    ];
    config-collision = [
      { custom.services.hermes.profiles.system-admin.settings.custom_capability_test = { }; }
    ];
    include-exclude = [
      {
        services.hermes-agent.profiles.system-admin.externalServices.forgejo.scope.exclude = [
          "other/repo"
        ];
      }
    ];
    broad-without-acknowledgement = [
      {
        services.hermes-agent.profiles.system-admin.externalServices.forgejo.scope = lib.mkForce {
          exclude = [ "other/repo" ];
        };
      }
    ];
    missing-service = [
      {
        services.hermes-agent.profiles.system-admin.externalServices = lib.mkForce { };
      }
    ];
    unknown-integration-instance = [
      {
        services.hermes-agent.profiles.system-admin.serviceIntegrations.repository-sync = lib.mkForce {
          instances.unknown-repository-sync = true;
        };
      }
    ];
    disabled-integration-unknown-setting = [
      {
        services.hermes-agent.profiles.system-admin.serviceIntegrations.forgejo-to-kanban = lib.mkForce {
          enable = false;
          settings.unknown = true;
        };
      }
    ];
    invalid-inline-source-pair = [
      {
        custom.services.hermes.profiles.system-admin.declarativeSkills.invalid = {
          category = "test";
          content = "# invalid";
          source = ./tests/leaf;
        };
      }
    ];
  };

  invalidResults = lib.mapAttrs (_name: modules: evalSucceeds { imports = modules; }) invalidCases;
  disabledStructuredSelectionEval = mkEval {
    # This fixture disables the workflow that normally supplies the destination,
    # but retains managed skills/jobs. Standalone deployments provide it explicitly.
    custom.services.hermes.managedSourceRepositories.project = lib.mkForce {
      root = ../../../../../.;
      board = "test-board";
      assignee = "system-admin";
      repository = "example/config";
    };
    services.hermes-agent.profiles.system-admin.serviceIntegrations.forgejo-to-kanban = lib.mkForce {
      enable = false;
      settings = { };
    };
  };
  disabledStructuredSelectionSucceeds = builtins.seq disabledStructuredSelectionEval.config.system.build.toplevel true;

  unexpectedlyAccepted = builtins.attrNames (
    lib.filterAttrs (_name: succeeds: succeeds) invalidResults
  );

  validClosure = capabilityLib.resolve validPackages [
    bundle.id
    forgejoKanban.id
    foundation.declarativeAgentCapabilityPackaging.id
  ];
  explicitRetentionClosure = capabilityLib.resolve validPackages [
    leaf.id
    bundle.id
  ];
  dependencyRemovedClosure = capabilityLib.resolve validPackages [ leaf.id ];
  finalReferenceRemovedClosure = capabilityLib.resolve validPackages [ ];
  supportedManagedArtifacts = [
    "skill:leaf"
    "hermes-config:custom-capability-test"
    "file:repository-docs-audit/inventory"
    "unit:forgejo-to-kanban"
    "timer:forgejo-poll"
    "cron:cv-repo-pull"
    "ci:repository/forgejo-wiki-publication"
  ];
  unsafeManagedArtifacts = [
    "file:../../escape"
    "package:unsafe//name"
    "unknown:artifact"
  ];

  profileConfig = builtins.elemAt validEval.config.systemd.services.hermes-agent-system-admin.restartTriggers 0;
  skillDir = builtins.head (
    builtins.filter (
      path: lib.hasInfix "declarative-skills" (toString path)
    ) validEval.config.systemd.services.hermes-agent-system-admin.restartTriggers
  );
  capabilityManifest =
    validEval.config.systemd.services.hermes-agent-system-admin.environment.HERMES_CAPABILITY_MANIFEST;
  reviewerSkillDir = builtins.head (
    builtins.filter (
      path: lib.hasInfix "declarative-skills" (toString path)
    ) validEval.config.systemd.services.hermes-agent-code-reviewer.restartTriggers
  );
  reviewerCapabilityManifest =
    validEval.config.systemd.services.hermes-agent-code-reviewer.environment.HERMES_CAPABILITY_MANIFEST;
  defaultDispatcherCapabilityManifest =
    validEval.config.systemd.services.hermes-agent.environment.HERMES_CAPABILITY_MANIFEST;
  unrelatedProfileCapabilityManifest =
    validEval.config.systemd.services.hermes-agent-software-developer.environment.HERMES_CAPABILITY_MANIFEST;
  defaultDispatcherNativeJobs =
    (builtins.fromJSON (builtins.readFile defaultDispatcherCapabilityManifest)).nativeJobs;
  unrelatedProfileNativeJobs =
    (builtins.fromJSON (builtins.readFile unrelatedProfileCapabilityManifest)).nativeJobs;
  unrelatedProfileCredentialMounts =
    builtins.filter (volume: lib.hasInfix "/run/hermes-credentials/services" volume)
      validEval.config.custom.services.hermes.profiles.software-developer.terminalSettings.docker_volumes
        or [ ];
  unrelatedProfileRuntimePackages =
    (builtins.fromJSON (builtins.readFile unrelatedProfileCapabilityManifest)).runtimePackages;
  disabledStructuredSelectionManifest =
    disabledStructuredSelectionEval.config.systemd.services.hermes-agent-system-admin.environment.HERMES_CAPABILITY_MANIFEST;
in
assert import ./tests/repository-skills.nix { inherit lib; };
assert import ./tests/repository-sync.nix { inherit lib pkgs; };
assert
  validClosure.orderedIds == [
    leaf.id
    bundle.id
    issueIntake.id
    forgejoKanban.id
    foundation.evidenceDrivenChangeControl.id
    foundation.profileScopedServiceCredentials.id
    foundation.securitySensitiveProtocolReview.id
    foundation.declarativeAgentCapabilityPackaging.id
  ];
assert
  explicitRetentionClosure.orderedIds == [
    leaf.id
    bundle.id
  ];
assert dependencyRemovedClosure.orderedIds == [ leaf.id ];
assert finalReferenceRemovedClosure.orderedIds == [ ];
assert validClosure.toolsets == [ "web" ];
assert validClosure.requiredExternalServices == [ "forgejo" ];
assert builtins.all capabilityLib.managedArtifactValid supportedManagedArtifacts;
assert builtins.all (
  artifact: !(capabilityLib.managedArtifactValid artifact)
) unsafeManagedArtifacts;
assert disabledStructuredSelectionSucceeds;
assert unexpectedlyAccepted == [ ];
assert defaultDispatcherNativeJobs == [ ];
assert unrelatedProfileNativeJobs == [ ];
assert unrelatedProfileCredentialMounts == [ ];
assert unrelatedProfileRuntimePackages == [ ];
pkgs.runCommand "nixos-hermes-capability-graph-eval" { } ''
  set -euo pipefail

  cp ${profileConfig} "$TMPDIR/system-admin-config.yaml"
  grep -F 'custom_capability_test:' "$TMPDIR/system-admin-config.yaml"
  grep -F -- '- web' "$TMPDIR/system-admin-config.yaml"
  grep -F 'external_dirs:' "$TMPDIR/system-admin-config.yaml"
  ${
    pkgs.python3.withPackages (ps: [ ps.pyyaml ])
  }/bin/python3 - "$TMPDIR/system-admin-config.yaml" <<'PY'
  import sys, yaml
  terminal = yaml.safe_load(open(sys.argv[1]))["terminal"]
  assert terminal["timeout"] == 271
  volumes = terminal["docker_volumes"]
  assert "/tmp/user-resource:/user-resource:ro" in volumes
  for target in ("/run/hermes-capabilities", "/nix/store"):
      assert sum(volume.endswith(f":{target}:ro") for volume in volumes) == 1
  assert any(arg.startswith("hermes-config-hash=") for arg in terminal["docker_extra_args"])
  PY

  test -f ${skillDir}/test/leaf/SKILL.md
  test "$(cat ${skillDir}/test/leaf/reference.txt)" = 'supporting fixture'
  test -f ${skillDir}/devops/declarative-agent-capability-packaging/SKILL.md
  test -f ${skillDir}/devops/evidence-driven-change-control/SKILL.md
  test -f ${skillDir}/devops/profile-scoped-service-credentials/SKILL.md
  test -f ${skillDir}/software-development/security-sensitive-protocol-review/SKILL.md

  # Verify the generated service input, not the spelling of its Nix assignment.
  ${pkgs.python3}/bin/python3 - ${capabilityManifest} \
    ${defaultDispatcherCapabilityManifest} ${unrelatedProfileCapabilityManifest} <<'PY'
  import copy, json, sys
  selected, default, unrelated = [json.load(open(path)) for path in sys.argv[1:]]
  expected = json.loads(${
    lib.escapeShellArg (
      builtins.toJSON (
        validEval.config.custom.services.hermes.capabilityPackages.${forgejoKanban.id}.safety
        // {
          provider = forgejoKanban.id;
        }
      )
    )
  })

  def verify(selected, default, unrelated):
      actual = [entry for entry in selected["safety"] if entry["provider"] == expected["provider"]]
      assert actual == [expected], (actual, expected)
      assert default["safety"] == []
      assert unrelated["safety"] == []

  verify(selected, default, unrelated)
  for mutation in ("missing", "empty", "corrupt", "default-leak", "unrelated-leak"):
      candidate, dispatcher, other = copy.deepcopy((selected, default, unrelated))
      if mutation == "missing":
          del candidate["safety"]
      elif mutation == "empty":
          candidate["safety"] = []
      elif mutation == "corrupt":
          for entry in candidate["safety"]:
              if entry["provider"] == expected["provider"]:
                  entry["verification"] = "not-required"
      elif mutation == "default-leak":
          dispatcher["safety"] = [expected]
      else:
          other["safety"] = [expected]
      try:
          verify(candidate, dispatcher, other)
      except (AssertionError, KeyError):
          continue
      raise AssertionError("Safety publication verifier accepted " + mutation)
  print("Generated safety equality, profile isolation and five negative controls passed")
  PY

  test -f ${capabilityManifest}
  grep -F '"agentSkill:leaf"' ${capabilityManifest}
  grep -F '"agentSkill:bundle"' ${capabilityManifest}
  grep -F '"externalService:forgejo:issue-intake"' ${capabilityManifest}
  grep -F '"serviceIntegration:forgejo-to-kanban"' ${capabilityManifest}
  grep -F '"serviceIntegration:repository-sync"' ${capabilityManifest}
  ${pkgs.jq}/bin/jq -e '
    .nativeJobs == ["nix-config-pull"]
    and .provenance["cron:nix-config-pull"] == "serviceIntegration:repository-sync"
    and (.provenance | has("cron:cv-repo-pull") | not)
    and (.provenance | has("cron:obsidian-main-vault-pull") | not)
  ' ${capabilityManifest}
  ${pkgs.jq}/bin/jq -e '
    (.explicitRoots | index("serviceIntegration:forgejo-to-kanban") | not)
    and (.closure | index("serviceIntegration:forgejo-to-kanban") | not)
    and (.provenance | has("unit:forgejo-to-kanban") | not)
  ' ${disabledStructuredSelectionManifest}
  grep -F '"skill:leaf": "agentSkill:leaf"' ${capabilityManifest}
  grep -F '"skill:declarative-agent-capability-packaging": "agentSkill:declarative-agent-capability-packaging"' ${capabilityManifest}
  grep -F '"skill:evidence-driven-change-control": "agentSkill:evidence-driven-change-control"' ${capabilityManifest}
  grep -F '"skill:profile-scoped-service-credentials": "agentSkill:profile-scoped-service-credentials"' ${capabilityManifest}
  grep -F '"skill:security-sensitive-protocol-review": "agentSkill:security-sensitive-protocol-review"' ${capabilityManifest}

  test -f ${reviewerSkillDir}/devops/evidence-driven-change-control/SKILL.md
  test -f ${reviewerSkillDir}/software-development/security-sensitive-protocol-review/SKILL.md
  test ! -e ${reviewerSkillDir}/devops/profile-scoped-service-credentials/SKILL.md
  ${pkgs.jq}/bin/jq -e '
    .explicitRoots == [
      "agentSkill:evidence-driven-change-control",
      "agentSkill:security-sensitive-protocol-review"
    ]
    and .provenance["skill:evidence-driven-change-control"]
      == "agentSkill:evidence-driven-change-control"
    and .provenance["skill:security-sensitive-protocol-review"]
      == "agentSkill:security-sensitive-protocol-review"
  ' ${reviewerCapabilityManifest}
  ${pkgs.jq}/bin/jq -e '
    .explicitRoots == []
    and .closure == []
    and .nativeJobs == []
    and .runtimePackages == []
    and .toolsets == []
    and .requiredExternalServices == []
    and .safety == []
  ' ${defaultDispatcherCapabilityManifest}
  ${pkgs.jq}/bin/jq -e '
    .explicitRoots == []
    and .closure == []
    and .nativeJobs == []
    and .runtimePackages == []
    and .toolsets == []
    and .requiredExternalServices == []
    and .safety == []
  ' ${unrelatedProfileCapabilityManifest}

  touch $out
''
