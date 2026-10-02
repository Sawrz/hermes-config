{ lib }:
let
  capabilityLib = import ../lib.nix { inherit lib; };
  foundation = import ../foundation-packages.nix { inherit lib; };
  packages = builtins.listToAttrs (
    map (p: {
      name = p.id;
      value = p;
    }) foundation.all
  );
  skills = roots: builtins.attrNames (capabilityLib.resolve packages roots).skills;
in
assert skills [ "agentSkill:repository-knowledge" ] == [ "repository-knowledge" ];
assert
  skills [ "agentSkill:forgejo-pr-lifecycle" ] == [
    "forgejo-pr-lifecycle"
    "repository-knowledge"
  ];
assert
  skills [ "agentSkill:nix-maintenance-protocol" ] == [
    "nix-maintenance-protocol"
    "repository-knowledge"
  ];
assert
  skills [
    "agentSkill:forgejo-pr-lifecycle"
    "agentSkill:nix-maintenance-protocol"
  ] == [
    "forgejo-pr-lifecycle"
    "nix-maintenance-protocol"
    "repository-knowledge"
  ];
# Removing the last workflow root removes its artifacts; an independently
# selected knowledge root remains. Profile-owned files are never publications.
assert skills [ ] == [ ];
assert
  skills [ "agentSkill:nix-config-pr-lifecycle" ] == [
    "forgejo-pr-lifecycle"
    "nix-config-pr-lifecycle"
    "repository-knowledge"
  ];
true
