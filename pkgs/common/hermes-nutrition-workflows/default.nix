{
  hermes-workflow-state,
  lib,
  makeWrapper,
  python3,
}:
python3.pkgs.buildPythonApplication {
  pname = "hermes-nutrition-workflows";
  version = "0.1.0";
  pyproject = false;

  nativeBuildInputs = [ makeWrapper ];
  propagatedBuildInputs = [ hermes-workflow-state ];
  dontBuild = true;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/${python3.sitePackages}" "$out/bin" "$out/share/hermes-nutrition-workflows"
    install -m 0644 ${./.}/src/hermes_nutrition_workflows.py \
      "$out/${python3.sitePackages}/hermes_nutrition_workflows.py"
    install -m 0644 ${./preferences.example.json} \
      "$out/share/hermes-nutrition-workflows/preferences.example.json"
    makeWrapper ${python3.interpreter} "$out/bin/hermes-nutrition-workflows" \
      --add-flags "$out/${python3.sitePackages}/hermes_nutrition_workflows.py" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}:${hermes-workflow-state}/${python3.sitePackages}"

    runHook postInstall
  '';

  checkPhase = ''
    runHook preCheck
    PYTHONPATH="${./.}/src:${hermes-workflow-state}/${python3.sitePackages}" \
      ${python3.interpreter} -m unittest discover -s ${./.}/tests -v
    runHook postCheck
  '';

  pythonImportsCheck = [
    "hermes_nutrition_workflows"
    "hermes_workflow_state"
  ];

  meta = {
    description = "Private deterministic nutrition and measurement workflow gates";
    license = lib.licenses.mit;
    mainProgram = "hermes-nutrition-workflows";
    platforms = lib.platforms.unix;
  };
}
