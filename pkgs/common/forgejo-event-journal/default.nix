{
  hermes-workflow-state,
  lib,
  makeWrapper,
  python3,
}:

python3.pkgs.buildPythonApplication {
  pname = "forgejo-event-journal";
  version = "0.1.0";
  pyproject = false;
  src = ./.;

  dontBuild = true;
  propagatedBuildInputs = [ hermes-workflow-state ];

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/${python3.sitePackages}" "$out/bin"
    install -m 0644 src/forgejo_event_journal.py "$out/${python3.sitePackages}/forgejo_event_journal.py"
    makeWrapper ${python3.interpreter} "$out/bin/forgejo-event-journal" \
      --add-flags "$out/${python3.sitePackages}/forgejo_event_journal.py" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}:${hermes-workflow-state}/${python3.sitePackages}"

    ${python3.interpreter} -m compileall -q --invalidation-mode checked-hash \
      "$out/${python3.sitePackages}"

    runHook postInstall
  '';

  nativeBuildInputs = [ makeWrapper ];

  checkPhase = ''
    runHook preCheck
    export HERMES_REPOSITORY_INSTANCE=${../hermes-workflow-state/tests/fixtures/instance.json}
    PYTHONPATH="$PWD/src:${hermes-workflow-state}/${python3.sitePackages}" \
      ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  pythonImportsCheck = [ "forgejo_event_journal" ];

  meta = {
    description = "Crash-safe polling-only Forgejo event journal";
    license = lib.licenses.mit;
    mainProgram = "forgejo-event-journal";
    platforms = lib.platforms.linux;
  };
}
