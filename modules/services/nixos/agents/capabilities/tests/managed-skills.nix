{ lib }:
let
  inventory = import ../managed-skills.nix {
    inherit lib;
    repositories.project = {
      root = ../../../../../..;
      repository = "example/config";
      board = "test-board";
      assignee = "test-owner";
      mode = "kanban";
    };
  };
  ordinary = {
    example = {
      category = "example";
      source = ../../skills/evidence-driven-change-control;
      content = null;
      declarationFiles = [ (toString ./managed-skills.nix) ];
    };
  };
  skills = ordinary;
  manifest = inventory.manifest {
    profile = "alpha";
    inherit skills;
    providers = { };
  };
in
assert builtins.attrNames skills == [ "example" ];
assert manifest.native_jobs == { };
assert manifest.schema_version == 2;
assert manifest.profile == "alpha";
assert builtins.attrNames manifest.skills == builtins.attrNames skills;
assert manifest.skills.example.source.kind == "repository-directory";
assert
  manifest.skills.example.source.path
  == "modules/services/nixos/agents/skills/evidence-driven-change-control";
assert (builtins.head manifest.skills.example.declarations).repository == "example/config";
assert
  (builtins.head manifest.skills.example.declarations).path
  == "modules/services/nixos/agents/capabilities/tests/managed-skills.nix";
true
