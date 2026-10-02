"""Run only declared offline startup probes inside the real Docker boundary."""

import json
import os
from pathlib import Path
import subprocess

# A custom Nix-based image may itself need store paths hidden by the private
# runtime mount. Prove the image's shell still starts as well as our helpers.
subprocess.run(["/bin/sh", "-c", "true"], check=True, timeout=10)
root = Path("/run/hermes-capabilities")
for directory in (root, Path("/nix/store")):
    for path in directory.rglob("*"):
        if path.is_symlink() and not path.exists():
            raise SystemExit(f"Hermes helper readiness failed: broken symlink {path}")
env = dict(os.environ, PATH=f"{root}/bin:/usr/local/bin:/usr/bin:/bin", PYTHONDONTWRITEBYTECODE="1")
for check in json.loads((root / "checks.json").read_text()):
    result = subprocess.run(
        check["argv"],
        cwd=check["cwd"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise SystemExit(f"Hermes helper readiness failed: {check['argv']}: {result.stderr}")
print("Hermes helper startup checks passed")
