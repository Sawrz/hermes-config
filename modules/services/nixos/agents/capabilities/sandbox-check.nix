{ pkgs }:
let
  inherit (pkgs) lib;
  capabilityLib = import ./lib.nix { inherit lib; };
  packageModules = [
    ./foundation-skills.nix
    ./cooking-apis.nix
    ./wger-api.nix
    ./miniflux-source.nix
    ./vikunja-task-reviews.nix
    ./nutrition-workflows.nix
    ./repository-sync.nix
    ./nix-maintenance-runtime.nix
    ./digest.nix
    ./jobs.nix
  ];
  packages = lib.foldl' (
    acc: module:
    acc // (import module { inherit lib pkgs; }).config.custom.services.hermes.capabilityPackages
  ) { } packageModules;
  roots = [
    "externalService:mealie:recipes"
    "externalService:mealie:meal-plans"
    "externalService:grocy:pantry"
    "externalService:wger:nutritionist"
    "externalService:miniflux:source-management"
    "externalService:vikunja:task-reviews"
    "agentSkill:daily-intake-closeout"
    "serviceIntegration:repository-sync"
    "serviceIntegration:nix-maintenance-runtime"
    "serviceIntegration:digest"
    "serviceIntegration:job-discovery"
  ];
  resolve = capabilityLib.resolve packages;
  selected = resolve roots;
  mk =
    name: closure:
    (import ./sandbox.nix { inherit lib pkgs; }) {
      inherit name;
      skills = lib.mapAttrs (_: skill: { content = null; } // skill) closure.skills;
      inherit (closure) runtimePackages;
    };
  profiles =
    lib.mapAttrs
      (
        name: closure:
        let
          publication = mk name closure;
        in
        {
          inherit (publication) volumes readiness;
        }
      )
      {
        inherit selected;
        independent = resolve [ "agentSkill:vikunja-task-reviews" ];
        unrelated = resolve [ ];
      };
  image = pkgs.dockerTools.buildImage {
    name = "hermes-helper-regression";
    tag = "test";
    extraCommands = ''
      mkdir -p bin
      cp ${pkgs.pkgsStatic.busybox}/bin/busybox bin/busybox
      for tool in sh sleep test touch ls; do ln -s busybox "bin/$tool"; done
    '';
    config.Env = [ "PATH=/bin" ];
    config.Cmd = [
      "sleep"
      "infinity"
    ];
  };
  manifest = pkgs.writeText "helper-regression.json" (
    builtins.toJSON {
      inherit profiles;
      image = "hermes-helper-regression:test";
      gatewaySkill = "${../skills/mealie-api}";
    }
  );
in
pkgs.runCommand "hermes-sandbox-helper-regression" { } ''
  mkdir -p "$out"
  ln -s ${manifest} "$out/manifest.json"
  ln -s ${image} "$out/image.tar.gz"
  cp ${./tests/test_sandbox.py} "$out/test_sandbox.py"
  cat > "$out/run" <<EOF
  #!${pkgs.runtimeShell}
  exec ${pkgs.python3}/bin/python3 "$out/test_sandbox.py" "$out/manifest.json"
  EOF
  chmod +x "$out/run"
''
