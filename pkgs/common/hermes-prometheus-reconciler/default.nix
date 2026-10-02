{
  hermes-workflow-state,
  lib,
  makeWrapper,
  python3,
}:
python3.pkgs.buildPythonApplication {
  pname = "hermes-prometheus-reconciler";
  version = "0.1.0";
  pyproject = false;

  nativeBuildInputs = [ makeWrapper ];
  propagatedBuildInputs = [ hermes-workflow-state ];
  dontBuild = true;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/${python3.sitePackages}" "$out/bin"
    install -m 0644 ${./.}/src/hermes_prometheus_reconciler.py \
      "$out/${python3.sitePackages}/hermes_prometheus_reconciler.py"
    makeWrapper ${python3.interpreter} "$out/bin/hermes-prometheus-reconciler" \
      --add-flags "$out/${python3.sitePackages}/hermes_prometheus_reconciler.py" \
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
    "hermes_prometheus_reconciler"
    "hermes_workflow_state"
  ];

  meta = {
    description = "Deterministic Prometheus incident reconciliation into native Hermes Kanban";
    license = lib.licenses.mit;
    mainProgram = "hermes-prometheus-reconciler";
    platforms = lib.platforms.unix;
  };
}
