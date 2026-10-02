"""Commit coverage, using real Git repositories and explicit durable ACK."""

from dataclasses import replace
from hermes_repository_instance import instance, instance_scope
import subprocess
import tempfile
import unittest
from pathlib import Path

import hermes_nix_maintenance as m


class DocsAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "master")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Isolated test")
        self.first = self.commit("module.nix", "first")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self, name, text):
        (self.repo / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-m", text)
        return self.git("rev-parse", "HEAD")

    def test_first_audit_and_failure_do_not_mark_code_checked(self):
        self.assertTrue(
            hasattr(m, "DocsAudit"), "Docs gate needs commit-based audit, not pre-ACK hash"
        )
        audit = m.DocsAudit(self.root / "state", self.repo)
        scope = audit.scope()
        self.assertIsNone(scope["base"])
        self.assertEqual(scope["target"], self.first)
        self.assertIn("module.nix", scope["paths"])
        self.assertIsNone(audit.successful_commit())
        self.assertEqual(audit.scope(), scope)  # failed audit retries the range

    def test_code_changes_and_missed_runs_span_successful_commit(self):
        audit = m.DocsAudit(self.root / "state", self.repo)
        audit.store.publish({"successful.json": {"commit": self.first}})
        self.assertEqual(audit.scope()["paths"], [])
        self.commit("module.nix", "missed first run")
        target = self.commit("service.py", "missed second run")
        scope = audit.scope()
        self.assertEqual(scope["base"], self.first)
        self.assertEqual(scope["target"], target)
        self.assertEqual(set(scope["paths"]), {"module.nix", "service.py"})
        self.git("checkout", "-b", "feature")
        feature = self.commit("feature.txt", "not main")
        self.assertNotEqual(feature, target)
        self.assertEqual(audit.scope()["target"], target)

    def test_nonancestor_or_missing_base_is_not_success(self):
        audit = m.DocsAudit(self.root / "state", self.repo)
        self.git("checkout", "--orphan", "unrelated")
        unrelated = self.commit("other.txt", "unrelated")
        for base in [unrelated, "f" * 40]:
            audit.store.publish({"successful.json": {"commit": base}})
            with self.assertRaises(m.WorkflowError):
                audit.scope()
            self.assertEqual(audit.successful_commit(), base)

    def test_second_repository_audits_explicit_main_branch_only(self):
        self.git("branch", "-m", "trunk")
        cfg = replace(
            instance(),
            identity="widget-audit",
            repository="acme/widgets",
            default_branch="trunk",
            namespace="widgets",
            implementer="builder",
            reviewer="auditor",
        )
        with instance_scope(cfg):
            audit = m.DocsAudit(self.root / "widget-state", self.repo)
            self.assertEqual(audit.scope()["target"], self.first)
            self.git("checkout", "-b", "feature")
            self.commit("feature.nix", "exclude from audit")
            self.assertEqual(audit.scope()["target"], self.first)
            self.assertNotIn("feature.nix", audit.scope()["paths"])

    def test_shallow_history_is_rejected(self):
        shallow = self.root / "shallow"
        subprocess.run(
            ["git", "clone", "--depth", "1", self.repo.as_uri(), str(shallow)],
            check=True,
            capture_output=True,
        )
        with self.assertRaises(m.WorkflowError):
            m.DocsAudit(self.root / "state", shallow).scope()


if __name__ == "__main__":
    unittest.main()
