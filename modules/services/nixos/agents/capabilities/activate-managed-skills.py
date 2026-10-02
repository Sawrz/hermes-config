"""Own only managed-resources.json symlinks to this module's immutable manifests."""

import argparse
import json
import os
from pathlib import Path
import re


def activate(root, profiles):
    if root.is_symlink() or (root / "profiles").is_symlink():
        raise ValueError("Refusing symlinked profile root")
    for name in profiles:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise ValueError("Invalid profile name")
    homes = {"default": root}
    if (root / "profiles").is_dir():
        homes.update({p.name: p for p in (root / "profiles").iterdir() if p.is_dir()})
    for name in profiles:
        homes[name] = root if name == "default" else root / "profiles" / name
    changes = []
    for name, home in homes.items():
        if home.is_symlink():
            raise ValueError(f"Refusing symlinked profile home: {home}")
        path = home / "managed-resources.json"
        row = profiles.get(name)
        target = row["target"] if row else None
        pattern = r"/nix/store/[a-z0-9]{32}-hermes-managed-skills-" + re.escape(name) + r"\.json"
        owned = path.is_symlink() and re.fullmatch(pattern, str(path.readlink()))
        if target:
            if not re.fullmatch(pattern, target):
                raise ValueError("Invalid generated manifest target")
            if os.path.lexists(path) and not owned:
                raise ValueError(f"Unowned manifest collision: {path}")
            for skill in row["skills"]:
                for candidate in [
                    home / "skills" / skill / "SKILL.md",
                    *(home / "skills").glob(f"*/{skill}/SKILL.md"),
                ]:
                    if os.path.lexists(candidate):
                        raise ValueError(f"Local skill would shadow managed skill: {candidate}")
            if owned and str(path.readlink()) == target:
                continue
        if target or owned:
            changes.append((home, path, target))
    # Validate the complete plan before changing any profile.
    for home, path, target in changes:
        if target:
            temporary = home / f".managed-resources.json.{os.getpid()}"
            temporary.symlink_to(target)
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    activate(args.home, json.loads(args.spec.read_text()))


if __name__ == "__main__":
    main()
