"""Native cron record correction; isolated home, no scheduler/model calls."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_native_cron_reconciler as r


class NativeCronTests(unittest.TestCase):
    def test_real_cli_subset_empty_readiness_and_manual_preservation(self):
        source = os.environ["HERMES_TEST_SOURCE"]
        cli = os.environ["HERMES_TEST_CLI"]
        sys.path.insert(0, source)
        from cron import jobs

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            scripts = home / "scripts/profile/.nix-managed"
            scripts.mkdir(parents=True)
            desired = []
            for name in ("first", "second"):
                script = scripts / f"{name}.sh"
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
            contract.write_text(
                json.dumps(
                    {"schema_version": 1, "managed_script_dir": str(scripts), "jobs": desired}
                )
            )
            policy = home / "native-cron-policy.json"

            def set_policy(names):
                policy.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "jobs": {
                                name: {"schedule": "every 1h", "deliver": "local"} for name in names
                            },
                        }
                        if names
                        else {}
                    )
                )
                policy.chmod(0o600)

            with patch.dict(os.environ, {"HERMES_HOME": str(home)}), jobs.use_cron_store(home):
                manual = jobs.create_job(name="manual", prompt="Keep me", schedule="every 2h")
                manual = jobs.list_jobs(include_disabled=True)[0]
                self.assertEqual(
                    r.reconcile(contract, policy, cli, bootstrap=True, native_contract=True), {}
                )
                self.assertEqual(jobs.list_jobs(include_disabled=True), [manual])
                set_policy(["first"])
                r.reconcile(contract, policy, cli, native_contract=True)
                rows = jobs.list_jobs(include_disabled=True)
                self.assertEqual({row["name"] for row in rows}, {"manual", "first"})
                first_id = next(row["id"] for row in rows if row["name"] == "first")
                self.assertTrue(jobs.get_job(first_id)["enabled"])
                r.reconcile(contract, policy, cli, native_contract=True, check_only=True)
                set_policy(["first", "second"])
                r.reconcile(contract, policy, cli, native_contract=True)
                self.assertEqual(jobs.get_job(first_id)["id"], first_id)
                set_policy(["second"])
                r.reconcile(contract, policy, cli, native_contract=True)
                self.assertFalse(jobs.get_job(first_id)["enabled"])
                set_policy([])
                r.reconcile(contract, policy, cli, native_contract=True)
                self.assertEqual(jobs.list_jobs(), [manual])
                observed = subprocess.run(
                    [cli, "cron", "list", "--all"], check=True, capture_output=True, text=True
                )
                self.assertEqual(
                    [
                        row["name"]
                        for row in r.parse_cron_list(observed.stdout)
                        if row["state"] == "active"
                    ],
                    ["manual"],
                )
                r.reconcile(contract, policy, cli, native_contract=True, check_only=True)

    def test_stale_prompt_and_toolsets_corrected_without_losing_runtime(self):
        source = os.environ.get("HERMES_TEST_SOURCE")
        if not source:
            self.fail("HERMES_TEST_SOURCE required; not a skipped integration")
        sys.path.insert(0, source)
        from cron import jobs

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            scripts = home / "scripts/profile/.nix-managed"
            scripts.mkdir(parents=True)
            script = scripts / "inventory.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}), jobs.use_cron_store(home):
                old = jobs.create_job(
                    name="inventory",
                    prompt="Obsolete LLM polling",
                    schedule="every 1h",
                    script=str(script),
                    no_agent=True,
                    enabled_toolsets=["terminal"],
                    model="old-model",
                    provider="old-provider",
                    base_url="https://obsolete.invalid/v1",
                )
                manual = jobs.create_job(
                    name="unrelated",
                    prompt="",
                    schedule="every 1h",
                    script=str(script),
                    no_agent=True,
                )
                jobs.update_job(
                    old["id"],
                    {"last_run_at": "2026-09-15T12:00:00+00:00", "last_status": "success"},
                )
                wanted = [
                    {
                        "name": "inventory",
                        "script": str(script),
                        "skills": [],
                        "no_agent": True,
                        "deliver_prefix": "local",
                    }
                ]
                self.assertTrue(
                    hasattr(r, "canonicalize_native"), "missing complete native contract adoption"
                )
                r.canonicalize_native(wanted, check_only=False)
                actual = jobs.get_job(old["id"])
                self.assertEqual(actual["prompt"], "")
                self.assertIsNone(actual["enabled_toolsets"])
                for key in ("model", "provider", "base_url", "model_snapshot", "provider_snapshot"):
                    self.assertIsNone(actual.get(key), key)
                self.assertEqual(actual["last_status"], "success")
                self.assertEqual(actual["last_run_at"], "2026-09-15T12:00:00+00:00")
                self.assertEqual(jobs.get_job(manual["id"])["enabled"], True)
                r.canonicalize_native(wanted, check_only=True)


if __name__ == "__main__":
    unittest.main()
