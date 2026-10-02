{
  self,
  inputs,
  pkgs,
  system,
}:
let
  inherit (pkgs) lib;
  declaration = name: {
    id = "fixture-${name}";
    repository = "example/${name}";
    defaultBranch = if name == "deployment" then "master" else "main";
    namespace = name;
    workflowKeyPrefix = "forgejo";
    roles = {
      implementer = "system-admin";
      reviewer = "code-reviewer";
    };
    tenant = "fixture";
    automationAuthors = [ ];
    repositoryJob = "${name}-pull";
    legacyContainerMarker = null;
    legacyGenerations = { };
    stateSubdirectory = if name == "deployment" then "" else "/repositories/${name}";
  };
  fixture = inputs.nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [
      self.nixosModules.default
      {
        system.stateVersion = "25.11";
        custom.services.hermes = {
          repositoryAuthority = {
            ownerProfile = "system-admin";
            board = "fixture";
            repositories = {
              deployment = declaration "deployment";
              project = declaration "project";
            };
          };
          enable = true;
          uid = 989;
          gid = 985;
          defaultProfile.telegram.enable = false;
          profiles.system-admin.telegram.enable = false;
          profiles.code-reviewer.telegram.enable = false;
          managedSourceRepositories.project = {
            root = self.outPath;
            repository = "example/project";
            board = "fixture";
            assignee = "system-admin";
          };
        };
        services.hermes-agent.profiles = {
          system-admin = {
            externalServices.forgejo = {
              endpoint = "https://forgejo.example.test";
              credentialFiles.credential = "/fixture/implementer-token";
              capabilities = [ "workflow" ];
              scope.include = [
                "example/deployment"
                "example/project"
              ];
            };
            serviceIntegrations = {
              forgejo-event-ingest = true;
              forgejo-to-kanban.settings = {
                board = "fixture";

              };
            };
          };
          code-reviewer.externalServices.forgejo = {
            endpoint = "https://forgejo.example.test";
            credentialFiles.credential = "/fixture/reviewer-token";
            capabilities = [ "workflow" ];
            scope.include = [
              "example/deployment"
              "example/project"
            ];
          };
        };
      }
    ];
  };
  validRepositoryAssertions =
    candidate:
    lib.all (a: a.assertion) (
      (import ../modules/services/nixos/agents/repository-instance.nix {
        config = candidate.config;
        inherit lib pkgs;
      }).config.content.assertions
    );
  duplicate = fixture.extendModules {
    modules = [
      {
        custom.services.hermes.repositoryAuthority.repositories.project.stateSubdirectory = lib.mkForce "";
      }
    ];
  };
  wrongScope = fixture.extendModules {
    modules = [
      {
        services.hermes-agent.profiles.code-reviewer.externalServices.forgejo.scope.include = lib.mkForce [
          "example/deployment"
        ];
      }
    ];
  };
  wrongOrigin = fixture.extendModules {
    modules = [
      {
        services.hermes-agent.profiles.code-reviewer.externalServices.forgejo.endpoint =
          lib.mkForce "https://wrong.example.test";
      }
    ];
  };
in
assert validRepositoryAssertions fixture;
assert !(validRepositoryAssertions duplicate);
assert !(validRepositoryAssertions wrongScope);
assert !(validRepositoryAssertions wrongOrigin);
assert fixture.config.custom.services.hermes.repositoryStateSubdirectories.deployment == "";
assert fixture.config.custom.services.hermes.repositoryInstances.project.default_branch == "main";
pkgs.runCommand "hermes-repository-routing" { nativeBuildInputs = [ pkgs.python3 ]; } ''
  python3 ${../modules/services/nixos/agents}/capabilities/tests/test_managed_skill_request.py
  touch $out
''
