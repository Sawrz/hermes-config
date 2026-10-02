{
  lib,
  makeWrapper,
  python3,
  hermes-workflow-state,
}:

python3.pkgs.buildPythonApplication {
  pname = "hermes-digest-workflow";
  version = "0.1.0";
  pyproject = false;
  src = ./.;

  dontBuild = true;
  nativeBuildInputs = [ makeWrapper ];
  propagatedBuildInputs = [ hermes-workflow-state ];
  HERMES_WORKFLOW_STATE_PYTHONPATH = "${hermes-workflow-state}/${python3.sitePackages}";

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/${python3.sitePackages}" "$out/bin" "$out/share/hermes-digest-workflow"
    install -m 0644 src/hermes_digest_workflow.py "$out/${python3.sitePackages}/hermes_digest_workflow.py"
    makeWrapper ${python3.interpreter} "$out/bin/hermes-digest-workflow" \
      --add-flags "$out/${python3.sitePackages}/hermes_digest_workflow.py" \
      --prefix PYTHONPATH : "${hermes-workflow-state}/${python3.sitePackages}:$out/${python3.sitePackages}"
    install -m 0644 contracts/*.json "$out/share/hermes-digest-workflow/"
    runHook postInstall
  '';

  checkPhase = ''
    runHook preCheck
    PYTHONPATH="$PWD/src:${hermes-workflow-state}/${python3.sitePackages}" \
      ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  pythonImportsCheck = [ "hermes_digest_workflow" ];

  meta = {
    description = "Deterministic Hermes digest claim and render-state protocol";
    license = lib.licenses.mit;
    mainProgram = "hermes-digest-workflow";
    platforms = lib.platforms.unix;
  };
}
