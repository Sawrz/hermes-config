"""Per-job setup policy: isolated files and native scheduler boundary."""

import json
import fcntl
import os
import stat
import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_reconciler import FakeHermes, contract, reconciler


class PolicyV2Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.contract = contract()
        self.contract_path = self.home / "contract.json"
        self.contract_path.write_text(json.dumps(self.contract))
        self.policy_path = self.home / "native-cron-policy.json"
        self.fake = FakeHermes()
        self.fake.rows = [self.fake.rows[0]]

    def apply(self, **kwargs):
        return reconciler.reconcile(
            self.contract_path,
            self.policy_path,
            "/hermes",
            runner=self.fake,
            bootstrap=True,
            **kwargs,
        )

    def write(self, value):
        self.policy_path.write_text(json.dumps(value))
        self.policy_path.chmod(0o600)

    def test_concurrent_reconcilers_converge_without_duplicate_native_jobs(self):
        self.write(
            {
                "schema_version": 2,
                "jobs": {
                    "script-job": {"status": "enabled", "schedule": "every 4h", "deliver": "local"}
                },
            }
        )
        start = threading.Barrier(4)
        failures = []

        def apply():
            try:
                start.wait(timeout=3)
                self.apply()
            except Exception as exc:
                failures.append(exc)

        workers = [threading.Thread(target=apply) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual([row["name"] for row in self.fake.rows], ["unmanaged", "script-job"])
        self.assertEqual(
            json.loads(self.policy_path.read_text())["jobs"]["agent-job"],
            {"status": "unconfigured"},
        )

    def test_disabled_preferences_and_dormant_invalid_entries_do_not_block_enabled_jobs(self):
        self.write(
            {
                "schema_version": 2,
                "jobs": {
                    "script-job": {"status": "enabled", "schedule": "every 4h", "deliver": "local"},
                    "agent-job": {
                        "status": "disabled",
                        "schedule": "old preference",
                        "deliver": "old route",
                    },
                    "dormant-job": {"status": "enabled", "schedule": "bad"},
                },
            }
        )
        before = self.policy_path.read_bytes()
        self.assertEqual(set(self.apply()), {"script-job"})
        self.assertEqual(self.policy_path.read_bytes(), before)
        result = reconciler.reconcile(
            self.contract_path, self.policy_path, "/hermes", runner=self.fake, check_only=True
        )
        self.assertEqual(set(result), {"script-job"})

    def test_invalid_statuses_and_corrupt_envelopes_are_preserved_without_execution(self):
        invalid = [
            [],
            None,
            {"schema_version": 3, "jobs": {}},
            {"schema_version": 2, "jobs": []},
            {"schema_version": True, "jobs": {}},
            {"schema_version": 2, "jobs": {}, "unexpected": True},
        ]
        invalid += [
            {"schema_version": 2, "jobs": {"script-job": entry}}
            for entry in (
                None,
                [],
                {},
                {"status": []},
                {"status": "other"},
                {"status": "enabled"},
                {"status": "enabled", "schedule": "4h", "deliver": "local"},
                {"status": "enabled", "schedule": "every 4h", "deliver": "origin"},
            )
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.write(value)
                before = self.policy_path.read_bytes()
                with self.assertRaises(reconciler.ReconcileError):
                    self.apply()
                self.assertEqual(self.policy_path.read_bytes(), before)
                self.assertEqual([row["name"] for row in self.fake.rows], ["unmanaged"])

    def test_readonly_check_never_creates_or_migrates_policy(self):
        for value in (None, {}, {"schema_version": 1, "jobs": {}}):
            if value is not None:
                self.write(value)
            before = {p.name: p.read_bytes() for p in self.home.iterdir()}
            with self.assertRaises(reconciler.ReconcileError):
                reconciler.reconcile(
                    self.contract_path,
                    self.policy_path,
                    "/hermes",
                    check_only=True,
                    runner=self.fake,
                )
            self.assertEqual({p.name: p.read_bytes() for p in self.home.iterdir()}, before)

    def test_bootstrap_syncs_file_and_directory_and_create_never_clobbers(self):
        synced = []
        real_sync = os.fsync

        def sync(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            real_sync(fd)

        with mock.patch.object(reconciler.os, "fsync", side_effect=sync):
            self.apply()
        self.assertIn(False, synced)
        self.assertIn(True, synced, "directory publication must be durable")
        self.policy_path.unlink()
        winner = {
            "schema_version": 2,
            "jobs": {job["name"]: {"status": "disabled"} for job in self.contract["jobs"]},
        }
        real_link = os.link

        def competing_create(source, destination):
            self.write(winner)
            real_link(source, destination)

        with mock.patch.object(reconciler.os, "link", side_effect=competing_create):
            self.apply()
        self.assertEqual(json.loads(self.policy_path.read_text()), winner)

    def test_failed_policy_publication_preserves_old_policy_and_pauses_managed_jobs(self):
        self.write(
            {
                "schema_version": 1,
                "jobs": {"script-job": {"schedule": "every 4h", "deliver": "local"}},
            }
        )
        before = self.policy_path.read_bytes()
        owned = FakeHermes().rows[2]
        owned["state"] = "active"
        self.fake.rows.append(owned)
        with mock.patch.object(reconciler.os, "replace", side_effect=OSError("publication failed")):
            with self.assertRaises(reconciler.ReconcileError):
                self.apply()
        self.assertEqual(self.policy_path.read_bytes(), before)
        self.assertEqual(owned["state"], "paused")
        self.assertEqual(list(self.home.glob(".native-cron-policy-*")), [])

    def test_invalid_entry_does_not_get_rewritten_while_filling_missing_entries(self):
        self.write({"schema_version": 2, "jobs": {"script-job": {"status": "enabled"}}})
        before = self.policy_path.read_bytes()
        with self.assertRaises(reconciler.ReconcileError):
            self.apply()
        self.assertEqual(self.policy_path.read_bytes(), before)

    def test_cli_status_distinguishes_partial_setup_from_error(self):
        status = self.home / "status.json"
        original = reconciler.reconcile

        def run(*args, **kwargs):
            return original(*args, **kwargs, runner=self.fake)

        argv = [
            "--contract",
            str(self.contract_path),
            "--policy",
            str(self.policy_path),
            "--hermes",
            "/hermes",
            "--bootstrap",
            "--status-file",
            str(status),
        ]
        self.write(
            {
                "schema_version": 2,
                "jobs": {
                    "script-job": {"status": "enabled", "schedule": "every 4h", "deliver": "local"},
                    "agent-job": {"status": "unconfigured"},
                },
            }
        )
        with mock.patch.object(reconciler, "reconcile", side_effect=run):
            self.assertEqual(reconciler.main(argv), 0)
            self.assertEqual(json.loads(status.read_text())["state"], "needs-configuration")
            self.assertEqual(
                [r["name"] for r in self.fake.rows if r["state"] == "active"],
                ["unmanaged", "script-job"],
            )
            self.policy_path.write_text("{")
            self.assertEqual(
                reconciler.main(argv), 0, "invalid policy must not prevent interactive repair"
            )
            self.assertEqual(json.loads(status.read_text())["state"], "error")
            self.assertEqual(self.policy_path.read_text(), "{")
            self.assertTrue(
                all(r["state"] != "active" for r in self.fake.rows if r["name"] != "unmanaged")
            )

    def test_reminders_only_list_declared_unconfigured_jobs(self):
        self.write(
            {
                "schema_version": 2,
                "jobs": {
                    "script-job": {"status": "unconfigured"},
                    "agent-job": {"status": "enabled", "schedule": "bad"},
                    "dormant-job": {"status": "unconfigured"},
                },
            }
        )
        sender = mock.Mock(return_value='{"success":true}')
        self.assertTrue(
            reconciler.remind(
                self.contract_path,
                self.policy_path,
                self.home / "status.json",
                "/hermes",
                "p",
                "telegram:123",
                runner=sender,
            )
        )
        text = sender.call_args.args[0][-1]
        self.assertIn("- script-job", text)
        self.assertNotIn("- agent-job", text)
        self.assertNotIn("- dormant-job", text)
        self.assertNotIn("or {}", text)
        self.write(
            {
                "schema_version": 2,
                "jobs": {
                    "script-job": {"status": "disabled"},
                    "agent-job": {"status": "enabled", "schedule": "bad"},
                },
            }
        )
        sender.reset_mock()
        self.assertFalse(
            reconciler.remind(
                self.contract_path,
                self.policy_path,
                self.home / "status.json",
                "/hermes",
                "p",
                "telegram:123",
                runner=sender,
            )
        )
        sender.assert_not_called()

    def test_agent_atomic_update_is_serialized_with_bootstrap(self):
        started, finished = threading.Event(), threading.Event()
        failures = []

        def bootstrap():
            started.set()
            try:
                self.apply()
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()

        lock = self.home / "native-cron-policy.json.lock"
        with lock.open("w") as handle:
            lock.chmod(0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            worker = threading.Thread(target=bootstrap)
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(0.2), "bootstrap must wait for the policy writer")
                temp = self.home / "agent-policy.tmp"
                choice = {"schema_version": 2, "jobs": {"script-job": {"status": "disabled"}}}
                temp.write_text(json.dumps(choice))
                temp.chmod(0o600)
                temp.replace(self.policy_path)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                worker.join(3)
        self.assertTrue(finished.is_set())
        self.assertEqual(failures, [])
        saved = json.loads(self.policy_path.read_text())
        self.assertEqual(saved["jobs"]["script-job"], {"status": "disabled"})
        self.assertEqual(saved["jobs"]["agent-job"], {"status": "unconfigured"})

    def test_invalid_enabled_job_is_paused_without_rewriting_or_stopping_valid_job(self):
        value = {
            "schema_version": 2,
            "jobs": {
                "script-job": {"status": "enabled", "schedule": "every 4h", "deliver": "local"},
                "agent-job": {
                    "status": "enabled",
                    "schedule": "every 8h",
                    "deliver": "telegram:123",
                },
            },
        }
        self.write(value)
        self.apply()
        value["jobs"]["agent-job"]["schedule"] = "bad schedule"
        self.write(value)
        before = self.policy_path.read_bytes()
        with self.assertRaisesRegex(reconciler.ReconcileError, "invalid schedule"):
            self.apply()
        rows = {row["name"]: row for row in self.fake.rows}
        self.assertEqual(rows["agent-job"]["state"], "paused")
        self.assertEqual(rows["script-job"]["state"], "active")
        self.assertEqual(self.policy_path.read_bytes(), before)

    def test_legacy_migration_preserves_choices_and_new_declarations_need_setup(self):
        for legacy in (
            {},
            {
                "schema_version": 1,
                "jobs": {"script-job": {"schedule": "every 4h", "deliver": "local"}},
            },
        ):
            with self.subTest(legacy=legacy):
                self.write(legacy)
                self.apply()
                migrated = json.loads(self.policy_path.read_text())
                self.assertEqual(migrated["schema_version"], 2)
                self.assertEqual(migrated["jobs"]["agent-job"], {"status": "disabled"})
                expected = (
                    {"status": "enabled", **legacy["jobs"]["script-job"]}
                    if legacy
                    else {"status": "disabled"}
                )
                self.assertEqual(migrated["jobs"]["script-job"], expected)
                newer = {
                    **self.contract,
                    "jobs": self.contract["jobs"]
                    + [{**self.contract["jobs"][0], "name": "future-job"}],
                }
                self.contract_path.write_text(json.dumps(newer))
                self.apply()
                self.assertEqual(
                    json.loads(self.policy_path.read_text())["jobs"]["future-job"],
                    {"status": "unconfigured"},
                )
                self.contract_path.write_text(json.dumps(self.contract))

    def test_reset_one_entry_pauses_only_that_job_preserving_history_and_other_jobs(self):
        value = {
            "schema_version": 2,
            "jobs": {
                "script-job": {"status": "enabled", "schedule": "every 4h", "deliver": "local"},
                "agent-job": {
                    "status": "enabled",
                    "schedule": "every 8h",
                    "deliver": "telegram:123",
                },
            },
        }
        self.write(value)
        self.apply()
        before = {row["name"]: dict(row) for row in self.fake.rows}
        del value["jobs"]["agent-job"]
        self.write(value)
        self.apply()
        rows = {row["name"]: row for row in self.fake.rows}
        self.assertIn("agent-job", rows, "reset must pause, not remove its scheduler history")
        self.assertEqual(rows["agent-job"], {**before["agent-job"], "state": "paused"})
        self.assertEqual(rows["script-job"], before["script-job"])
        self.assertEqual(
            json.loads(self.policy_path.read_text())["jobs"]["agent-job"],
            {"status": "unconfigured"},
        )

    def test_fresh_bootstrap_creates_private_per_job_setup_and_is_idempotent(self):
        self.apply()
        self.assertTrue(self.policy_path.exists(), "bootstrap must materialize setup policy")
        self.assertEqual(
            json.loads(self.policy_path.read_text()),
            {
                "schema_version": 2,
                "jobs": {job["name"]: {"status": "unconfigured"} for job in self.contract["jobs"]},
            },
        )
        self.assertEqual(self.policy_path.stat().st_mode & 0o777, 0o600)
        before = self.policy_path.stat()
        self.apply()
        self.assertEqual(self.policy_path.stat().st_ino, before.st_ino)
        self.assertEqual(self.policy_path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual([row["name"] for row in self.fake.rows], ["unmanaged"])
