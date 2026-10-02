{
  lib,
  makeWrapper,
  python3,
  git,
  nix,
  skopeo,
  openssl,
  hermes-workflow-state,
  forgejo-kanban-workflow,
  hermes-repository-sync,
}:
let
  # These helpers are applications, not Python modules: makePythonPath filters
  # them out. Include their declared transitive import closure explicitly.
  importPath = lib.makeSearchPath python3.sitePackages (
    lib.closePropagation [
      hermes-workflow-state
      forgejo-kanban-workflow
      hermes-repository-sync
    ]
  );
in
python3.pkgs.buildPythonApplication {
  pname = "hermes-nix-maintenance";
  version = "0.1.0";
  pyproject = false;

  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [
    git
    nix
    skopeo
    openssl
  ];
  propagatedBuildInputs = [
    hermes-workflow-state
    forgejo-kanban-workflow
    hermes-repository-sync
  ];
  dontBuild = true;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/${python3.sitePackages}" "$out/bin"
    install -m 0644 ${./.}/src/hermes_nix_maintenance.py \
      "$out/${python3.sitePackages}/hermes_nix_maintenance.py"
    install -m 0644 ${./.}/src/nix_config_docs_inventory.py \
      "$out/${python3.sitePackages}/nix_config_docs_inventory.py"
    install -m 0644 ${./.}/src/nix_config_wiki_sync.py \
      "$out/${python3.sitePackages}/nix_config_wiki_sync.py"

    for entry in \
      hermes-nix-maintenance:hermes_nix_maintenance.py \
      nix-config-docs-inventory:nix_config_docs_inventory.py \
      nix-config-wiki-sync:nix_config_wiki_sync.py
    do
      name="''${entry%%:*}"
      source="''${entry#*:}"
      makeWrapper ${python3.interpreter} "$out/bin/$name" \
        --add-flags "$out/${python3.sitePackages}/$source" \
        --prefix PYTHONPATH : "$out/${python3.sitePackages}:${importPath}" \
        --prefix PATH : ${
          lib.makeBinPath [
            git
            nix
            skopeo
          ]
        }
    done

    # Compile before import checks with source hashes, not build-time mtimes.
    # dontUnpack/dontBuild otherwise leave Python to create timestamp caches
    # during pythonImportsCheck, after the regular fixup hooks have run.
    ${python3.interpreter} -m compileall -q --invalidation-mode checked-hash \
      "$out/${python3.sitePackages}"

    runHook postInstall
  '';

  checkPhase = ''
    runHook preCheck
    export HERMES_REPOSITORY_INSTANCE=${../hermes-workflow-state/tests/fixtures/instance.json}
    export PYTHONPATH=${./.}/src:${importPath}
    export HERMES_CONFIG_ROOT=${../../..}
    ${python3.interpreter} -m unittest discover -s ${./.}/tests -v
    runHook postCheck
  '';

  pythonImportsCheck = [
    "hermes_nix_maintenance"
    "nix_config_docs_inventory"
    "nix_config_wiki_sync"
  ];

  meta = {
    description = "Deterministic Nix repository maintenance mechanics";
    license = lib.licenses.mit;
    mainProgram = "hermes-nix-maintenance";
    platforms = lib.platforms.linux;
  };
}
