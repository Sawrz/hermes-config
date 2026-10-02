# One immutable execution environment per resolved profile. No credentials or
# profile state are inputs. The private store contains only this runtime closure.
{ lib, pkgs }:
{
  name,
  skills,
  runtimePackages,
  extraStorePaths ? [ ],
}:
let
  root = "/run/hermes-capabilities";
  packages = lib.unique ([ pkgs.python3 ] ++ runtimePackages);
  runtime = pkgs.buildEnv {
    name = "hermes-${name}-helper-runtime";
    paths = packages;
    pathsToLink = [ "/bin" ];
    postBuild = ''mkdir -p "$out/bin"'';
  };
  checks = map (package: {
    cwd = root;
    argv = [
      (lib.getExe package)
      "--help"
    ];
  }) runtimePackages;
  manifest = pkgs.writeText "hermes-${name}-helper-checks.json" (builtins.toJSON checks);
  tree = pkgs.runCommand "hermes-${name}-helpers" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    mkdir -p "$out"
    ${lib.concatStringsSep "\n" (
      lib.mapAttrsToList (skillName: skill: ''
        mkdir "$out/${skillName}"
        ${
          if skill.source != null then
            ''
              # Dereference the entire skill, including modules, assets and templates.
              cp -RL --preserve=mode ${lib.escapeShellArg "${skill.source}"}/. "$out/${skillName}/"
            ''
          else
            ''
              cp ${pkgs.writeText "SKILL.md" skill.content} "$out/${skillName}/SKILL.md"
            ''
        }
      '') skills
    )}
    ${pkgs.python3}/bin/python3 ${./validate-sandbox-skill.py} "$out" > "$TMPDIR/script-checks.json"
    ${pkgs.python3}/bin/python3 - "$TMPDIR/script-checks.json" ${manifest} "$out/checks.json" <<'PY'
    import json, sys
    from pathlib import Path
    Path(sys.argv[3]).write_text(json.dumps(json.loads(Path(sys.argv[1]).read_text()) + json.loads(Path(sys.argv[2]).read_text())))
    PY
    ln -s ${runtime}/bin "$out/bin"
    cp ${./sandbox-readiness.py} "$out/readiness.py"
  '';
  closure = pkgs.closureInfo {
    rootPaths = [
      runtime
      pkgs.python3
    ]
    ++ extraStorePaths;
  };
  store = pkgs.runCommand "hermes-${name}-helper-store" { } ''
    mkdir -p "$out"
    while IFS= read -r path; do
      cp -a "$path" "$out/"
    done < ${closure}/store-paths
  '';
in
{
  inherit tree store checks;
  volumes = [
    "${tree}:${root}:ro"
    "${store}:/nix/store:ro"
  ];
  readiness = [
    "${pkgs.python3}/bin/python3"
    "${root}/readiness.py"
  ];
}
