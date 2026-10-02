{
  lib,
  makeWrapper,
  python3,
}:

let
  runtimePython = python3.withPackages (ps: [ ps.croniter ]);
in
python3.pkgs.buildPythonApplication {
  pname = "hermes-native-cron-reconciler";
  version = "0.1.0";
  pyproject = false;
  src = ./.;

  dontBuild = true;
  nativeBuildInputs = [ makeWrapper ];
  propagatedBuildInputs = [ python3.pkgs.croniter ];

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/${python3.sitePackages}" "$out/bin"
    install -m 0644 src/hermes_native_cron_reconciler.py "$out/${python3.sitePackages}/hermes_native_cron_reconciler.py"
    makeWrapper ${runtimePython}/bin/python3 "$out/bin/hermes-native-cron-reconciler" \
      --add-flags "$out/${python3.sitePackages}/hermes_native_cron_reconciler.py"
    runHook postInstall
  '';

  checkPhase = ''
    runHook preCheck
    ${python3.interpreter} -m unittest discover -s tests -v
    # Build-time PYTHONPATH must not hide missing launcher dependencies.
    env -i "$out/bin/hermes-native-cron-reconciler" --help >/dev/null
    runHook postCheck
  '';

  pythonImportsCheck = [ "hermes_native_cron_reconciler" ];

  meta = {
    description = "Profile-policy reconciliation through supported Hermes cron CLI";
    license = lib.licenses.mit;
    mainProgram = "hermes-native-cron-reconciler";
    platforms = lib.platforms.unix;
  };
}
