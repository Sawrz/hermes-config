"""Discover supported Python CLI helpers; reject unsupported loose executables."""

import ast
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
checks = []
for skill in sorted(root.iterdir()):
    if not skill.is_dir():
        continue
    if not (skill / "SKILL.md").is_file():
        raise SystemExit(f"{skill.name}: missing SKILL.md")
    for path in sorted(skill.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"{path}: unresolved helper symlink")
        if not path.is_file() or "tests" in path.relative_to(skill).parts:
            continue
        data = path.read_bytes()
        executable = data.startswith(b"#!") or bool(path.stat().st_mode & 0o111)
        if path.suffix == ".py":
            tree = ast.parse(data, filename=str(path))
            executable = executable or any(
                isinstance(node, ast.Constant) and node.value == "__main__"
                for node in ast.walk(tree)
            )
            if executable:
                checks.append(
                    {
                        "cwd": f"/run/hermes-capabilities/{skill.name}",
                        "argv": ["python3", str(path.relative_to(skill)), "--help"],
                    }
                )
        elif executable or data.startswith(b"\x7fELF"):
            raise SystemExit(
                f"{path}: unsupported loose executable; use provides.runtimePackages with its complete Nix dependencies"
            )
print(json.dumps(checks))
