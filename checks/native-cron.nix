{ self, pkgs }:
let
  inputs = self.inputs;
  hermesNativeCronCliContractCheck = pkgs.runCommand "hermes-native-cron-cli-contract" { } ''
    set -euo pipefail
    parser=${inputs.hermes-agent}/hermes_cli/subcommands/cron.py
    reconciler=${
      (self + "/pkgs/common/hermes-native-cron-reconciler/src/hermes_native_cron_reconciler.py")
    }
    ${pkgs.gnugrep}/bin/grep -F 'cron_subparsers.add_parser("resume"' "$parser"
    ${pkgs.gnugrep}/bin/grep -F 'cron_subparsers.add_parser("pause"' "$parser"
    ${pkgs.gnugrep}/bin/grep -F '"resume",' "$reconciler"
    if ${pkgs.gnugrep}/bin/grep -F '"enable",' "$reconciler"; then
      echo "native cron reconciler uses unsupported pinned-Hermes enable command" >&2
      exit 1
    fi
    touch $out
  '';
in
{
  hermes-native-cron-cli-contract = hermesNativeCronCliContractCheck;
}
