{ lib, ... }:
let
  capabilityLib = import ./lib.nix { inherit lib; };

  mealieApi = capabilityLib.mkAgentSkill "mealie-api" {
    provides.skills.mealie-api = {
      category = "cooking";
      source = ../skills/mealie-api;
    };
  };

  grocyApi = capabilityLib.mkAgentSkill "grocy-api" {
    provides.skills.grocy-api = {
      category = "cooking";
      source = ../skills/grocy-api;
    };
  };

  mealieSafety = {
    credentialRequirements = [
      {
        service = "mealie";
        field = "credential";
      }
    ];
    pathRequirements = [
      {
        path = "$HERMES_HOME/locks/mealie-api";
        access = "read-write";
      }
    ];
    writeScope = "external-system";
    confirmation = "operator-required";
    verification = "external-readback-required";
  };

  grocySafety = {
    credentialRequirements = [
      {
        service = "grocy";
        field = "credential";
      }
    ];
    pathRequirements = [
      {
        path = "$HERMES_HOME/locks/grocy-api";
        access = "read-write";
      }
    ];
    writeScope = "external-system";
    confirmation = "operator-required";
    verification = "external-readback-required";
  };

  mealieRecipes = capabilityLib.mkExternalServiceCapability "mealie" "recipes" {
    requires = [ mealieApi.id ];
    safety = mealieSafety;
  };

  mealieMealPlans = capabilityLib.mkExternalServiceCapability "mealie" "meal-plans" {
    requires = [ mealieApi.id ];
    safety = mealieSafety;
  };

  grocyPantry = capabilityLib.mkExternalServiceCapability "grocy" "pantry" {
    requires = [ grocyApi.id ];
    safety = grocySafety;
  };

  grocyShoppingList = capabilityLib.mkExternalServiceCapability "grocy" "shopping-list" {
    requires = [ grocyApi.id ];
    safety = grocySafety;
  };

in
{
  config.custom.services.hermes.capabilityPackages = {
    ${mealieApi.id} = mealieApi;
    ${grocyApi.id} = grocyApi;
    ${mealieRecipes.id} = mealieRecipes;
    ${mealieMealPlans.id} = mealieMealPlans;
    ${grocyPantry.id} = grocyPantry;
    ${grocyShoppingList.id} = grocyShoppingList;
  };
}
