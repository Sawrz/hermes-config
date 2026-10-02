#!/usr/bin/env python3
"""Configure private source reads inside an ephemeral Actions job."""

import os
import json
from pathlib import Path
import shlex
import tempfile


def configure(environ, known_hosts, *, require_github=True):
    keys = {}
    for host, variable in (
        ("forgejo", "NIX_FORGEJO_SSH_KEY"),
        ("github", "NIX_GITHUB_SSH_KEY"),
    ):
        if host == "github" and not require_github:
            continue
        value = environ.get(variable, "").strip()
        if not value:
            raise ValueError(f"Required private source credential {variable} is missing")
        keys[host] = value + "\n"
    directory = Path(tempfile.mkdtemp(prefix="nix-source-access-", dir=environ["RUNNER_TEMP"]))
    directory.chmod(0o700)
    for host, key in keys.items():
        path = directory / host
        path.touch(mode=0o600)
        path.write_text(key)
    trust = directory / "known_hosts"
    trust.write_bytes(Path(known_hosts).read_bytes())
    config = directory / "config"
    github_config = (
        f'''Host github.com
  Hostname ssh.github.com
  Port 443
  HostKeyAlias github.com
  IdentityFile "{directory / "github"}"
'''
        if require_github
        else ""
    )
    config.write_text(
        f'''Host ssh.git.wrzalek.com
  IdentityFile "{directory / "forgejo"}"
{github_config}Host *
  User git
  IdentityAgent none
  IdentitiesOnly yes
  BatchMode yes
  StrictHostKeyChecking yes
  UserKnownHostsFile "{trust}"
  GlobalKnownHostsFile /dev/null
  ConnectTimeout 10
  ConnectionAttempts 1
'''
    )
    command = "ssh -F " + shlex.quote(str(config))
    with Path(environ["GITHUB_ENV"]).open("a") as output:
        output.write(f"GIT_SSH_COMMAND={command}\n")
    return directory


if __name__ == "__main__":
    try:
        recovery = Path(__file__).resolve().parents[1] / "recovery"
        policy = json.loads((recovery / "sources.json").read_text())
        lock = json.loads((recovery.parent / "flake.lock").read_text())
        inputs = {node.get("locked", {}).get("url") for node in lock["nodes"].values()}
        require_github = any(
            source["recovery"].startswith("ssh://git@github.com/")
            for url, source in policy["sources"].items()
            if url in inputs
        )
        configure(os.environ, recovery / "known_hosts", require_github=require_github)
    except (KeyError, ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    print("Private source SSH access configured with strict repository-managed host trust")
