{
  lib,
  makeWrapper,
  python3,
  git,
  openssh,
}:
python3.pkgs.buildPythonApplication {
  pname = "hermes-repository-sync";
  version = "0.1.0";
  pyproject = false;

  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ git ];
  dontBuild = true;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/${python3.sitePackages}" "$out/bin" "$out/share/hermes-repository-sync"
    install -m 0644 ${./.}/src/hermes_repository_sync.py \
      "$out/${python3.sitePackages}/hermes_repository_sync.py"
    install -m 0644 ${./schedule-templates.json} \
      "$out/share/hermes-repository-sync/schedule-templates.json"
    install -m 0644 ${./registry.example.json} \
      "$out/share/hermes-repository-sync/registry.example.json"
    makeWrapper ${python3.interpreter} "$out/bin/hermes-repository-sync" \
      --add-flags "$out/${python3.sitePackages}/hermes_repository_sync.py" \
      --prefix PATH : ${
        lib.makeBinPath [
          git
          openssh
        ]
      }

    runHook postInstall
  '';

  checkPhase = ''
    runHook preCheck
    PYTHONPATH=${./.}/src ${python3.interpreter} -m unittest discover -s ${./.}/tests -v
    runHook postCheck
  '';

  pythonImportsCheck = [ "hermes_repository_sync" ];

  meta = {
    description = "Fail-closed deterministic synchronization for allowlisted Git checkouts";
    license = lib.licenses.mit;
    mainProgram = "hermes-repository-sync";
    platforms = lib.platforms.linux;
  };
}
