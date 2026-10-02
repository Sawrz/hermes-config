{
  forgejo-event-journal,
  hermes-workflow-state,
  lib,
  makeWrapper,
  python3,
}:

python3.pkgs.buildPythonApplication {
  pname = "forgejo-kanban-workflow";
  version = "0.1.0";
  pyproject = false;
  src = ./.;

  dontBuild = true;
  propagatedBuildInputs = [
    forgejo-event-journal
    hermes-workflow-state
  ];

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/${python3.sitePackages}" "$out/bin"
    install -m 0644 src/forgejo_kanban_workflow.py "$out/${python3.sitePackages}/forgejo_kanban_workflow.py"
    makeWrapper ${python3.interpreter} "$out/bin/forgejo-kanban-workflow" \
      --add-flags "$out/${python3.sitePackages}/forgejo_kanban_workflow.py" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}:${forgejo-event-journal}/${python3.sitePackages}:${hermes-workflow-state}/${python3.sitePackages}"
    # Import checks must not create timestamp-based bytecode after fixup.
    ${python3.interpreter} -m compileall -q --invalidation-mode checked-hash \
      "$out/${python3.sitePackages}"
    runHook postInstall
  '';

  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ forgejo-event-journal ];

  checkPhase = ''
    runHook preCheck
    export HERMES_REPOSITORY_INSTANCE=${../hermes-workflow-state/tests/fixtures/instance.json}
    PYTHONPATH="$PWD/src:${forgejo-event-journal}/${python3.sitePackages}:${hermes-workflow-state}/${python3.sitePackages}" \
      ${python3.interpreter} -m unittest discover -s tests -v
    PYTHONPATH="$PWD/src:${forgejo-event-journal}/${python3.sitePackages}:${hermes-workflow-state}/${python3.sitePackages}" \
      ${python3.interpreter} -c 'import forgejo_event_journal as journal, forgejo_kanban_workflow as workflow; assert workflow.JOURNAL_SCHEMA_VERSION == journal.SCHEMA_VERSION'
    runHook postCheck
  '';

  pythonImportsCheck = [ "forgejo_kanban_workflow" ];

  meta = {
    description = "Deterministic Forgejo-to-native-Hermes-Kanban reconciler";
    license = lib.licenses.mit;
    mainProgram = "forgejo-kanban-workflow";
    platforms = lib.platforms.linux;
  };
}
