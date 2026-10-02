{ lib, ... }:
let
  capabilityLib = import ./lib.nix { inherit lib; };

  minifluxSource = capabilityLib.mkAgentSkill "miniflux-source" {
    provides.skills.miniflux-source = {
      category = "productivity";
      source = ../skills/miniflux-source;
    };
  };

  sourceManagement = capabilityLib.mkExternalServiceCapability "miniflux" "source-management" {
    requires = [ minifluxSource.id ];
    safety = {
      credentialRequirements = [
        {
          service = "miniflux";
          field = "credential";
        }
      ];
      pathRequirements = [ ];
      writeScope = "external-system";
      confirmation = "operator-required";
      verification = "external-readback-required";
    };
  };
in
{
  config.custom.services.hermes.capabilityPackages = {
    ${minifluxSource.id} = minifluxSource;
    ${sourceManagement.id} = sourceManagement;
  };
}
