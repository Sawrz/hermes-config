{ self, pkgs }:
let
  inputs = self.inputs // {
    hermes-config = self;
  };
  inherit (pkgs) lib;
  system = pkgs.stdenv.hostPlatform.system;
  kanbanVikunjaProjectionPackage = pkgs.kanban-vikunja-projection;
  hermesForgejoEventIngestCheck =
    let
      eval = inputs.nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = {
          inherit inputs;
          adminUser = "tester";
          hostname = "hermes-forgejo-event-ingest-eval";
          stateVersion = "25.11";
        };
        modules = [
          inputs.hermes-config.nixosModules.default
          inputs.hermes-config.nixosModules.incident-reconciliation
          inputs.sops-nix.nixosModules.sops
          {
            system.stateVersion = "25.11";
            users.users.tester.isNormalUser = true;
            custom.services.hermes = {
              managedSourceRepositories.project = {
                root = inputs.hermes-config.outPath;
                repository = "nixos/hermes-config";
                board = "fixture";
                assignee = "system-admin";
              };
              enable = true;
              uid = 989;
              gid = 985;
              defaultProfile.telegram.enable = false;
              profiles.system-admin = {
                telegram.enable = false;
              };
            };
            fileSystems."/" = {
              device = "/dev/fixture";
              fsType = "ext4";
            };
            boot.loader.grub.devices = [ "/dev/vda" ];
            custom.services.hermes.profiles.code-reviewer.telegram.enable = false;
            services.hermes-agent.profiles.code-reviewer.externalServices.forgejo = {
              endpoint = "https://git.example.test";
              credentialFiles.credential = "/test-secrets/reviewer";
              capabilities = [ "workflow" ];
              scope.include = [ "nixos/nix-config" ];
            };
            custom.services.hermes.repositoryAuthority = {
              ownerProfile = "system-admin";
              board = "homelab-devops";
              repositories.fixture = {
                stateSubdirectory = "";
                id = "fixture-nix-config";
                repository = "nixos/nix-config";
                defaultBranch = "master";
                namespace = "nix-config";
                workflowKeyPrefix = "forgejo";
                roles = {
                  implementer = "system-admin";
                  reviewer = "code-reviewer";
                };
                tenant = "homelab-devops";
                automationAuthors = [ "system-admin-agent" ];
                repositoryJob = null;
                legacyContainerMarker = null;
                legacyGenerations = { };
              };
            };
            services.hermes-agent.profiles.system-admin = {
              externalServices.forgejo = {
                endpoint = "https://git.example.test";
                credentialFiles.credential = "/test-secrets/forgejo-token";
                capabilities = [ "workflow" ];
                scope.include = [ "nixos/nix-config" ];
              };
              serviceIntegrations.forgejo-to-kanban.settings = {
                board = "homelab-devops";

              };
              serviceIntegrations.forgejo-event-ingest.settings = {
                initialSince = "2026-08-28T00:00:00Z";
                pollInterval = "7m";
              };
            };
          }
        ];
      };
      poll = eval.config.systemd.services.hermes-forgejo-event-poll;
      timer = eval.config.systemd.timers.hermes-forgejo-event-poll;
      endpointInIntegrationSettingsSucceeds =
        (builtins.tryEval
          (eval.extendModules {
            modules = [
              {
                services.hermes-agent.profiles.system-admin.serviceIntegrations.forgejo-event-ingest.settings.endpoint =
                  "https://forbidden.example.test";
              }
            ];
          }).config.system.build.toplevel
        ).success;
      invalidPollIntervalSucceeds =
        (builtins.tryEval
          (eval.extendModules {
            modules = [
              {
                services.hermes-agent.profiles.system-admin.serviceIntegrations.forgejo-event-ingest.settings.pollInterval =
                  lib.mkForce "../../command";
              }
            ];
          }).config.system.build.toplevel
        ).success;
    in
    assert lib.all (a: a.assertion) eval.config.assertions;
    assert !endpointInIntegrationSettingsSucceeds;
    assert !invalidPollIntervalSucceeds;
    pkgs.runCommand "hermes-forgejo-event-ingest-eval" { } ''
      set -euo pipefail
      grep -F '/bin/forgejo-event-journal' ${poll.serviceConfig.ExecStart}
      grep -F ' poll ' ${poll.serviceConfig.ExecStart}
      grep -F -- '--credential-file /test-secrets/forgejo-token' ${poll.serviceConfig.ExecStart}
      grep -F -- '--initial-since 2026-08-28T00:00:00Z' ${poll.serviceConfig.ExecStart}
      test ${lib.escapeShellArg poll.serviceConfig.StateDirectory} = hermes-forgejo-event-journal
      test ${lib.escapeShellArg timer.timerConfig.OnUnitActiveSec} = 7m
      test ${lib.escapeShellArg (lib.boolToString poll.serviceConfig.NoNewPrivileges)} = true
      if grep -R -E '(/hooks|method[[:space:]]*=[[:space:]]*"(POST|PATCH|DELETE)")' \
        ${(inputs.hermes-config + "/pkgs/common/forgejo-event-journal/src")}; then
        echo "event journal must not mutate Forgejo hooks" >&2
        exit 1
      fi
      touch $out
    '';

  hermesForgejoKanbanCheck =
    let
      eval = inputs.nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = {
          inherit inputs;
          adminUser = "tester";
          hostname = "hermes-forgejo-kanban-eval";
          stateVersion = "25.11";
        };
        modules = [
          inputs.hermes-config.nixosModules.default
          inputs.hermes-config.nixosModules.incident-reconciliation
          inputs.sops-nix.nixosModules.sops
          {
            system.stateVersion = "25.11";
            fileSystems."/" = {
              device = "/dev/disk/by-label/nixos";
              fsType = "ext4";
            };
            boot.loader.grub.devices = [ "/dev/vda" ];
            users.users.tester.isNormalUser = true;
            custom.services.hermes = {
              managedSourceRepositories.project = {
                root = inputs.hermes-config.outPath;
                repository = "nixos/hermes-config";
                board = "fixture";
                assignee = "system-admin";
              };
              enable = true;
              uid = 989;
              gid = 985;
              defaultProfile.telegram.enable = false;
              profiles = {
                system-admin = {
                  telegram.enable = false;
                  extraToolsets = [ "kanban" ];
                };
                code-reviewer = {
                  telegram.enable = false;
                  extraToolsets = [ "kanban" ];
                };
              };
            };
            custom.services.hermes.repositoryAuthority = {
              ownerProfile = "system-admin";
              board = "homelab-devops";
              repositories.fixture = {
                stateSubdirectory = "";
                id = "fixture-nix-config";
                repository = "nixos/nix-config";
                defaultBranch = "master";
                namespace = "nix-config";
                workflowKeyPrefix = "forgejo";
                roles = {
                  implementer = "system-admin";
                  reviewer = "code-reviewer";
                };
                tenant = "homelab-devops";
                automationAuthors = [ "system-admin-agent" ];
                repositoryJob = null;
                legacyContainerMarker = null;
                legacyGenerations = { };
              };
            };
            services.hermes-agent.profiles = {
              system-admin = {
                externalServices.forgejo = {
                  endpoint = "https://git.example.test";
                  credentialFiles.credential = "/test-secrets/forgejo-token";
                  capabilities = [ "workflow" ];
                  scope.include = [ "nixos/nix-config" ];
                };
                serviceIntegrations = {
                  forgejo-event-ingest = true;
                  forgejo-to-kanban.settings = {

                    board = "homelab-devops";
                    reconcileInterval = "2m";
                  };
                };
              };
              code-reviewer = {
                externalServices.forgejo = {
                  endpoint = "https://git.example.test";
                  credentialFiles.credential = "/test-secrets/forgejo-reviewer-token";
                  capabilities = [ "workflow" ];
                  scope.include = [ "nixos/nix-config" ];
                };
              };
            };
          }
        ];
      };
      secondEval = eval.extendModules {
        modules = [
          {
            custom.services.hermes.profiles = {
              builder.telegram.enable = false;
              auditor.telegram.enable = false;
            };
            custom.services.hermes.repositoryAuthority = lib.mkForce {
              ownerProfile = "builder";
              board = "engineering";
              repositories.fixture = {
                stateSubdirectory = "";
                id = "widgets";
                repository = "acme/widgets";
                defaultBranch = "trunk";
                namespace = "widgets";
                workflowKeyPrefix = "forgejo-widgets";
                roles = {
                  implementer = "builder";
                  reviewer = "auditor";
                };
                tenant = "engineering";
                automationAuthors = [ "builder-bot" ];
                repositoryJob = "widget-source";
                legacyContainerMarker = null;
                legacyGenerations = { };
              };
            };
            services.hermes-agent.profiles = {
              system-admin.serviceIntegrations = lib.mkForce { };
              builder = {
                externalServices.forgejo = {
                  endpoint = "https://code.example.test";
                  credentialFiles.credential = "/fixture/builder-token";
                  capabilities = [ "workflow" ];
                  scope.include = [ "acme/widgets" ];
                };
                externalServices.vikunja = {
                  endpoint = "https://tasks.example.test";
                  credentialFiles.credential = "/fixture/project-token";
                  capabilities = [ "workflow" ];
                  scope.include = [ "72" ];
                };
                serviceIntegrations = {
                  forgejo-event-ingest = true;
                  forgejo-to-kanban.settings = {
                    board = "engineering";

                  };
                  forgejo-pr-lifecycle = true;
                  kanban-to-vikunja.settings = {
                    board = "engineering";
                    projectOwner = "alex";
                    humanActionProject = 72;
                    humanActionProjectTitle = "Decisions";
                    projectPolicies."72".mode = "linkedOnly";
                  };
                };
              };
              auditor.agentSkills.forgejo-pr-lifecycle = true;
              auditor.externalServices.forgejo = {
                endpoint = "https://code.example.test";
                scope.include = [ "acme/widgets" ];
              };
            };
          }
        ];
      };
      secondContract = secondEval.config.custom.services.hermes.repositoryInstances.fixture;
      reconcile = eval.config.systemd.services.hermes-forgejo-kanban-reconcile;
      timer = eval.config.systemd.timers.hermes-forgejo-kanban-reconcile;
      systemAdminConfig = builtins.elemAt eval.config.systemd.services.hermes-agent-system-admin.restartTriggers 0;
      codeReviewerConfig = builtins.elemAt eval.config.systemd.services.hermes-agent-code-reviewer.restartTriggers 0;

      missingJournalSucceeds =
        (builtins.tryEval
          (eval.extendModules {
            modules = [
              {
                services.hermes-agent.profiles.system-admin.serviceIntegrations.forgejo-event-ingest =
                  lib.mkForce false;
              }
            ];
          }).config.system.build.toplevel
        ).success;
    in
    assert lib.all (a: a.assertion) secondEval.config.assertions;
    assert secondContract.repository == "acme/widgets";
    assert secondContract.default_branch == "trunk";
    assert secondContract.roles.reviewer == "auditor";
    assert secondContract.projection.project == 72;
    assert !missingJournalSucceeds;
    pkgs.runCommand "hermes-forgejo-kanban-eval" { } ''
      set -euo pipefail
      grep -F -- '--instance-config' ${secondEval.config.systemd.services.hermes-forgejo-kanban-reconcile.serviceConfig.ExecStart}
      grep -F -- 'forgejo-kanban-workflow' ${reconcile.serviceConfig.ExecStart}
      grep -F -- '--board homelab-devops' ${reconcile.serviceConfig.ExecStart}
      grep -F -- '--journal-state-root /var/lib/hermes-forgejo-event-journal' ${reconcile.serviceConfig.ExecStart}
      grep -F -- '--state-root /var/lib/hermes-forgejo-kanban-workflow' ${reconcile.serviceConfig.ExecStart}
      cp ${systemAdminConfig} "$TMPDIR/system-admin.yaml"
      cp ${codeReviewerConfig} "$TMPDIR/code-reviewer.yaml"
      grep -F -- '- kanban' "$TMPDIR/system-admin.yaml"
      grep -F -- '- kanban' "$TMPDIR/code-reviewer.yaml"
      test ${lib.escapeShellArg timer.timerConfig.OnUnitActiveSec} = 2m
      test ${lib.escapeShellArg reconcile.serviceConfig.StateDirectory} = hermes-forgejo-kanban-workflow
      test ${lib.escapeShellArg reconcile.serviceConfig.ProtectSystem} = strict
      test ${lib.escapeShellArg (lib.boolToString reconcile.serviceConfig.NoNewPrivileges)} = true
      touch $out
    '';

  hermesKanbanVikunjaCheck =
    let
      eval = inputs.nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = {
          inherit inputs;
          adminUser = "tester";
          hostname = "hermes-kanban-vikunja-eval";
          stateVersion = "25.11";
        };
        modules = [
          inputs.hermes-config.nixosModules.default
          inputs.hermes-config.nixosModules.incident-reconciliation
          inputs.sops-nix.nixosModules.sops
          {
            system.stateVersion = "25.11";
            fileSystems."/" = {
              device = "/dev/disk/by-label/nixos";
              fsType = "ext4";
            };
            boot.loader.grub.devices = [ "/dev/vda" ];
            users.users.tester.isNormalUser = true;
            custom.services.hermes = {
              managedSourceRepositories.project = {
                root = inputs.hermes-config.outPath;
                repository = "nixos/hermes-config";
                board = "fixture";
                assignee = "system-admin";
              };
              enable = true;
              uid = 989;
              gid = 985;
              defaultProfile.telegram.enable = false;
              profiles = {
                system-admin = {
                  telegram.enable = false;
                };
                code-reviewer.telegram.enable = false;
              };
            };
            services.hermes-agent.profiles.code-reviewer.externalServices.forgejo = {
              endpoint = "https://forgejo.example.test";
              credentialFiles.credential = "/test-secrets/reviewer";
              capabilities = [ "workflow" ];
              scope.include = [ "nixos/nix-config" ];
            };
            custom.services.hermes.repositoryAuthority = {
              ownerProfile = "system-admin";
              board = "homelab-devops";
              repositories.fixture = {
                stateSubdirectory = "";
                id = "fixture-nix-config";
                repository = "nixos/nix-config";
                defaultBranch = "master";
                namespace = "nix-config";
                workflowKeyPrefix = "forgejo";
                roles = {
                  implementer = "system-admin";
                  reviewer = "code-reviewer";
                };
                tenant = "homelab-devops";
                automationAuthors = [ "system-admin-agent" ];
                repositoryJob = null;
                legacyContainerMarker = null;
                legacyGenerations = { };
              };
            };
            services.hermes-agent.profiles.system-admin = {
              externalServices.vikunja = {
                endpoint = "https://todo.example.test/api/v1";
                credentialFiles.credential = "/test-secrets/vikunja-system-admin";
                capabilities = [ "workflow" ];
                scope.include = [ "110" ];
              };
              externalServices.forgejo = {
                endpoint = "https://forgejo.example.test";
                credentialFiles.credential = "/test-secrets/forgejo-system-admin";
                capabilities = [ "workflow" ];
                scope.include = [ "nixos/nix-config" ];
              };
              serviceIntegrations = {
                forgejo-event-ingest = true;
                forgejo-to-kanban.settings = {
                  board = "homelab-devops";

                };
                kanban-to-vikunja.settings = {
                  board = "homelab-devops";
                  interval = "2m";
                  defaultPolicy = "linkedOnly";
                  projectOwner = "sandro";
                  humanActionProject = 110;
                  humanActionProjectTitle = "PRs";
                  projectPolicies."110" = {
                    mode = "linkedOnly";
                    taskAllowlist = [ ];
                  };
                };
                vikunja-to-kanban.settings = {
                  actorUsername = "system-admin";
                  board = "homelab-devops";
                  interval = "3m";
                  defaultPolicy = "linkedOnly";
                  projectOwner = "sandro";
                  humanActionProject = 110;
                  humanActionProjectTitle = "PRs";
                  projectPolicies."110" = {
                    mode = "linkedOnly";
                    taskAllowlist = [ ];
                  };
                  wakeOnHumanComment = true;
                };
              };
            };
          }
        ];
      };
      k2v = eval.config.systemd.services.hermes-kanban-to-vikunja;
      v2k = eval.config.systemd.services.hermes-vikunja-to-kanban;
      k2vTimer = eval.config.systemd.timers.hermes-kanban-to-vikunja;
      v2kTimer = eval.config.systemd.timers.hermes-vikunja-to-kanban;
      codeReviewerConfig = builtins.elemAt eval.config.systemd.services.hermes-agent-code-reviewer.restartTriggers 0;
      assertionsPass = evaluated: lib.all (row: row.assertion) evaluated.config.assertions;
      assertionsSucceed =
        evaluated:
        (builtins.tryEval (
          assert assertionsPass evaluated;
          true
        )).success;
      missingProjectScopeSucceeds = assertionsSucceed (
        eval.extendModules {
          modules = [
            {
              services.hermes-agent.profiles.system-admin.externalServices.vikunja.scope.include =
                lib.mkForce
                  [ ];
            }
          ];
        }
      );
      projectWide110Succeeds = assertionsSucceed (
        eval.extendModules {
          modules = [
            {
              services.hermes-agent.profiles.system-admin.serviceIntegrations.kanban-to-vikunja.settings.projectPolicies."110".mode =
                lib.mkForce "projectWide";
            }
          ];
        }
      );
      splitDirectionalEval = eval.extendModules {
        modules = [
          {
            custom.services.hermes.profiles.split-owner.telegram.enable = false;
            services.hermes-agent.profiles = {
              system-admin.serviceIntegrations.vikunja-to-kanban = lib.mkForce false;
              split-owner = {
                externalServices.vikunja = {
                  endpoint = "https://todo.example.test/api/v1";
                  credentialFiles.credential = "/test-secrets/vikunja-split-owner";
                  capabilities = [ "workflow" ];
                  scope.include = [ "110" ];
                };
                serviceIntegrations.vikunja-to-kanban.settings = {
                  actorUsername = "split-owner";
                  board = "homelab-devops";
                  interval = "4m";
                  projectOwner = "sandro";
                  humanActionProject = 110;
                  humanActionProjectTitle = "PRs";
                  projectPolicies."110" = {
                    mode = "linkedOnly";
                    taskAllowlist = [ ];
                  };
                };
              };
            };
          }
        ];
      };
      splitV2k = splitDirectionalEval.config.systemd.services.hermes-vikunja-to-kanban;
      lifecycleEval = eval.extendModules {
        modules = [
          {
            custom.services.hermes.managedSourceRepositories.project = lib.mkForce {
              root = inputs.hermes-config.outPath;
              board = "homelab-devops";
              assignee = "system-admin";
              repository = "nixos/nix-config";
            };
            services.hermes-agent.profiles.system-admin = {
              serviceIntegrations.forgejo-pr-lifecycle = true;
              externalServices.forgejo = {
                endpoint = "https://forgejo.example.test";
                credentialFiles.credential = "/test-secrets/forgejo-system-admin";
                capabilities = [ "workflow" ];
                scope.include = [ "nixos/nix-config" ];
              };
            };
          }
        ];
      };
      lifecycle = lifecycleEval.config.systemd.services.hermes-kanban-to-vikunja;
      missingForgejoSucceeds = assertionsSucceed (
        lifecycleEval.extendModules {
          modules = [
            {
              services.hermes-agent.profiles.system-admin.externalServices.forgejo.scope.include =
                lib.mkForce
                  [ ];
            }
          ];
        }
      );

    in
    assert lib.assertMsg (assertionsPass lifecycleEval) (
      lib.concatStringsSep "\n" (
        map (row: row.message) (builtins.filter (row: !row.assertion) lifecycleEval.config.assertions)
      )
    );
    assert !missingForgejoSucceeds;
    assert builtins.elem "forgejo-credential:/test-secrets/forgejo-system-admin"
      lifecycle.serviceConfig.LoadCredential;
    assert !missingProjectScopeSucceeds;
    assert !projectWide110Succeeds;
    assert assertionsPass splitDirectionalEval;
    assert
      splitV2k.serviceConfig.LoadCredential == [ "vikunja-credential:/test-secrets/vikunja-split-owner" ];
    assert
      k2v.serviceConfig.LoadCredential == [ "vikunja-credential:/test-secrets/vikunja-system-admin" ];
    assert
      v2k.serviceConfig.LoadCredential == [ "vikunja-credential:/test-secrets/vikunja-system-admin" ];
    assert k2v.serviceConfig.StateDirectory == "hermes-kanban-vikunja-projection";
    assert v2k.serviceConfig.StateDirectory == "hermes-kanban-vikunja-projection";
    assert k2vTimer.timerConfig.OnUnitActiveSec == "2m";
    assert v2kTimer.timerConfig.OnUnitActiveSec == "3m";
    pkgs.runCommand "hermes-kanban-vikunja-eval" { } ''
            set -euo pipefail
      grep -F -- '--pr-lifecycle' ${lifecycle.serviceConfig.ExecStart}
      grep -F -- '--forgejo-endpoint https://forgejo.example.test' ${lifecycle.serviceConfig.ExecStart}
      ! grep -F -- '--forgejo' ${k2v.serviceConfig.ExecStart}
      grep -F -- 'https://todo.example.test/api/v1' ${splitV2k.serviceConfig.ExecStart}
      grep -F -- '--actor-username split-owner' ${splitV2k.serviceConfig.ExecStart}
      grep -F -- 'kanban-vikunja-projection' ${k2v.serviceConfig.ExecStart}
      grep -F -- 'kanban-to-vikunja' ${k2v.serviceConfig.ExecStart}
      grep -F -- 'vikunja-to-kanban' ${v2k.serviceConfig.ExecStart}
      grep -F -- '--credential-file %d/vikunja-credential' ${k2v.serviceConfig.ExecStart}
            cp ${codeReviewerConfig} "$TMPDIR/code-reviewer.yaml"
            ! grep -F 'vikunja' "$TMPDIR/code-reviewer.yaml"
            test ${lib.escapeShellArg k2v.serviceConfig.ProtectSystem} = strict
            test ${lib.escapeShellArg v2k.serviceConfig.ProtectSystem} = strict
            test ${lib.escapeShellArg (lib.boolToString k2v.serviceConfig.NoNewPrivileges)} = true
            test ${lib.escapeShellArg (lib.boolToString v2k.serviceConfig.NoNewPrivileges)} = true
            ! grep -R -E '(sqlite|hermes cron|delegate_task|kanban_complete)' \
              ${(inputs.hermes-config + "/pkgs/common/kanban-vikunja-projection/src")}
            K2V_EXEC=${lib.escapeShellArg k2v.serviceConfig.ExecStart} \
              V2K_EXEC=${lib.escapeShellArg v2k.serviceConfig.ExecStart} \
              PYTHONDONTWRITEBYTECODE=1 \
              PYTHONPATH=${
                lib.makeSearchPath pkgs.python3.sitePackages (
                  lib.closePropagation [ kanbanVikunjaProjectionPackage ]
                )
              } \
              ${pkgs.python3}/bin/python3 -c '
      import json
      import os
      import shlex
      from pathlib import Path
      from hermes_repository_instance import load_instance, instance_scope
      from hermes_workflow_state import ProtocolError
      from kanban_vikunja_projection import validate_config, InputError
      from pr_projection_lifecycle import PrLifecycle
      from pr_legacy_adoption import legacy_identity
      os.environ.pop("HERMES_REPOSITORY_INSTANCE", None)
      for variable in ("K2V_EXEC", "V2K_EXEC"):
          argv = shlex.split(Path(os.environ[variable]).read_text().splitlines()[-1])
          config_path = argv[argv.index("--config") + 1]
          instance_path = Path(argv[argv.index("--instance-config") + 1])
          with open(config_path, encoding="utf-8") as handle:
              config = json.load(handle)
          try:
              validate_config(config)
          except ProtocolError as exc:
              assert "explicit repository instance" in str(exc), str(exc)
          else:
              raise AssertionError("missing instance unexpectedly accepted")
          # Use the actual generated service binding, not an unrelated fixture
          # or a default that could conceal broken module-to-CLI wiring.
          with instance_scope(load_instance(instance_path)):
              validate_config(config)
              for field in ("board", "project_owner"):
                  mismatched = dict(config, **{field: "unrelated"})
                  try:
                      validate_config(mismatched)
                  except InputError as exc:
                      assert "owner/board differs" in str(exc), str(exc)
                  else:
                      raise AssertionError("foreign instance binding accepted")
          print("Verified instance binding and rejection controls:", variable)
      '
            touch $out
    '';

in
{
  hermes-forgejo-event-ingest-eval = hermesForgejoEventIngestCheck;
  hermes-forgejo-kanban-eval = hermesForgejoKanbanCheck;
  hermes-kanban-vikunja-eval = hermesKanbanVikunjaCheck;
}
