from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).parents[1] / "src" / "hermes_native_cron_reconciler.py"
SPEC = importlib.util.spec_from_file_location("hermes_native_cron_reconciler", MODULE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load native cron reconciler")
reconciler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconciler)


def contract() -> dict:
    return {
        "schema_version": 1,
        "managed_script_dir": "/var/lib/hermes/.hermes/profiles/p/scripts/.nix-managed",
        "jobs": [
            {
                "name": "script-job",
                "script": "/var/lib/hermes/.hermes/profiles/p/scripts/.nix-managed/script-job.sh",
                "no_agent": True,
                "deliver_prefix": "local",
                "skills": [],
            },
            {
                "name": "agent-job",
                "script": "/var/lib/hermes/.hermes/profiles/p/scripts/.nix-managed/agent-job.sh",
                "no_agent": False,
                "deliver_prefix": "telegram:",
                "skills": ["job-ranking"],
            },
        ],
    }


def policy() -> dict:
    return {
        "schema_version": 1,
        "jobs": {
            "script-job": {"schedule": "every 4h", "deliver": "local"},
            "agent-job": {"schedule": "0 8 * * *", "deliver": "telegram:123:456"},
        },
    }


class FakeHermes:
    def __init__(self) -> None:
        self.rows = [
            {
                "id": "unmanaged001",
                "state": "active",
                "name": "unmanaged",
                "schedule": "30m",
                "deliver": "local",
                "skills": "",
                "script": "/tmp/unmanaged.sh",
                "mode": "no-agent (script stdout delivered directly)",
            },
            {
                "id": "stale001",
                "state": "paused",
                "name": "stale-nix-job",
                "schedule": "1h",
                "deliver": "local",
                "skills": "",
                "script": "/var/lib/hermes/.hermes/profiles/p/scripts/.nix-managed/stale-nix-job.sh",
                "mode": "no-agent (script stdout delivered directly)",
            },
            {
                "id": "existing001",
                "state": "disabled",
                "name": "script-job",
                "schedule": "1m",
                "deliver": "origin",
                "skills": "bad-skill",
                "script": "/var/lib/hermes/.hermes/profiles/p/scripts/.nix-managed/old.sh",
                "mode": "agent",
            },
        ]
        self.commands: list[list[str]] = []

    def render(self) -> str:
        chunks = []
        for row in self.rows:
            chunks.extend(
                [
                    f"  {row['id']} [{row['state']}]",
                    f"    Name:      {row['name']}",
                    f"    Schedule:  {row['schedule']}",
                    f"    Deliver:   {row['deliver']}",
                    f"    Skills:    {row['skills']}",
                    f"    Script:    {row['script']}",
                    f"    Mode:      {row['mode']}",
                ]
            )
        return "\n".join(chunks) + "\n"

    @staticmethod
    def value(command: list[str], flag: str) -> str:
        return command[command.index(flag) + 1]

    def __call__(self, command, _env) -> str:
        command = list(command)
        self.commands.append(command)
        action = command[2]
        if action == "list":
            return self.render()
        if action == "edit":
            row = next(item for item in self.rows if item["id"] == command[3])
            row.update(
                schedule=self.value(command, "--schedule"),
                name=self.value(command, "--name"),
                deliver=self.value(command, "--deliver"),
                script=self.value(command, "--script"),
                skills=", ".join(
                    command[index + 1] for index, item in enumerate(command) if item == "--skill"
                ),
                mode=(
                    "no-agent (script stdout delivered directly)"
                    if "--no-agent" in command
                    else "agent"
                ),
            )
            return "updated\n"
        if action == "resume":
            next(item for item in self.rows if item["id"] == command[3])["state"] = "active"
            return "resumed\n"
        if action == "pause":
            next(item for item in self.rows if item["id"] == command[3])["state"] = "paused"
            return "paused\n"
        if action == "remove":
            self.rows = [item for item in self.rows if item["id"] != command[3]]
            return "removed\n"
        if action == "create":
            self.rows.append(
                {
                    "id": f"created{len(self.rows):03d}",
                    "state": "active",
                    "name": self.value(command, "--name"),
                    "schedule": command[3],
                    "deliver": self.value(command, "--deliver"),
                    "skills": ", ".join(
                        command[index + 1]
                        for index, item in enumerate(command)
                        if item == "--skill"
                    ),
                    "script": self.value(command, "--script"),
                    "mode": (
                        "no-agent (script stdout delivered directly)"
                        if "--no-agent" in command
                        else "agent"
                    ),
                }
            )
            return "created\n"
        raise AssertionError(command)


class NativeCronReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.contract_path = self.root / "contract.json"
        self.policy_path = self.root / "policy.json"
        self.contract_path.write_text(json.dumps(contract()), encoding="utf-8")
        self.policy_path.write_text(json.dumps(policy()), encoding="utf-8")
        os.chmod(self.policy_path, 0o600)
        reconciler._prepare_policy(self.policy_path, contract()["jobs"])

    def reconcile(self, *args, **kwargs):
        return reconciler.reconcile(*args, **kwargs)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_policy_never_pauses_same_named_independent_job(self):
        self.policy_path.unlink()
        fake = FakeHermes()
        fake.rows = [dict(fake.rows[2], state="active", script="/tmp/independent.sh")]
        before = dict(fake.rows[0])
        with redirect_stderr(io.StringIO()):
            self.reconcile(
                self.contract_path, self.policy_path, "/bin/hermes", bootstrap=True, runner=fake
            )
        self.assertEqual(fake.rows, [before])
        self.assertTrue(all(command[2] == "list" for command in fake.commands))

    def test_selected_independent_name_collision_is_read_only(self):
        fake = FakeHermes()
        fake.rows[2]["script"] = "/tmp/independent.sh"
        before = [dict(row) for row in fake.rows]
        with self.assertRaisesRegex(reconciler.ReconcileError, "independent"):
            self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=fake)
        self.assertEqual(fake.rows, before)
        self.assertTrue(all(command[2] == "list" for command in fake.commands))

    def test_bootstrap_without_policy_allows_blank_profile_and_reports_pending(self):
        self.policy_path.unlink()
        fake = FakeHermes()
        fake.rows = []
        diagnostics = io.StringIO()
        with redirect_stderr(diagnostics):
            result = self.reconcile(
                self.contract_path, self.policy_path, "/bin/hermes", bootstrap=True, runner=fake
            )
        self.assertEqual(result, {})
        self.assertEqual(fake.rows, [])
        self.assertEqual(diagnostics.getvalue(), "")
        self.assertEqual(json.loads(self.policy_path.read_text())["schema_version"], 2)
        self.assertEqual([command[2] for command in fake.commands], ["list", "list"])

    def test_reminder_bootstrapped_policy_sends_one_aggregated_native_message(self):
        self.policy_path.unlink()
        reconciler._prepare_policy(self.policy_path, contract()["jobs"])
        runner = mock.Mock(return_value='{"success": true}')
        self.assertTrue(
            reconciler.remind(
                self.contract_path,
                self.policy_path,
                self.root / "status.json",
                "/profile-hermes",
                "personal-assistant-sandro",
                "telegram:123",
                runner=runner,
            )
        )
        runner.assert_called_once()
        command = runner.call_args.args[0]
        self.assertEqual(command[:5], ["/profile-hermes", "send", "--to", "telegram:123", "--json"])
        self.assertIn("personal-assistant-sandro", command[-1])
        self.assertIn("- script-job", command[-1])
        self.assertIn("- agent-job", command[-1])
        self.assertIn("Suggested prompt", command[-1])
        self.assertIn("ask me", command[-1])
        self.assertIn("daily", command[-1])
        self.assertTrue(self.policy_path.exists())

    def test_reminder_ready_jobs_are_silent_and_status_is_unchanged(self):
        status = self.root / "status.json"
        reconciler._write_status(
            status,
            "ready",
            "Verified",
            fingerprint=reconciler._fingerprint(contract(), policy()["jobs"]),
        )
        before = status.read_bytes()
        runner = mock.Mock()
        self.assertFalse(
            reconciler.remind(
                self.contract_path,
                self.policy_path,
                status,
                "/profile-hermes",
                "p",
                "telegram:123",
                runner=runner,
            )
        )
        runner.assert_not_called()
        self.assertEqual(status.read_bytes(), before)

    def test_reminder_no_enabled_jobs_is_silent_without_policy_or_status(self):
        empty = contract()
        empty["jobs"] = []
        self.contract_path.write_text(json.dumps(empty))
        self.policy_path.unlink()
        runner = mock.Mock()
        self.assertFalse(
            reconciler.remind(
                self.contract_path,
                self.policy_path,
                self.root / "status.json",
                "/profile-hermes",
                "p",
                "telegram:123",
                runner=runner,
            )
        )
        runner.assert_not_called()

    def test_reminder_invalid_policy_cannot_be_hidden_by_stale_ready_status(self):
        status = self.root / "status.json"
        reconciler._write_status(status, "ready", "Old readiness")
        self.policy_path.write_text("{")
        runner = mock.Mock(return_value='{"success": true}')
        with self.assertRaises(reconciler.ReconcileError):
            reconciler.remind(
                self.contract_path,
                self.policy_path,
                status,
                "/profile-hermes",
                "p",
                "telegram:123",
                runner=runner,
            )
        runner.assert_not_called()

    def test_reminder_error_status_does_not_publish_private_diagnostics(self):
        self.policy_path.unlink()
        reconciler._prepare_policy(self.policy_path, contract()["jobs"])
        status = self.root / "status.json"
        reconciler._write_status(status, "error", "private diagnostic sentinel")
        runner = mock.Mock(return_value='{"success": true}')
        self.assertTrue(
            reconciler.remind(
                self.contract_path,
                self.policy_path,
                status,
                "/profile-hermes",
                "p",
                "telegram:123",
                runner=runner,
            )
        )
        self.assertNotIn("private diagnostic sentinel", runner.call_args.args[0][-1])

    def test_reminder_requires_successful_delivery(self):
        self.policy_path.unlink()
        reconciler._prepare_policy(self.policy_path, contract()["jobs"])
        for response in ('{"skipped":true}', '{"error":"no home channel"}', "not json"):
            with self.subTest(response=response), self.assertRaises(reconciler.ReconcileError):
                reconciler.remind(
                    self.contract_path,
                    self.policy_path,
                    self.root / "status.json",
                    "/profile-hermes",
                    "p",
                    "telegram:123",
                    runner=mock.Mock(return_value=response),
                )

    def test_reminder_bounds_long_job_lists(self):
        required = contract()
        template = required["jobs"][0]
        required["jobs"] = [dict(template, name=f"job-{i}-" + "x" * 100) for i in range(100)]
        self.contract_path.write_text(json.dumps(required))
        self.policy_path.unlink()
        reconciler._prepare_policy(self.policy_path, required["jobs"])
        runner = mock.Mock(return_value='{"success": true}')
        reconciler.remind(
            self.contract_path,
            self.policy_path,
            self.root / "status.json",
            "/profile-hermes",
            "p",
            "telegram:123",
            runner=runner,
        )
        self.assertLess(len(runner.call_args.args[0][-1]), 4096)
        self.assertIn("more (see the job contract)", runner.call_args.args[0][-1])

    def test_bootstrap_suspends_managed_jobs_then_valid_policy_restores_them(self):
        self.policy_path.unlink()
        fake = FakeHermes()
        for row in fake.rows:
            row["state"] = "active"
        unrelated = dict(fake.rows[0])
        with redirect_stderr(io.StringIO()):
            result = self.reconcile(
                self.contract_path, self.policy_path, "/bin/hermes", bootstrap=True, runner=fake
            )
        self.assertEqual(result, {})
        self.assertEqual(fake.rows[0], unrelated)
        self.assertTrue(all(row["state"] == "paused" for row in fake.rows[1:]))
        self.assertEqual(
            [command[2] for command in fake.commands], ["list", "remove", "pause", "list"]
        )
        self.policy_path.write_text(json.dumps(policy()))
        os.chmod(self.policy_path, 0o600)
        result = self.reconcile(
            self.contract_path, self.policy_path, "/bin/hermes", bootstrap=True, runner=fake
        )
        self.assertEqual(result, policy()["jobs"])
        self.assertTrue(all(row["state"] == "active" for row in fake.rows))
        self.assertEqual(fake.rows[0], unrelated)
        self.assertNotIn("stale-nix-job", [row["name"] for row in fake.rows])

    def test_bootstrap_invalid_policy_also_suspends_managed_jobs(self):
        for raw, mode in (("{", 0o600), (json.dumps(policy()), 0o644), ('{"jobs":{}}', 0o600)):
            with self.subTest(raw=raw, mode=mode):
                self.policy_path.write_text(raw)
                os.chmod(self.policy_path, mode)
                fake = FakeHermes()
                fake.rows[1]["state"] = "active"
                with self.assertRaises(reconciler.PolicyError):
                    self.reconcile(
                        self.contract_path,
                        self.policy_path,
                        "/bin/hermes",
                        bootstrap=True,
                        runner=fake,
                    )
                self.assertEqual(fake.rows[1]["state"], "paused")

    def test_bootstrap_requires_readback_proof_that_managed_jobs_are_inactive(self):
        self.policy_path.unlink()
        fake = FakeHermes()
        fake.rows[2]["state"] = "active"

        def runner(command, env):
            if command[2] == "pause":
                return "paused (but not actually persisted)"
            return fake(command, env)

        with self.assertRaisesRegex(reconciler.ReconcileError, "remain active"):
            self.reconcile(
                self.contract_path, self.policy_path, "/bin/hermes", bootstrap=True, runner=runner
            )

    def test_readonly_check_still_rejects_missing_policy(self):
        self.policy_path.unlink()
        fake = FakeHermes()
        with self.assertRaises(reconciler.ReconcileError):
            self.reconcile(
                self.contract_path, self.policy_path, "/bin/hermes", check_only=True, runner=fake
            )
        self.assertEqual(fake.commands, [])
        with self.assertRaises(reconciler.ReconcileError):
            self.reconcile(
                self.contract_path,
                self.policy_path,
                "/bin/hermes",
                check_only=True,
                bootstrap=True,
                runner=fake,
            )
        self.assertEqual(fake.commands, [])

    def test_bootstrap_cli_writes_private_pending_then_ready_status(self):
        self.policy_path.unlink()
        status = self.root / "status.json"
        fake = FakeHermes()
        original = reconciler.reconcile

        def run(*args, **kwargs):
            return original(*args, **kwargs, runner=fake)

        argv = [
            "--contract",
            str(self.contract_path),
            "--policy",
            str(self.policy_path),
            "--hermes",
            "/bin/hermes",
            "--bootstrap",
            "--status-file",
            str(status),
        ]
        with (
            mock.patch.object(reconciler, "reconcile", side_effect=run),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(reconciler.main(argv), 0)
            self.assertEqual(json.loads(status.read_text())["state"], "needs-configuration")
            self.assertEqual(status.stat().st_mode & 0o777, 0o600)
            self.policy_path.write_text(json.dumps(policy()))
            os.chmod(self.policy_path, 0o600)
            self.assertEqual(reconciler.main(argv), 0)
            self.assertEqual(json.loads(status.read_text())["state"], "ready")

    def test_reconcile_removes_stale_managed_edits_creates_resumes_and_preserves_unmanaged(self):
        fake = FakeHermes()
        result = self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=fake)
        self.assertEqual(result["agent-job"]["deliver"], "telegram:123:456")
        by_name = {row["name"]: row for row in fake.rows}
        self.assertEqual(set(by_name), {"unmanaged", "script-job", "agent-job"})
        self.assertEqual(by_name["unmanaged"]["schedule"], "30m")
        self.assertEqual(by_name["script-job"]["schedule"], "every 4h")
        self.assertEqual(by_name["script-job"]["deliver"], "local")
        self.assertIn("no-agent", by_name["script-job"]["mode"])
        self.assertEqual(by_name["agent-job"]["deliver"], "telegram:123:456")
        self.assertEqual(by_name["agent-job"]["skills"], "job-ranking")
        self.assertTrue(any(command[2] == "edit" for command in fake.commands))
        self.assertTrue(any(command[2] == "create" for command in fake.commands))
        self.assertTrue(any(command[2] == "resume" for command in fake.commands))
        self.assertTrue(any(command[2] == "remove" for command in fake.commands))
        self.assertFalse(any(command[2] == "enable" for command in fake.commands))

    def test_empty_desired_set_removes_final_managed_job_without_policy(self):
        empty = contract()
        empty["jobs"] = []
        self.contract_path.write_text(json.dumps(empty), encoding="utf-8")
        self.policy_path.unlink()
        fake = FakeHermes()
        fake.rows = [fake.rows[1]]

        self.assertEqual(
            self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=fake),
            {},
        )
        self.assertEqual(fake.rows, [])
        self.assertEqual([command[2] for command in fake.commands], ["list", "remove", "list"])

    def test_check_only_rejects_stale_managed_job_without_mutation(self):
        fake = FakeHermes()
        fake.rows = [fake.rows[1]]
        with self.assertRaisesRegex(reconciler.ReconcileError, "undeclared Nix-managed"):
            self.reconcile(
                self.contract_path,
                self.policy_path,
                "/bin/hermes",
                check_only=True,
                runner=fake,
            )
        self.assertEqual([command[2] for command in fake.commands], ["list"])

    def test_matching_active_jobs_are_read_only(self):
        fake = FakeHermes()
        self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=fake)
        fake.commands.clear()

        self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=fake)

        self.assertEqual([command[2] for command in fake.commands], ["list", "list"])

    def test_check_only_never_mutates(self):
        fake = FakeHermes()
        fake.rows = [row for row in fake.rows if row["name"] != "stale-nix-job"]
        with self.assertRaisesRegex(reconciler.ReconcileError, "disabled"):
            self.reconcile(
                self.contract_path,
                self.policy_path,
                "/bin/hermes",
                check_only=True,
                runner=fake,
            )
        self.assertEqual([command[2] for command in fake.commands], ["list"])

    def test_policy_must_be_private_complete_and_profile_owned(self):
        os.chmod(self.policy_path, 0o644)
        with self.assertRaisesRegex(reconciler.ReconcileError, "group or other"):
            self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=FakeHermes())
        os.chmod(self.policy_path, 0o600)
        broken = policy()
        broken["jobs"]["agent-job"]["schedule"] = "PROFILE_OWNS_FINAL_SCHEDULE"
        self.policy_path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaisesRegex(reconciler.ReconcileError, "invalid schedule"):
            self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=FakeHermes())
        broken = policy()
        broken["jobs"]["script-job"]["schedule"] = "4h"
        self.policy_path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaisesRegex(reconciler.ReconcileError, "invalid schedule"):
            self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=FakeHermes())
        broken = policy()
        broken["jobs"]["agent-job"]["deliver"] = "telegram:not-a-chat"
        self.policy_path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaisesRegex(reconciler.ReconcileError, "delivery ownership"):
            self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=FakeHermes())

    def test_contract_rejects_non_string_managed_script_directory(self):
        broken = contract()
        broken["managed_script_dir"] = 42
        self.contract_path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaisesRegex(reconciler.ReconcileError, "managed script directory"):
            self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=FakeHermes())

    def test_schedule_grammar_matches_pinned_native_hermes(self):
        accepted = [
            "every 30m",
            "every 2 hours",
            "every 1D",
            "0 8 * * *",
            "0 8 * * * 0",
            "0 8 * * * 0 2027",
        ]
        rejected = [
            "every 1s",
            "every 1w",
            "0 8 * JAN *",
            "0 8 ? * MON",
            "0 8 * * * 2027",
            "0 8 * * * * extra",
        ]
        for schedule in accepted:
            with self.subTest(schedule=schedule):
                self.assertTrue(reconciler._valid_schedule(schedule))
        for schedule in rejected:
            with self.subTest(schedule=schedule):
                self.assertFalse(reconciler._valid_schedule(schedule))

    def test_parser_accepts_ansi_and_detects_non_active_collisions(self):
        output = "\x1b[32m  abcdef123456 [completed]\x1b[0m\n    Name:      agent-job\n"
        rows = reconciler.parse_cron_list(output)
        self.assertEqual(rows[0]["state"], "completed")
        self.assertEqual(rows[0]["name"], "agent-job")

    def test_duplicate_required_name_fails_before_mutation(self):
        fake = FakeHermes()
        fake.rows.append(dict(fake.rows[2], id="duplicate001"))
        with self.assertRaisesRegex(reconciler.ReconcileError, "collision"):
            self.reconcile(self.contract_path, self.policy_path, "/bin/hermes", runner=fake)
        self.assertEqual([command[2] for command in fake.commands], ["list"])

    def test_cli_check_uses_only_profile_local_contract_inputs(self):
        output = io.StringIO()
        with (
            mock.patch.object(reconciler, "reconcile", lambda *args, **kwargs: policy()["jobs"]),
            redirect_stdout(output),
        ):
            result = reconciler.main(
                [
                    "--contract",
                    str(self.contract_path),
                    "--policy",
                    str(self.policy_path),
                    "--hermes",
                    "/bin/hermes",
                    "--check",
                    "--print-deliver",
                    "agent-job",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "telegram:123:456\n")


class ProfileJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.contract_path = self.home / "contract.json"
        self.policy_path = self.home / "native-cron-policy.json"
        self.status = self.home / "status.json"
        self.contract = contract()
        directory = self.home / "scripts/profile/.nix-managed"
        self.contract["managed_script_dir"] = str(directory)
        for job in self.contract["jobs"]:
            job["script"] = str(directory / (job["name"] + ".sh"))
        self.contract_path.write_text(json.dumps(self.contract))
        self.fake = FakeHermes()
        self.fake.rows = [self.fake.rows[0]]
        self.targets = ["telegram:123", "telegram:456"]

    def write_policy(self, value):
        self.policy_path.write_text(json.dumps(value))
        self.policy_path.chmod(0o600)

    def apply(self, **kwargs):
        with redirect_stderr(io.StringIO()):
            return reconciler.reconcile(
                self.contract_path, self.policy_path, "/hermes", runner=self.fake, **kwargs
            )

    def remind(self, runner=None):
        runner = runner or mock.Mock(return_value='{"success":true}')
        result = reconciler.remind(
            self.contract_path,
            self.policy_path,
            self.status,
            "/hermes",
            "family",
            self.targets,
            runner=runner,
        )
        return result, runner

    def mark_ready(self, policy_value):
        reconciler._write_status(
            self.status,
            "ready",
            "Verified",
            fingerprint=reconciler._fingerprint(self.contract, policy_value),
        )

    def test_blank_profile_starts_and_reminds_all_chats(self):
        self.assertEqual(self.apply(bootstrap=True), {})
        result, sender = self.remind()
        self.assertTrue(result)
        self.assertEqual([call.args[0][3] for call in sender.call_args_list], self.targets)
        messages = [call.args[0][-1] for call in sender.call_args_list]
        self.assertEqual(messages[0], messages[1])
        self.assertIn("one shared profile policy", messages[0])
        self.assertIn("disabled to opt out of that job", messages[0])

    def test_legacy_empty_policy_pauses_jobs_and_stops_all_reminders(self):
        self.write_policy(policy())
        self.apply()
        self.write_policy({})
        self.assertEqual(self.apply(), {})
        self.assertEqual(
            [row["name"] for row in self.fake.rows if row["state"] == "active"], ["unmanaged"]
        )
        self.assertEqual(len(self.fake.rows), 3)
        result, sender = self.remind()
        self.assertFalse(result)
        sender.assert_not_called()

    def test_subset_creates_one_job_for_profile_with_explicit_family_destination(self):
        choice = {
            "schema_version": 1,
            "jobs": {"agent-job": {"schedule": "every 4h", "deliver": "telegram:-123:45"}},
        }
        self.write_policy(choice)
        applied = self.apply()
        managed = [row for row in self.fake.rows if row["name"] != "unmanaged"]
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0]["name"], "agent-job")
        self.assertEqual(managed[0]["deliver"], "telegram:-123:45")
        self.mark_ready(applied)
        self.assertFalse(self.remind()[0])
        self.assertEqual(self.apply(check_only=True), applied)

    def test_changed_enabled_policy_does_not_trigger_setup_reminders(self):
        self.write_policy(policy())
        applied = self.apply()
        self.mark_ready(applied)
        self.assertFalse(self.remind()[0])
        changed = policy()
        changed["jobs"]["agent-job"]["schedule"] = "every 8h"
        self.write_policy(changed)
        self.apply()
        self.assertEqual(self.remind()[1].call_count, 0)

    def test_failed_delivery_does_not_skip_other_chats(self):
        self.apply(bootstrap=True)
        sender = mock.Mock(side_effect=[reconciler.ReconcileError("failure"), '{"success":true}'])
        with self.assertRaisesRegex(reconciler.ReconcileError, "telegram:123"):
            self.remind(sender)
        self.assertEqual(sender.call_count, 2)

    def test_targets_must_be_explicit_and_duplicates_send_once(self):
        self.apply(bootstrap=True)
        self.targets = ["telegram"]
        with self.assertRaisesRegex(reconciler.ReconcileError, "explicit"):
            self.remind()
        self.targets = ["telegram:123", "telegram:123"]
        self.assertEqual(self.remind()[1].call_count, 1)

    def test_retire_per_user_jobs_without_merging_or_deleting_settings(self):
        for identity in ("123", "456"):
            root = self.home / "job-users" / identity
            root.mkdir(parents=True)
            (root / "policy.json").write_text(json.dumps(policy()))
            self.fake.rows.append(
                {
                    **self.fake.rows[0],
                    "id": identity,
                    "name": f"telegram-{identity}--agent-job",
                    "script": str(
                        self.home / f"scripts/job-users/{identity}/.nix-managed/agent-job.sh"
                    ),
                }
            )
        self.fake.rows.append(
            {
                **self.fake.rows[0],
                "id": "old",
                "name": "old",
                "script": str(self.home / "scripts/.nix-managed/old.sh"),
            }
        )
        self.assertEqual(self.apply(bootstrap=True), {})
        self.assertEqual([row["name"] for row in self.fake.rows], ["unmanaged"])
        for identity in ("123", "456"):
            self.assertTrue((self.home / "job-users" / identity / "policy.json").exists())
        self.assertTrue(self.policy_path.exists())

    def test_migration_check_is_read_only_and_failures_are_not_ignored(self):
        self.write_policy({})
        self.fake.rows.append(
            {
                **self.fake.rows[0],
                "id": "old",
                "script": str(self.home / "scripts/.nix-managed/old.sh"),
            }
        )
        with self.assertRaisesRegex(reconciler.ReconcileError, "obsolete"):
            self.apply(check_only=True)
        self.assertTrue(all(command[2] == "list" for command in self.fake.commands))

        def failed_remove(command, env):
            return "" if command[2] == "remove" else self.fake(command, env)

        with self.assertRaisesRegex(reconciler.ReconcileError, "remain"):
            reconciler.reconcile(
                self.contract_path,
                self.policy_path,
                "/hermes",
                bootstrap=True,
                runner=failed_remove,
            )

    def test_dormant_job_retained_and_legacy_empty_jobs_object_migrated(self):
        self.write_policy(
            {"schema_version": 1, "jobs": {"unknown": {"schedule": "every 4h", "deliver": "local"}}}
        )
        self.assertEqual(self.apply(), {})
        self.assertEqual(
            json.loads(self.policy_path.read_text())["jobs"]["unknown"]["status"], "enabled"
        )
        self.write_policy({"schema_version": 1, "jobs": {}})
        self.assertEqual(self.apply(), {})

    def test_missing_policy_pauses_profile_jobs_without_stopping_gateway(self):
        self.write_policy(policy())
        self.apply()
        self.policy_path.unlink()
        self.assertEqual(self.apply(bootstrap=True), {})
        managed = [row for row in self.fake.rows if row["name"] != "unmanaged"]
        self.assertTrue(all(row["state"] == "paused" for row in managed))
        self.assertEqual(self.fake.rows[0]["state"], "active")
        self.assertEqual(self.remind()[1].call_count, 2)


@unittest.skipUnless(os.environ.get("HERMES_UPSTREAM_SOURCE"), "requires pinned Hermes source")
class UpstreamScriptCompatibilityTests(unittest.TestCase):
    def test_profile_scripts_obey_real_upstream_script_boundary_and_wake_gate(self):
        # Import the exact pinned script executor and parser. Extracting two
        # AST functions from scheduler.py no longer tests their real helpers.
        sys.path.insert(0, os.environ["HERMES_UPSTREAM_SOURCE"])
        from cron import scheduler_prompt, scheduler_script

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            namespace = {
                "_run_job_script": scheduler_script._run_job_script,
                "_parse_wake_gate": scheduler_prompt._parse_wake_gate,
            }
            env_module = mock.Mock(
                build_subprocess_env=lambda: {"PATH": os.environ["PATH"], "HERMES_HOME": str(home)}
            )
            redact_module = mock.Mock(redact_sensitive_text=lambda text: text)
            with (
                mock.patch.dict(
                    sys.modules,
                    {"tools.environments.local": env_module, "agent.redact": redact_module},
                ),
                mock.patch.object(scheduler_script._sched, "_get_hermes_home", return_value=home),
            ):
                script = home / "scripts/profile/.nix-managed/test.sh"
                script.parent.mkdir(parents=True)
                script.write_text("printf 'profile job\\n'\n")
                success, output = namespace["_run_job_script"](str(script))
                self.assertTrue(success, output)
                self.assertEqual(output, "profile job")
                outside = home / "outside.sh"
                outside.write_text("exit 99\n")
                escaped = home / "scripts/escape.sh"
                escaped.symlink_to(outside)
                success, detail = namespace["_run_job_script"](str(escaped))
                self.assertFalse(success)
                self.assertIn("outside the scripts directory", detail)
                self.assertFalse(
                    namespace["_parse_wake_gate"]('User context\n{"wakeAgent": false}')
                )


if __name__ == "__main__":
    unittest.main()
