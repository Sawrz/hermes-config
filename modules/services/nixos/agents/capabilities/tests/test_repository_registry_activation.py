import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "activate-repository-registry.sh"


class RepositoryRegistryActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "profile"
        self.home.mkdir()
        self.registry = self.home / "job-config/repository-sync/registry.json"
        self.target = "/nix/store/test-hermes-fixture-repository-sync-registry.json"

    def activate(self, target=None):
        return subprocess.run(
            [
                "bash",
                str(SCRIPT),
                str(self.home),
                "fixture",
                self.target if target is None else target,
            ],
            capture_output=True,
            text=True,
        )

    def test_create_update_and_unenroll_only_owned_link(self):
        self.assertEqual(self.activate().returncode, 0)
        self.assertEqual(str(self.registry.readlink()), self.target)
        self.assertEqual(self.activate().returncode, 0)
        other = "/nix/store/other-hermes-fixture-repository-sync-registry.json"
        self.assertEqual(self.activate(other).returncode, 0)
        self.assertEqual(str(self.registry.readlink()), other)
        cache = self.home / "repositories/keep"
        cache.mkdir(parents=True)
        (cache / "local.txt").write_text("keep")
        self.assertEqual(self.activate("").returncode, 0)
        self.assertFalse(self.registry.is_symlink())
        self.assertEqual((cache / "local.txt").read_text(), "keep")

    @unittest.skipUnless(os.geteuid() == 0, "root activation ownership probe")
    def test_root_activation_preserves_profile_owner_access(self):
        os.chown(self.home, 12345, 12345)
        self.assertEqual(self.activate().returncode, 0)
        self.assertEqual(self.registry.parent.stat().st_uid, self.home.stat().st_uid)
        self.assertEqual(self.registry.parent.stat().st_gid, self.home.stat().st_gid)
        self.assertEqual(self.registry.parent.stat().st_mode & 0o777, 0o700)

    def test_unknown_registry_is_not_overwritten_or_removed(self):
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text("private registry")
        self.assertNotEqual(self.activate().returncode, 0)
        self.assertEqual(self.registry.read_text(), "private registry")
        self.assertEqual(self.activate("").returncode, 0)
        self.assertEqual(self.registry.read_text(), "private registry")

    def test_symlink_parent_is_rejected(self):
        outside = self.home.parent / "outside"
        outside.mkdir()
        (self.home / "job-config").symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.activate().returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_unenrollment_preserves_private_symlink_directory(self):
        outside = self.home.parent / "outside"
        outside.mkdir()
        (self.home / "job-config").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.activate("").returncode, 0)
        self.assertTrue((self.home / "job-config").is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_other_profile_link_is_not_adopted(self):
        self.registry.parent.mkdir(parents=True)
        target = "/nix/store/test-hermes-another-repository-sync-registry.json"
        self.registry.symlink_to(target)
        self.assertNotEqual(self.activate().returncode, 0)
        self.assertEqual(self.activate("").returncode, 0)
        self.assertEqual(str(self.registry.readlink()), target)
