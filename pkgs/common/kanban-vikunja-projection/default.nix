{
  forgejo-kanban-workflow,
  hermes-workflow-state,
  lib,
  makeWrapper,
  python3,
}:
let
  importPath = lib.makeSearchPath python3.sitePackages (
    lib.closePropagation [
      hermes-workflow-state
      forgejo-kanban-workflow
    ]
  );
in
python3.pkgs.buildPythonApplication {
  pname = "kanban-vikunja-projection";
  version = "0.1.0";
  pyproject = false;
  src = ./.;

  dontBuild = true;
  propagatedBuildInputs = [
    hermes-workflow-state
    forgejo-kanban-workflow
  ];

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/${python3.sitePackages}" "$out/bin"
    install -m 0644 src/kanban_vikunja_projection.py \
      "$out/${python3.sitePackages}/kanban_vikunja_projection.py"
    install -m 0644 src/pr_projection_lifecycle.py \
      "$out/${python3.sitePackages}/pr_projection_lifecycle.py"
    install -m 0644 src/pr_legacy_adoption.py \
      "$out/${python3.sitePackages}/pr_legacy_adoption.py"
    makeWrapper ${python3.interpreter} "$out/bin/kanban-vikunja-projection" \
      --add-flags "$out/${python3.sitePackages}/kanban_vikunja_projection.py" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}:${importPath}"
    # Import checks run after fixup; precompile by source hash so they cannot
    # embed this build's installation timestamps in the output bytecode.
    ${python3.interpreter} -m compileall -q --invalidation-mode checked-hash \
      "$out/${python3.sitePackages}"
    runHook postInstall
  '';

  nativeBuildInputs = [ makeWrapper ];

  checkPhase = ''
    runHook preCheck
    export HERMES_REPOSITORY_INSTANCE=${../hermes-workflow-state/tests/fixtures/instance.json}
    PYTHONPATH="$PWD/src:${importPath}" \
      ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  pythonImportsCheck = [
    "kanban_vikunja_projection"
    "pr_projection_lifecycle"
    "pr_legacy_adoption"
  ];

  meta = {
    description = "Deterministic native Kanban and Vikunja human-action projection";
    license = lib.licenses.mit;
    mainProgram = "kanban-vikunja-projection";
    platforms = lib.platforms.linux;
  };
}
