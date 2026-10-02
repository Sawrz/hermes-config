{ lib, ... }:
let
  packages = import ./foundation-packages.nix { inherit lib; };
in
{
  config.custom.services.hermes.capabilityPackages = builtins.listToAttrs (
    map (package: {
      name = package.id;
      value = package;
    }) packages.all
  );
}
