"""Nix-generated publication + exact pinned Hermes native discovery, without mocks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml


def native_probe():
    sys.path.insert(0, os.environ["HERMES_UPSTREAM_SOURCE"])
    from tools.skills_tool import skills_list, skill_view

    listing = json.loads(skills_list())
    assert listing.get("success"), listing
    view = json.loads(skill_view("nix-managed-resource-changes", preprocess=False))
    assert view.get("success"), view
    reference = json.loads(
        skill_view("nix-managed-resource-changes", "references/managed-resources.json")
    )
    assert reference.get("success"), reference
    content = json.loads(reference["content"])
    assert content["profile"] == os.environ["EXPECTED_PROFILE"], content
    print(json.dumps({"profile": content["profile"], "native_reference": True}))


def main():
    expected = {
        "inventory-alpha": {
            "inline-example",
            "external-example",
            "profile-scoped-service-credentials",
            "evidence-driven-change-control",
            "nix-managed-resource-changes",
        },
        "inventory-beta": {"evidence-driven-change-control", "nix-managed-resource-changes"},
        "inventory-empty": set(),
        "inventory-cron-only": {"nix-managed-resource-changes"},
        "inventory-unselected": set(),
    }
    configs = json.loads(Path(os.environ["INVENTORY_TEST_CONFIGS"]).read_text())
    for name, config_file in configs.items():
        config = yaml.safe_load(Path(config_file).read_text())
        dirs = config.get("skills", {}).get("external_dirs", [])
        if not expected[name]:
            assert not dirs, dirs
            continue
        assert len(dirs) == 1, dirs
        root = Path(dirs[0])
        names = {p.parent.name for p in root.glob("*/*/SKILL.md")}
        assert names == expected[name], (name, names)
        reference = (
            root / "repo-managed/nix-managed-resource-changes/references/managed-resources.json"
        )
        manifest = json.loads(reference.read_text())
        assert set(manifest["skills"]) == expected[name], manifest
        assert manifest["profile"] == name
        managed = manifest["skills"]["nix-managed-resource-changes"]
        assert managed["source"]["repository"] == "nixos/hermes-config"
        assert managed["source"]["route"]["repository"] == "nixos/hermes-config"
        assert managed["provider"] == "agentSkill:nix-managed-resource-changes"
        assert (
            managed["source"]["path"]
            == "modules/services/nixos/agents/skills/nix-managed-resource-changes"
        )
        assert "modules/services/nixos/agents/capabilities/foundation-skills.nix" in [
            d["path"] for d in managed["declarations"]
        ]
        if name in ("inventory-alpha", "inventory-cron-only"):
            assert set(manifest["native_jobs"]) == {"nix-config-pull"}
            job = manifest["native_jobs"]["nix-config-pull"]
            assert job["provider"] == "serviceIntegration:repository-sync"
            assert [d["path"] for d in job["declarations"]] == [
                "modules/services/nixos/agents/capabilities/repository-sync.nix"
            ]
        else:
            assert manifest["native_jobs"] == {}
        if name == "inventory-alpha":
            assert manifest["skills"]["inline-example"]["source"]["kind"] == "inline"
            assert manifest["skills"]["inline-example"]["declarations"]
        volumes = dict(
            (v.split(":")[1], v.split(":")[0]) for v in config["terminal"]["docker_volumes"]
        )
        helpers = Path(volumes["/run/hermes-capabilities"])
        private_store = Path(volumes["/nix/store"])
        if name == "inventory-alpha":
            source = manifest["skills"]["external-example"]["source"]
            assert source["kind"] == "external" and source["path"] is None
            assert not (private_store / Path(source["packaged_input"]).name).exists(), (
                "metadata must not expand the terminal store closure"
            )
        assert (
            helpers / "nix-managed-resource-changes/references/managed-resources.json"
        ).read_bytes() == reference.read_bytes()
        stored = list(private_store.glob(f"*-hermes-managed-skills-{name}.json"))
        assert len(stored) == 1
        assert stored[0].read_bytes() == reference.read_bytes()
        assert stored[0].stat().st_mode & 0o222 == 0
        assert not any(private_store.glob("*-hermes-managed-skills-inventory-empty.json"))
        for other in expected.keys() - {name}:
            assert not any(private_store.glob(f"*-hermes-managed-skills-{other}.json"))
        assert "/run/hermes-credentials/services" not in volumes
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "skills").mkdir()
            (home / "config.yaml").write_text(yaml.safe_dump({"skills": {"external_dirs": dirs}}))
            env = os.environ | {"HERMES_HOME": str(home), "EXPECTED_PROFILE": name}
            subprocess.run([sys.executable, __file__, "--native"], env=env, check=True)
        print(
            f"{name}: exact closure, provenance, read-only private store and linked reference passed"
        )


if __name__ == "__main__":
    native_probe() if "--native" in sys.argv else main()
