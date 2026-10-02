{ lib, ... }:
let
  capabilityLib = import ./lib.nix { inherit lib; };

  bindingSafety = {
    credentialRequirements = [
      {
        service = "wger";
        field = "credential";
      }
    ];
    pathRequirements = [ ];
    writeScope = "external-system";
    confirmation = "operator-required";
    verification = "external-readback-required";
  };

  wgerApi = capabilityLib.mkAgentSkill "wger-api" {
    provides.skills.wger-api = {
      category = "health";
      source = ../skills/wger-api;
    };
  };

  nutritionistBinding = capabilityLib.mkExternalServiceCapability "wger" "nutritionist" {
    requires = [ wgerApi.id ];
    safety = bindingSafety;
  };

  fitnessCoachBinding = capabilityLib.mkExternalServiceCapability "wger" "fitness-coach" {
    requires = [ wgerApi.id ];
    safety = bindingSafety;
  };
in
{
  config.custom.services.hermes.capabilityPackages = {
    ${wgerApi.id} = wgerApi;
    ${nutritionistBinding.id} = nutritionistBinding;
    ${fitnessCoachBinding.id} = fitnessCoachBinding;
  };
}
