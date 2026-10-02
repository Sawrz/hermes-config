{
  lib,
  python3,
}:

python3.pkgs.buildPythonApplication {
  pname = "hermes-workflow-state";
  version = "0.1.0";
  pyproject = false;
  src = ./.;

  dontBuild = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/${python3.sitePackages}" "$out/bin"
    install -m 0644 src/hermes_workflow_state.py "$out/${python3.sitePackages}/hermes_workflow_state.py"
    install -m 0644 src/hermes_repository_instance.py "$out/${python3.sitePackages}/hermes_repository_instance.py"
    install -m 0755 src/hermes_workflow_preferences.py "$out/bin/hermes-workflow-preferences"
    install -m 0755 src/hermes_repository_task.py "$out/bin/hermes-repository-task"

    # Imports after fixup must not create timestamp-based bytecode in the output.
    ${python3.interpreter} -m compileall -q --invalidation-mode checked-hash \
      "$out/${python3.sitePackages}"

    runHook postInstall
  '';

  checkPhase = ''
    runHook preCheck
    PYTHONPATH="$PWD/src" ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  pythonImportsCheck = [
    "hermes_workflow_state"
    "hermes_repository_instance"
  ];

  meta = {
    description = "Crash-safe state and bounded preferences for deterministic Hermes integrations";
    license = lib.licenses.mit;
    mainProgram = "hermes-workflow-preferences";
    platforms = lib.platforms.unix;
  };
}
