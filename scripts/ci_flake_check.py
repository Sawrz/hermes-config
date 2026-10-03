#!/usr/bin/env python3
"""Run native flake checks in fresh evaluators, without retaining every host graph.

Snapshot the source once. Native `nix flake check` still validates all other
outputs, each current-system check (including its build), and every NixOS host.
Only the output view changes per process; no validation is reimplemented here.
Like plain flake check, this does not build host toplevels or foreign systems.
"""

import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time
from urllib.parse import quote


def nix_string(value):
    return json.dumps(value, ensure_ascii=False).replace("${", "\\${")


def nix_json(*args):
    return json.loads(subprocess.check_output(["nix", *args], text=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flake", default=".")
    args = parser.parse_args()
    metadata = nix_json(
        "flake", "metadata", "--json", "--no-write-lock-file", "--no-update-lock-file", args.flake
    )
    # A path alone is unlocked. Its NAR hash pins the exact store snapshot for
    # every partition, even if the worktree changes during this run.
    reference = (
        "path:" + metadata["path"] + "?narHash=" + quote(metadata["locked"]["narHash"], safe="")
    )
    system = subprocess.check_output(
        ["nix", "eval", "--raw", "--impure", "--expr", "builtins.currentSystem"], text=True
    ).strip()
    source = "builtins.getFlake " + nix_string(reference)
    system_attr = nix_string(system)
    inventory = nix_json(
        "eval",
        "--json",
        "--expr",
        f"""
      let source = {source}; in {{
        checks = builtins.attrNames (source.checks.{system_attr} or {{}});
        hosts = builtins.attrNames (source.nixosConfigurations or {{}});
      }}
    """,
    )
    partitions = [
        (
            "other outputs",
            f"""
      builtins.removeAttrs source.outputs [ "checks" "nixosConfigurations" ] // {{
        checks = builtins.removeAttrs (source.checks or {{}}) [ {system_attr} ];
      }}
    """,
        )
    ]
    for name in inventory["checks"]:
        attr = nix_string(name)
        partitions.append(
            (
                f"checks.{system}.{name}",
                f"{{ checks.{system_attr}.{attr} = source.checks.{system_attr}.{attr}; }}",
            )
        )
    for name in inventory["hosts"]:
        attr = nix_string(name)
        partitions.append(
            (
                f"nixosConfigurations.{name}",
                f"{{ nixosConfigurations.{attr} = source.nixosConfigurations.{attr}; }}",
            )
        )

    print(f"Source: {reference}", flush=True)
    print(
        f"Inventory: {len(inventory['checks'])} checks, {len(inventory['hosts'])} hosts, other outputs",
        flush=True,
    )
    failures = []
    with tempfile.TemporaryDirectory(prefix="ci-flake-check-") as directory:
        for label, expression in partitions:
            # No source edits, extra inputs, dependency overrides or lock writes.
            (Path(directory) / "flake.nix").write_text(
                f"{{ outputs = {{ self }}: let source = {source}; in {expression}; }}\n"
            )
            print(f"BEGIN {label}", flush=True)
            started = time.monotonic()
            command = [
                "nix",
                "flake",
                "check",
                directory,
                "--print-build-logs",
                "--no-write-lock-file",
                "--no-update-lock-file",
            ]
            peak_fds = 0
            nofile = (None, None)
            with subprocess.Popen(command) as process:
                while True:
                    try:
                        peak_fds = max(peak_fds, len(os.listdir(f"/proc/{process.pid}/fd")))
                        nofile = resource.prlimit(process.pid, resource.RLIMIT_NOFILE)
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        # A short-lived child may exit between reads. Do not
                        # replace its actual wait status with a sampling error.
                        pass
                    pid, status, usage = os.wait4(process.pid, os.WNOHANG)
                    if pid:
                        break
                    time.sleep(0.2)
                process.returncode = os.waitstatus_to_exitcode(status)
            outcome = "FAIL" if process.returncode else "PASS"
            print(
                f"{outcome} {label} elapsed={time.monotonic() - started:.1f}s "
                f"max_rss_kib={usage.ru_maxrss} max_open_fds={peak_fds} "
                f"nofile_soft={nofile[0]} nofile_hard={nofile[1]}",
                flush=True,
            )
            if process.returncode:
                # The immutable partitions are independent. Gather every failure
                # in this run rather than hiding later errors behind the first.
                failures.append(f"{label} (exit {process.returncode})")
    if failures:
        raise SystemExit("Failed partitions:\n" + "\n".join(failures))
    print(f"Complete: {len(partitions)}/{len(partitions)} partitions", flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        # Keep actual enforcement/peak/OOM evidence even when a child fails.
        for filename in ("memory.max", "memory.swap.max", "memory.peak", "memory.events"):
            path = Path("/sys/fs/cgroup") / filename
            if path.is_file():
                print(f"cgroup {filename}: {path.read_text().strip()}", flush=True)
