"""Run only in a disposable test VM with Docker; no production daemon or credentials."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import yaml

configs = json.loads(Path(os.environ["INVENTORY_TEST_CONFIGS"]).read_text())
spec = importlib.util.spec_from_file_location(
    "activation", Path(__file__).parents[1] / "activate-managed-skills.py"
)
assert spec is not None and spec.loader is not None
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)
root = Path("/var/lib/hermes/.hermes")
root.mkdir(parents=True)
plan = {}
settings = {}
for name, path in configs.items():
    config = yaml.safe_load(Path(path).read_text())
    settings[name] = config
    home = root / "profiles" / name
    home.mkdir(parents=True)
    (home / "local-note").write_text("preserve")
    private_store = next(
        Path(v.split(":")[0])
        for v in config["terminal"]["docker_volumes"]
        if v.split(":")[1] == "/nix/store"
    )
    manifests = list(private_store.glob(f"*-hermes-managed-skills-{name}.json"))
    if manifests:
        data = json.loads(manifests[0].read_text())
        plan[name] = {"target": "/nix/store/" + manifests[0].name, "skills": list(data["skills"])}
activation.activate(root, plan)
activation.activate(root, plan)

for name, config in settings.items():
    args = ["docker", "run", "--rm", "--network=none"]
    for volume in config["terminal"]["docker_volumes"]:
        host, destination, mode = volume.split(":")
        if mode == "rw":
            Path(host).mkdir(parents=True, exist_ok=True)
        args += ["-v", volume]
    home = root / "profiles" / name
    code = "import json,pathlib,sys; home=pathlib.Path(sys.argv[1]); "
    if name in plan:
        code += (
            'm=json.loads((home/"managed-resources.json").read_text()); '
            'r=json.loads(pathlib.Path("/run/hermes-capabilities/nix-managed-resource-changes/references/managed-resources.json").read_text()); '
            'assert m==r and m["profile"]==sys.argv[2]; '
            'assert not pathlib.Path("/run/hermes-credentials/services").exists(); '
            'print(m["profile"], "manifest and helper reference readable in real Docker")'
        )
    else:
        code += (
            'assert not (home/"managed-resources.json").exists(); '
            'assert not pathlib.Path("/run/hermes-capabilities/nix-managed-resource-changes").exists(); '
            'print("empty profile has no inventory or automatic skill")'
        )
    subprocess.run(
        args
        + [
            "hermes-inventory-test:latest",
            "/run/hermes-capabilities/bin/python3",
            "-c",
            code,
            str(home),
            name,
        ],
        check=True,
    )
    if name in plan:
        result = subprocess.run(
            args + ["hermes-inventory-test:latest", "touch", plan[name]["target"]],
            capture_output=True,
        )
        assert result.returncode == 1, result.stderr
        assert b"Read-only file system" in result.stderr, result.stderr
        # The original gateway publication is intentionally absent from the private store.
        external = config["skills"]["external_dirs"][0]
        result = subprocess.run(args + ["hermes-inventory-test:latest", "test", "-e", external])
        assert result.returncode == 1
activation.activate(root, {})
for name in settings:
    home = root / "profiles" / name
    assert not os.path.lexists(home / "managed-resources.json")
    assert (home / "local-note").read_text() == "preserve"
print(
    "PASS: real Docker isolated profiles, readable symlinks, read-only mounts, empty profile, retirement"
)
