{ self, pkgs }:
let
  inputs = self.inputs // {
    hermes-config = self;
  };
  inherit (pkgs) lib;
  system = pkgs.stdenv.hostPlatform.system;
  hermesCookingApiFrameworkCheck =
    let
      cookingEval = inputs.nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = {
          inherit inputs;
          adminUser = "tester";
          hostname = "hermes-cooking-api-framework-eval";
          stateVersion = "25.11";
        };
        modules = [
          inputs.hermes-config.nixosModules.default
          inputs.sops-nix.nixosModules.sops
          {
            system.stateVersion = "25.11";
            users.users.tester.isNormalUser = true;
            custom.services.hermes = {
              enable = true;
              uid = 989;
              gid = 985;
              defaultProfile.telegram.enable = false;
              profiles = {
                chef.telegram.enable = false;
                unrelated.telegram.enable = false;
              };
            };
            services.hermes-agent.profiles.chef = {
              externalServices = {
                mealie = {
                  endpoint = "https://mealie.example.test";
                  credentialFiles.credential = "/test-secrets/mealie";
                  capabilities = [
                    "recipes"
                    "meal-plans"
                  ];
                };
                grocy = {
                  endpoint = "https://grocy.example.test/api";
                  credentialFiles.credential = "/test-secrets/grocy";
                  capabilities = [
                    "pantry"
                    "shopping-list"
                  ];
                };
              };
            };
          }
        ];
      };
      chefConfig = builtins.elemAt cookingEval.config.systemd.services.hermes-agent-chef.restartTriggers 0;
      unrelatedConfig = builtins.elemAt cookingEval.config.systemd.services.hermes-agent-unrelated.restartTriggers 0;
      chefPreStart = cookingEval.config.systemd.services.hermes-agent-chef.preStart;
      chefSkillDir = builtins.head (
        builtins.filter (
          path: lib.hasInfix "declarative-skills" (toString path)
        ) cookingEval.config.systemd.services.hermes-agent-chef.restartTriggers
      );
      cookingPackages = cookingEval.config.custom.services.hermes.capabilityPackages;
    in
    assert cookingPackages ? "agentSkill:mealie-api";
    assert cookingPackages ? "agentSkill:grocy-api";
    assert cookingPackages ? "externalService:mealie:recipes";
    assert cookingPackages ? "externalService:mealie:meal-plans";
    assert cookingPackages ? "externalService:grocy:pantry";
    assert cookingPackages ? "externalService:grocy:shopping-list";
    pkgs.runCommand "nixos-hermes-cooking-api-framework" { } ''
      set -euo pipefail
      cp ${chefConfig} "$TMPDIR/chef-config"
      cp ${unrelatedConfig} "$TMPDIR/unrelated-config"
      printf '%s\n' ${lib.escapeShellArg chefPreStart} > "$TMPDIR/chef-pre-start"

      grep -F 'https://mealie.example.test' "$TMPDIR/chef-pre-start"
      grep -F '/test-secrets/mealie' "$TMPDIR/chef-pre-start"
      grep -F 'https://grocy.example.test/api' "$TMPDIR/chef-pre-start"
      grep -F '/test-secrets/grocy' "$TMPDIR/chef-pre-start"
      grep -F '/run/user/989/hermes-credentials/chef/services:/run/hermes-credentials/services:ro' "$TMPDIR/chef-config"
      grep -F 'external_dirs:' "$TMPDIR/chef-config"
      ! grep -E 'MEALIE_|GROCY_' "$TMPDIR/chef-config"
      ! grep -E -- '--env-file.*(mealie|grocy)|(mealie|grocy).*--env-file' "$TMPDIR/chef-config"
      ! grep -E 'mealie-api|grocy-api' "$TMPDIR/unrelated-config"

      test -f ${chefSkillDir}/cooking/mealie-api/SKILL.md
      test -f ${chefSkillDir}/cooking/mealie-api/scripts/mealie_api.py
      test -f ${chefSkillDir}/cooking/grocy-api/SKILL.md
      test -f ${chefSkillDir}/cooking/grocy-api/scripts/grocy_api.py
      cp -R ${
        (inputs.hermes-config + "/modules/services/nixos/agents/skills/mealie-api")
      } "$TMPDIR/mealie-api"
      cp -R ${
        (inputs.hermes-config + "/modules/services/nixos/agents/skills/grocy-api")
      } "$TMPDIR/grocy-api"
      chmod -R u+w "$TMPDIR/mealie-api" "$TMPDIR/grocy-api"
      ${pkgs.python3}/bin/python3 -m unittest discover "$TMPDIR/mealie-api/tests" -v
      ${pkgs.python3}/bin/python3 -m unittest discover "$TMPDIR/grocy-api/tests" -v
      touch $out
    '';
  hermesVikunjaReviewCheck = pkgs.runCommand "hermes-vikunja-review" { } ''
    set -euo pipefail
    export PYTHONDONTWRITEBYTECODE=1
    export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
    export PYTHONPATH=${(inputs.hermes-config + "/pkgs/common/hermes-vikunja-review/src")}:${
      (inputs.hermes-config + "/pkgs/common/hermes-workflow-state/src")
    }
    ${pkgs.python3}/bin/python3 -m unittest discover \
      -s ${(inputs.hermes-config + "/pkgs/common/hermes-vikunja-review/tests")} -v
    ${pkgs.python3}/bin/python3 -m py_compile \
      ${(inputs.hermes-config + "/pkgs/common/hermes-vikunja-review/src/hermes_vikunja_review.py")}
    ${pkgs.python3}/bin/python3 - <<'PY'
    import json
    from pathlib import Path
    from hermes_workflow_state import validate_preference_contract
    contract = json.loads(Path("${
      (inputs.hermes-config + "/pkgs/common/hermes-vikunja-review/review-preferences.json")
    }").read_text())
    validate_preference_contract(contract)
    PY
    touch $out
  '';

  hermesNutritionWorkflowsCheck = pkgs.runCommand "hermes-nutrition-workflows" { } ''
    set -euo pipefail
    cp -R ${
      (inputs.hermes-config + "/pkgs/common/hermes-nutrition-workflows")
    } "$TMPDIR/nutrition-workflows"
    cp -R ${(inputs.hermes-config + "/pkgs/common/hermes-workflow-state/src")} "$TMPDIR/workflow-state"
    chmod -R u+w "$TMPDIR/nutrition-workflows" "$TMPDIR/workflow-state"
    PYTHONPATH="$TMPDIR/nutrition-workflows/src:$TMPDIR/workflow-state" \
      ${pkgs.python3}/bin/python3 -m unittest discover \
        -s "$TMPDIR/nutrition-workflows/tests" -v
    touch $out
  '';

  hermesVerificationHarnessCheck = pkgs.runCommand "hermes-verification-harness" { } ''
    set -euo pipefail
    cp -R ${
      (inputs.hermes-config + "/modules/services/nixos/agents/verification-harness")
    } "$TMPDIR/verification-harness"
    chmod -R u+w "$TMPDIR/verification-harness"
    ${pkgs.python3}/bin/python3 -m unittest discover \
      "$TMPDIR/verification-harness/tests" -v
    ${pkgs.python3}/bin/python3 "$TMPDIR/verification-harness/verify.py" \
      "$TMPDIR/verification-harness/fixtures/healthy-no-change.json"
    ${pkgs.python3}/bin/python3 "$TMPDIR/verification-harness/verify.py" \
      "$TMPDIR/verification-harness/fixtures/real-change-positive-control.json"
    touch $out
  '';

  hermesWgerApiFrameworkCheck = pkgs.runCommand "hermes-wger-api-framework" { } ''
    set -euo pipefail
    cp -R ${
      (inputs.hermes-config + "/modules/services/nixos/agents/skills/wger-api")
    } "$TMPDIR/wger-api"
    chmod -R u+w "$TMPDIR/wger-api"
    ${pkgs.python3}/bin/python3 -m py_compile \
      "$TMPDIR/wger-api/scripts/wger_api.py" \
      "$TMPDIR/wger-api/tests/test_wger_api.py"
    ${pkgs.python3}/bin/python3 -m unittest discover \
      "$TMPDIR/wger-api/tests" -v
    touch $out
  '';

in
{
  hermesCookingApiFrameworkCheck = hermesCookingApiFrameworkCheck;
  hermesVikunjaReviewCheck = hermesVikunjaReviewCheck;
  hermesNutritionWorkflowsCheck = hermesNutritionWorkflowsCheck;
  hermesVerificationHarnessCheck = hermesVerificationHarnessCheck;
  hermesWgerApiFrameworkCheck = hermesWgerApiFrameworkCheck;
}
