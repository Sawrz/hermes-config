"""Exercise actual owned symlink activation in a disposable profile tree."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).parents[1] / "activate-managed-skills.py"


class ActivationTests(unittest.TestCase):
    def test_collisions_and_symlink_homes_fail_without_mutation(self):
        spec = importlib.util.spec_from_file_location("activate", SCRIPT)
        assert spec is not None and spec.loader is not None
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        for kind in ["file", "link", "local-skill", "home-link"]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / "profiles/alpha"
                home.mkdir(parents=True)
                manifest = home / "managed-resources.json"
                if kind == "file":
                    manifest.write_text("local data")
                elif kind == "link":
                    manifest.symlink_to("/local/not-owned")
                elif kind == "local-skill":
                    skill = home / "skills/category/example/SKILL.md"
                    skill.parent.mkdir(parents=True)
                    skill.write_text("local skill")
                else:
                    home.rmdir()
                    home.symlink_to(root, target_is_directory=True)
                target = "/nix/store/" + "a" * 32 + "-hermes-managed-skills-alpha.json"
                with self.assertRaises(ValueError):
                    helper.activate(root, {"alpha": {"target": target, "skills": ["example"]}})
                if kind == "file":
                    self.assertEqual(manifest.read_text(), "local data")
                if kind == "link":
                    self.assertEqual(str(manifest.readlink()), "/local/not-owned")

    def test_add_repeat_remove_preserves_local_data(self):
        spec = importlib.util.spec_from_file_location("activate", SCRIPT)
        assert spec is not None and spec.loader is not None
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "profiles/alpha"
            home.mkdir(parents=True)
            local = home / "notes.txt"
            local.write_text("keep me")
            target = "/nix/store/" + "a" * 32 + "-hermes-managed-skills-alpha.json"
            helper.activate(root, {"alpha": {"target": target, "skills": ["example"]}})
            manifest = home / "managed-resources.json"
            self.assertEqual(str(manifest.readlink()), target)
            before = manifest.lstat().st_mtime_ns
            helper.activate(root, {"alpha": {"target": target, "skills": ["example"]}})
            self.assertEqual(before, manifest.lstat().st_mtime_ns)
            newer = target.replace("a" * 32, "b" * 32)
            helper.activate(root, {"alpha": {"target": newer, "skills": ["example"]}})
            self.assertEqual(str(manifest.readlink()), newer)
            # Dropping one consumer leaves independently selected publication intact.
            helper.activate(root, {"alpha": {"target": newer, "skills": ["example"]}})
            own_skill = home / "skills/widget-knowledge/SKILL.md"
            own_skill.parent.mkdir(parents=True)
            own_skill.write_text("Agent-owned knowledge")
            helper.activate(root, {})
            self.assertEqual(own_skill.read_text(), "Agent-owned knowledge")
            self.assertFalse(manifest.is_symlink())
            self.assertEqual(local.read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
