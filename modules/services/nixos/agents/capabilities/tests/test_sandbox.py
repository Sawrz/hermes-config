"""Reproduce the host/terminal boundary in disposable, credential-free Docker containers."""

import json
from pathlib import Path
import subprocess
import sys

manifest = json.loads(Path(sys.argv[1]).read_text())


def run(*args, succeeds=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if succeeds:
        assert result.returncode == 0, result.stderr
    else:
        # Docker failures (125/126/127) must not masquerade as an absent file.
        assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    return result


# Readable on the gateway host does not imply accessible in Docker.
source = Path(manifest["gatewaySkill"])
assert (source / "SKILL.md").is_file()
assert (source / "scripts/mealie_api.py").is_file()
run(
    "run",
    "--rm",
    "--network=none",
    manifest["image"],
    "test",
    "-f",
    str(source / "scripts/mealie_api.py"),
    succeeds=False,
)
for name, publication in manifest["profiles"].items():
    args = ["run", "--rm", "--network=none", "--tmpfs", "/workspace"]
    for volume in publication["volumes"]:
        args += ["-v", volume]
    run(*args, "--read-only", manifest["image"], *publication["readiness"])
    run(
        *args,
        manifest["image"],
        "test",
        "-f",
        "/run/hermes-capabilities/mealie-api/SKILL.md",
        succeeds=name == "selected",
    )
    run(
        *args,
        manifest["image"],
        "test",
        "-f",
        "/run/hermes-capabilities/vikunja-task-reviews/SKILL.md",
        succeeds=name in {"selected", "independent"},
    )
    # Prove the mount itself is read-only, independently of the container root.
    run(*args, manifest["image"], "touch", "/run/hermes-capabilities/forbidden", succeeds=False)
    run(
        *args,
        manifest["image"],
        "sh",
        "-c",
        'test -z "$(ls -A /workspace)" && touch /workspace/request.json',
    )
    print(
        f"PASS {name}: actual helper startups/imports, isolation, read-only artifacts, fresh writable workspace"
    )
