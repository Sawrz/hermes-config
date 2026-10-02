{
  hermes-workflow-state,
  lib,
  makeWrapper,
  python3,
}:
python3.pkgs.buildPythonApplication {
  pname = "hermes-vikunja-review";
  version = "0.1.0";
  pyproject = false;

  nativeBuildInputs = [ makeWrapper ];
  propagatedBuildInputs = [ hermes-workflow-state ];
  dontBuild = true;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/${python3.sitePackages}" "$out/bin" "$out/share/hermes-vikunja-review"
    install -m 0644 ${./.}/src/hermes_vikunja_review.py \
      "$out/${python3.sitePackages}/hermes_vikunja_review.py"
    install -m 0644 ${./review-preferences.json} \
      "$out/share/hermes-vikunja-review/review-preferences.json"
    install -m 0644 ${./review-config.example.json} \
      "$out/share/hermes-vikunja-review/review-config.example.json"
    makeWrapper ${python3.interpreter} "$out/bin/hermes-vikunja-review" \
      --add-flags "$out/${python3.sitePackages}/hermes_vikunja_review.py" \
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
    "hermes_vikunja_review"
    "hermes_workflow_state"
  ];

  meta = {
    description = "Deterministic read-only Vikunja daily and weekly review collector";
    license = lib.licenses.mit;
    mainProgram = "hermes-vikunja-review";
    platforms = lib.platforms.unix;
  };
}
