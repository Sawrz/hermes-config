"""Exercise the pinned Hermes CLI/API in disposable profile homes, never a live store."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_reconciler import reconciler as r


@unittest.skipUnless(os.environ.get("HERMES_TEST_CLI"), "requires pinned Hermes CLI")
class NativePolicyTests(unittest.TestCase):
    def test_bootstrap_mixed_reset_migration_retirement_and_profile_isolation(self):
        sys.path.insert(0, os.environ["HERMES_UPSTREAM_SOURCE"])
        from cron import jobs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, sibling = root / "profile", root / "sibling"
            home.mkdir()
            sibling.mkdir()
            with jobs.use_cron_store(sibling):
                other = jobs.create_job(
                    name="first", prompt="untouched sibling", schedule="every 2h"
                )
                other = jobs.list_jobs(include_disabled=True)[0]
            scripts = home / "scripts/profile/.nix-managed"
            scripts.mkdir(parents=True)
            desired = []
            for name in ("first", "second", "third"):
                script = scripts / (name + ".sh")
                script.write_text("#!/bin/sh\nexit 0\n")
                script.chmod(0o700)
                desired.append(
                    {
                        "name": name,
                        "script": str(script),
                        "skills": [],
                        "no_agent": True,
                        "deliver_prefix": "local",
                    }
                )
            contract = home / "native-cron-contract.json"
            policy = home / "native-cron-policy.json"
            status = home / "native-cron-status.json"
            state = home / "state"
            state.mkdir()
            sentinel = state / "processing.json"
            sentinel.write_text('{"cursor":"preserve"}')

            def declare(selected):
                contract.write_text(
                    json.dumps(
                        {"schema_version": 1, "managed_script_dir": str(scripts), "jobs": selected}
                    )
                )

            def choose(value):
                with r._policy_lock(policy):
                    temp = home / "agent-policy.tmp"
                    temp.write_text(json.dumps(value))
                    temp.chmod(0o600)
                    temp.replace(policy)

            def apply(**kwargs):
                return r.reconcile(
                    contract, policy, os.environ["HERMES_TEST_CLI"], native_contract=True, **kwargs
                )

            def rows():
                return {row["name"]: row for row in jobs.list_jobs(include_disabled=True)}

            declare(desired)
            # Deliberately bind the ambient native API to a DIFFERENT profile.
            # Reconciliation must scope both its API and CLI to policy.parent.
            with patch.dict(os.environ, {"HERMES_HOME": str(sibling)}):
                self.assertEqual(apply(bootstrap=True, status_path=status), {})
            self.assertEqual(json.loads(status.read_text())["state"], "needs-configuration")
            self.assertEqual(policy.stat().st_mode & 0o777, 0o600)
            with jobs.use_cron_store(home):
                manual = jobs.create_job(name="manual", prompt="Keep me", schedule="every 2h")
                manual = rows()["manual"]
                value = {
                    "schema_version": 2,
                    "jobs": {
                        "first": {"status": "enabled", "schedule": "every 1h", "deliver": "local"},
                        "second": {"status": "disabled"},
                        "third": {"status": "unconfigured"},
                    },
                }
                choose(value)
                apply(status_path=status)
                self.assertEqual(set(rows()), {"first", "manual"})
                first = rows()["first"]
                jobs.update_job(
                    first["id"],
                    {"last_status": "success", "last_run_at": "2026-01-01T00:00:00+00:00"},
                )
                self.assertEqual(json.loads(status.read_text())["state"], "needs-configuration")
                self.assertEqual(set(apply(check_only=True)), {"first"})
                # Invalid enabled choice stays enabled in policy but inactive in scheduler.
                value["jobs"]["first"]["schedule"] = "not a schedule"
                choose(value)
                with self.assertRaises(r.PolicyError):
                    apply(status_path=status)
                self.assertFalse(rows()["first"]["enabled"])
                self.assertEqual(json.loads(policy.read_text()), value)
                self.assertEqual(json.loads(status.read_text())["state"], "error")
                value["jobs"]["first"]["schedule"] = "every 1h"
                choose(value)
                apply()
                self.assertTrue(rows()["first"]["enabled"])
                # Entry reset pauses in place; enabling it reuses identity/history.
                del value["jobs"]["first"]
                choose(value)
                apply()
                self.assertFalse(rows()["first"]["enabled"])
                self.assertEqual(rows()["first"]["id"], first["id"])
                self.assertEqual(rows()["first"]["last_status"], "success")
                self.assertEqual(
                    json.loads(policy.read_text())["jobs"]["first"], {"status": "unconfigured"}
                )
                # Existing opt-outs migrate once; future declarations are unconfigured.
                choose(
                    {
                        "schema_version": 1,
                        "jobs": {"first": {"schedule": "every 1h", "deliver": "local"}},
                    }
                )
                apply()
                migrated = json.loads(policy.read_text())
                self.assertEqual(migrated["jobs"]["second"], {"status": "disabled"})
                self.assertTrue(rows()["first"]["enabled"])
                # Removing a declaration retires only its native managed record.
                declare(desired[1:])
                apply()
                self.assertEqual(rows(), {"manual": manual})
                self.assertEqual(json.loads(policy.read_text()), migrated)
                declare(desired)
                apply()
                policy.unlink()
                apply(bootstrap=True)
                self.assertFalse(rows()["first"]["enabled"])
                self.assertTrue(
                    all(
                        e["status"] == "unconfigured"
                        for e in json.loads(policy.read_text())["jobs"].values()
                    )
                )
                # Malformed and symlink policy cannot overwrite preferences or run jobs.
                policy.write_text("{")
                with self.assertRaises(r.PolicyError):
                    apply()
                self.assertEqual(policy.read_text(), "{")
                policy.unlink()
                target = home / "protected.json"
                target.write_text('{"keep":true}')
                policy.symlink_to(target)
                with self.assertRaises(r.PolicyError):
                    apply()
                self.assertEqual(target.read_text(), '{"keep":true}')
                declare([])
                apply()
                self.assertEqual(rows(), {"manual": manual})
                self.assertEqual(sentinel.read_text(), '{"cursor":"preserve"}')
            with jobs.use_cron_store(sibling):
                self.assertEqual(jobs.list_jobs(include_disabled=True), [other])
