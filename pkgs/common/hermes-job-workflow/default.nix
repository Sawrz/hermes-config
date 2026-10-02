{
  lib,
  makeWrapper,
  python3,
  hermes-workflow-state,
}:

python3.pkgs.buildPythonApplication {
  pname = "hermes-job-workflow";
  version = "0.1.0";
  pyproject = false;
  src = ./.;

  dontBuild = true;
  nativeBuildInputs = [ makeWrapper ];
  propagatedBuildInputs = [ hermes-workflow-state ];
  HERMES_WORKFLOW_STATE_PYTHONPATH = "${hermes-workflow-state}/${python3.sitePackages}";

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/${python3.sitePackages}" "$out/bin" "$out/share/hermes-job-workflow"
    install -m 0644 src/hermes_job_workflow.py "$out/${python3.sitePackages}/hermes_job_workflow.py"
    makeWrapper ${python3.interpreter} "$out/bin/hermes-job-workflow" \
      --add-flags "$out/${python3.sitePackages}/hermes_job_workflow.py" \
      --prefix PYTHONPATH : "${hermes-workflow-state}/${python3.sitePackages}:$out/${python3.sitePackages}"
    install -m 0644 contracts/*.json "$out/share/hermes-job-workflow/"
    runHook postInstall
  '';

  checkPhase = ''
    runHook preCheck
    PYTHONPATH="$PWD/src:${hermes-workflow-state}/${python3.sitePackages}" \
      ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  pythonImportsCheck = [ "hermes_job_workflow" ];

  meta = {
    description = "Deterministic vacancy collection, lifecycle, and active-research gate";
    license = lib.licenses.mit;
    mainProgram = "hermes-job-workflow";
    platforms = lib.platforms.unix;
  };
}
