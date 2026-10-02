"""Native CLI/dispatcher integration, no network or paid model calls.

HERMES_TEST_SOURCE must point at the repository-pinned, unmodified Hermes
source. Mandatory in the Nix integration check; absent locally is an error,
not a skipped/pass result. Only the worker spawn boundary is instrumented.
"""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import forgejo_event_journal as j
import forgejo_kanban_workflow as w


class IssueSource:
    origin = "https://git.example.test"

    def __init__(self):
        self.origin = w.instance().forgejo_origin
        self.issue = {
            "number": 12,
            "title": "Repair service",
            "body": "Observed failure",
            "state": "open",
            "labels": [],
            "updated_at": "2026-09-15T10:00:00Z",
        }

    def pages(self, path, query=None):
        return [self.issue] if path.endswith("/issues") else []

    def snapshot(self, event):
        return {
            "target": "issue",
            "number": 12,
            "url": self.origin + f"/{w.instance().repository}/issues/12",
            "issue": self.issue,
            "comments": [],
        }


class JournalBoundary:
    def __init__(self, store):
        self.store = store

    def pending(self):
        return [w.validate_event(e) for e in self.store.pending()]

    def ack(self, event_id, proof):
        self.store.acknowledge(event_id, proof)


class NativeFixture(unittest.TestCase):
    def setUp(self):
        source = os.environ.get("HERMES_TEST_SOURCE")
        if not source:
            self.fail("HERMES_TEST_SOURCE is required for native integration")
        sys.path.insert(0, source)
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli import kanban_db_dispatch as kbd

        self.kbc, self.kbd = kbc, kbd
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        (self.home / "isolated-test-home").touch()
        contract = json.loads(Path(os.environ["HERMES_REPOSITORY_INSTANCE"]).read_text())
        contract["board"] = "default"
        contract.update(getattr(self, "instance_overrides", {}))
        self.board = contract["board"]
        profile = self.home / "profiles" / contract["roles"]["implementer"]
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("{}\n")
        contract_path = self.home / "instance.json"
        contract_path.write_text(json.dumps(contract))
        self.env = patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(self.home),
                "HERMES_REPOSITORY_INSTANCE": str(contract_path),
                "HERMES_KANBAN_BOARD": self.board,
                "HERMES_KANBAN_DB": str(self.home / "kanban.db"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        from hermes_cli import kanban_db

        kanban_db.create_board(self.board)
        self.kbc.init_db(board=self.board)
        self.cli = Path(
            os.environ.get("HERMES_TEST_CLI", str(Path(__file__).with_name("native_cli.py")))
        ).resolve()
        self.kanban = w.NativeKanban(self.cli, self.board, self.home)
        self.journal = j.Journal(Path(self.tmp.name) / "journal")
        if not getattr(self, "defer_journal", False):
            self.journal.initialize()
        if not getattr(self, "defer_journal", False):
            self.mappings = w.MappingStore(Path(self.tmp.name) / "mappings")
        self.source = IssueSource()
        self.spawned = []

    def poll(self):
        events = j.poll_events(self.source, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
        self.journal.ingest(events, source="poll")
        return w.reconcile(JournalBoundary(self.journal), self.source, self.kanban, self.mappings)

    def reference_registry(self, repo):
        """Real clean Git cache for consumers; no remote SSH authentication claim."""
        import json
        import subprocess

        root = Path(self.tmp.name)
        origin = "ssh://git@fixture.invalid/nixos/nix-config.git"
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
        key = root / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        known = root / "known_hosts"
        known.write_text("fixture.invalid " + key.with_suffix(".pub").read_text())
        registry = self.home / "job-config/repository-sync/registry.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "nix-config-pull": {
                            "origin": origin,
                            "repository": "nixos/nix-config",
                            "ref": "refs/heads/master",
                            "destination": str(repo),
                            "transport": "ssh",
                            "mode": "immutable-reference-cache",
                            "timeout_seconds": 30,
                            "retries": 0,
                            "lock_timeout_seconds": 5,
                            "ssh_host": "fixture.invalid",
                            "ssh_port": 22,
                            "known_hosts_file": str(known),
                            "identity_file": str(key),
                        }
                    },
                }
            )
        )
        for directory, dirs, files in os.walk(repo, topdown=False):
            for name in files + dirs:
                path = Path(directory) / name
                path.chmod(path.stat().st_mode & ~0o222)
            Path(directory).chmod(0o555)
        return registry

    def dispatch(self):
        def spawn(task, workspace, **kwargs):
            self.spawned.append(task.id)
            return os.getpid()  # live PID: do not simulate a crashed worker on next tick

        with self.kbc.connect_closing(board=self.board) as conn:
            return self.kbd.dispatch_once(
                conn, spawn_fn=spawn, board=self.board, max_in_progress_per_profile=1
            )


class NativeSecondInstanceTests(NativeFixture):
    instance_overrides = {
        "id": "fixture-widget",
        "repository": "acme/widgets",
        "default_branch": "trunk",
        "namespace": "widgets",
        "workflow_key_prefix": "widget-forgejo",
        "board": "engineering",
        "roles": {"implementer": "builder", "reviewer": "auditor"},
        "tenant": "engineering",
        "automation_authors": ["builder-bot"],
        "forgejo_origin": "https://code.example.test",
        "projection": {
            "origin": "https://tasks.example.test",
            "project": 72,
            "owner": "alex",
            "title": "Decisions",
        },
    }

    def test_same_packages_route_other_repository_to_other_profile_once(self):
        self.assertEqual(self.poll()["created"], 1)
        task_id = next(iter(self.mappings.read().values()))["current_task_id"]
        task = self.kanban.show(task_id)["task"]
        self.assertEqual(task["assignee"], "builder")
        self.assertIn("https://code.example.test/acme/widgets/issues/12", task["body"])
        self.assertIn("widget-forgejo:acme/widgets:issue:12", self.mappings.read())
        self.assertEqual(len(self.dispatch().spawned), 1)
        for _ in range(2):
            self.assertEqual(self.poll()["events"], 0)
            self.assertEqual(self.dispatch().spawned, [])
        self.assertEqual(self.spawned, [task_id])


class NativeIntakeTests(NativeFixture):
    def test_issue_without_comment_dispatches_once_and_repeat_never_spawns(self):
        self.assertEqual(self.poll()["created"], 1)
        self.assertEqual(len(self.dispatch().spawned), 1)
        for _ in range(3):
            self.assertEqual(self.poll()["events"], 0)
            self.assertEqual(self.dispatch().spawned, [])
        self.assertEqual(len(self.spawned), 1)
        task = self.kanban.show(self.spawned[0])
        self.assertEqual(task["task"]["status"], "running")
        self.assertIn("planning", task["task"]["body"].lower())

    def test_parent_timestamp_churn_is_not_new_work(self):
        self.poll()
        self.dispatch()
        self.source.issue["updated_at"] = "2026-09-16T10:00:00Z"
        self.assertEqual(self.poll()["events"], 0)
        self.assertEqual(self.dispatch().spawned, [])

    def test_issue_edit_is_forwarded_to_running_task(self):
        self.poll()
        self.dispatch()
        before = self.kanban.show(self.spawned[0])
        self.source.issue.update(body="Changed requirement", updated_at="2026-09-16T10:00:00Z")
        self.assertEqual(self.poll()["actionable"], 1)
        self.assertEqual(self.dispatch().spawned, [])
        after = self.kanban.show(self.spawned[0])
        self.assertEqual(len(after["comments"]), len(before["comments"]) + 1)
        self.assertEqual(self.poll()["events"], 0)

    def test_existing_issue_work_is_adopted_without_competing_worker(self):
        legacy = self.kanban._run(
            [
                "create",
                "Existing implementation",
                "--body",
                "Forgejo authority: https://git.example.test/nixos/nix-config/issues/12",
                "--assignee",
                "system-admin",
                "--json",
            ],
            expect_json=True,
        )
        self.dispatch()
        self.poll()
        self.assertEqual(self.dispatch().spawned, [])
        self.assertEqual(
            self.mappings.read()[w.logical_key("issue", 12)]["current_task_id"], legacy["id"]
        )
        rows = self.kanban._run(["list", "--json"], expect_json=True)
        self.assertEqual(len(rows), 1)

    def test_comments_do_not_release_unrelated_human_gate(self):
        self.poll()
        task_id = next(iter(self.mappings.read().values()))["current_task_id"]
        self.kanban._run(
            ["block", task_id, "Awaiting deployment authorization", "--kind", "needs_input"]
        )
        task = self.kanban.show(task_id)
        self.kanban.wake_actionable(task, {"event_id": "fj-" + "f" * 64, "kind": "comment"})
        self.assertEqual(self.kanban.show(task_id)["task"]["status"], "blocked")

    def test_comment_on_human_only_issue_does_not_create_work(self):
        self.source.issue["labels"] = [{"name": "human-only"}]
        event = j.make_event(
            origin=self.source.origin,
            kind="comment",
            source_key="comment:41",
            revision="2026-09-15T10:00:00Z",
            target_kind="issue",
            target_number=12,
            disposition="actionable",
            summary="human comment",
            semantic={"id": 41, "body": "Updated instructions"},
            source="poll",
        )
        self.journal.ingest([event], source="poll")
        w.reconcile(JournalBoundary(self.journal), self.source, self.kanban, self.mappings)
        self.assertEqual(self.dispatch().spawned, [])
        self.assertEqual(self.kanban._run(["list", "--json"], expect_json=True), [])

    def test_own_echo_does_not_reenter_running_worker(self):
        self.poll()
        self.dispatch()
        task_id = self.spawned[0]
        before = self.kanban.show(task_id)["comments"]
        event = j.make_event(
            origin=self.source.origin,
            kind="comment",
            source_key="comment:42",
            revision="2026-09-15T11:00:00Z",
            target_kind="issue",
            target_number=12,
            disposition="evidence",
            summary="own echo",
            semantic={"id": 42, "body": "automation reply"},
            source="poll",
        )
        self.journal.ingest([event], source="poll")
        w.reconcile(JournalBoundary(self.journal), self.source, self.kanban, self.mappings)
        self.assertEqual(self.kanban.show(task_id)["comments"], before)
        self.assertEqual(self.dispatch().spawned, [])


class NativeDocsTests(NativeFixture):
    def audit_header(self, task_id):
        import json

        return {
            **json.loads(self.kanban.show(task_id)["task"]["body"].splitlines()[0])["docs_audit"],
            "task_id": task_id,
        }

    def test_two_repositories_on_same_board_never_share_audit_even_with_common_history(self):
        from dataclasses import replace
        from hermes_repository_instance import instance, instance_scope
        import hermes_nix_maintenance as m
        import subprocess

        repo, git, first, audit = self.docs_fixture()
        other_repo = Path(self.tmp.name) / "other-repo"
        subprocess.run(
            ["git", "clone", str(repo), str(other_repo)], check=True, capture_output=True
        )
        other = replace(instance(), identity="other-audit", repository="acme/widgets")
        # Initial null bases and then identical non-null bases/targets both need isolation.
        for index, base in enumerate((None, first)):
            if base:
                (repo / "module.nix").write_text("next")
                git("add", ".")
                git("commit", "-m", "next")
                subprocess.run(
                    ["git", "-C", str(other_repo), "pull", "--ff-only"],
                    check=True,
                    capture_output=True,
                )
                audit.store.publish({"successful.json": {"commit": base}})
            a = audit.reconcile(self.kanban)
            with instance_scope(other):
                second = m.DocsAudit(Path(self.tmp.name) / f"other-state-{index}", other_repo)
                if base:
                    second.store.publish({"successful.json": {"commit": base}})
                b = second.reconcile(self.kanban)
                self.assertEqual(second.reconcile(self.kanban)["task_id"], b["task_id"])
                self.assertEqual(second.successful_commit(), base)
            self.assertNotEqual(a["task_id"], b["task_id"])
            self.assertEqual(audit.reconcile(self.kanban)["task_id"], a["task_id"])

    def test_foreign_completion_identity_or_native_owner_cannot_ack(self):
        import copy
        import json
        import hermes_nix_maintenance as m

        _repo, _git, _target, audit = self.docs_fixture()
        task_id = audit.reconcile(self.kanban)["task_id"]
        self.dispatch()
        header = self.audit_header(task_id)
        self.kanban._run(
            [
                "complete",
                task_id,
                "--summary",
                "Audit complete",
                "--metadata",
                json.dumps({"docs_audit": {**header, "result": "success", "findings": []}}),
            ]
        )
        original = self.kanban.show(task_id)
        variants = []
        for field in (
            "repository",
            "instance",
            "origin",
            "branch",
            "namespace",
            "board",
            "profile",
        ):
            row = copy.deepcopy(original)
            row["runs"][-1]["metadata"]["docs_audit"]["identity"][field] = "foreign"
            variants.append(row)
        for field in ("profile",):
            row = copy.deepcopy(original)
            row["runs"][-1][field] = "foreign"
            variants.append(row)
        for field in ("key", "task_id"):
            row = copy.deepcopy(original)
            row["runs"][-1]["metadata"]["docs_audit"][field] = "foreign"
            variants.append(row)
        for field in ("assignee", "idempotency_key", "created_by"):
            row = copy.deepcopy(original)
            row["task"][field] = "foreign"
            variants.append(row)
        row = copy.deepcopy(original)
        del row["runs"][-1]["metadata"]["docs_audit"]["identity"]
        variants.append(row)
        for row in variants:
            with (
                patch.object(self.kanban, "show", return_value=row),
                self.assertRaises(m.WorkflowError),
            ):
                audit.reconcile(self.kanban)
            self.assertIsNone(audit.successful_commit())
        self.assertEqual(audit.reconcile(self.kanban)["status"], "unchanged")

    def test_lost_create_response_recovers_same_card_without_model_work(self):
        _repo, _git, _target, audit = self.docs_fixture()
        original = self.kanban._run

        def lose_response(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == "create":
                raise OSError("lost native write response")
            return result

        with (
            patch.object(self.kanban, "_run", side_effect=lose_response),
            self.assertRaises(OSError),
        ):
            audit.reconcile(self.kanban)
        rows = original(["list", "--json"], expect_json=True)
        self.assertEqual(len(rows), 1)
        recovered = audit.reconcile(self.kanban)
        self.assertEqual(recovered["task_id"], rows[0]["id"])
        self.assertEqual(len(self.dispatch().spawned), 1)
        self.assertEqual(audit.reconcile(self.kanban)["task_id"], recovered["task_id"])
        self.assertEqual(self.dispatch().spawned, [])
        self.assertIsNone(audit.successful_commit())

    def test_unbound_legacy_audit_blocks_without_creating_duplicate(self):
        import json
        import hermes_nix_maintenance as m
        from hermes_repository_instance import instance

        _repo, _git, target, audit = self.docs_fixture()
        task = self.kanban._run(
            [
                "create",
                "Legacy audit",
                "--body",
                json.dumps({"docs_audit": {"base": None, "target": target}}),
                "--assignee",
                instance().implementer,
                "--idempotency-key",
                instance().namespace + "-docs-audit:initial",
                "--json",
            ],
            expect_json=True,
        )
        with self.assertRaises(m.WorkflowError):
            audit.reconcile(self.kanban)
        self.assertEqual(
            [r["id"] for r in self.kanban._run(["list", "--json"], expect_json=True)], [task["id"]]
        )
        self.assertIsNone(audit.successful_commit())

    def test_installed_docs_cli_does_not_depend_on_injected_pythonpath(self):
        import json
        import subprocess

        repo, _git, _target, _audit = self.docs_fixture()
        registry = self.reference_registry(repo)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        command = [
            os.environ["HERMES_TEST_MAINTENANCE"],
            "docs-gate",
            "--registry",
            str(registry),
            "--state-dir",
            str(Path(self.tmp.name) / "docs-state"),
            "--hermes",
            str(self.cli),
            "--board",
            "default",
        ]
        repo.chmod(0o755)
        invalid = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(self.dispatch().spawned, [])
        self.assertIsNone(_audit.successful_commit())
        repo.chmod(0o555)
        first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        pending = json.loads(first.stdout)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(len(self.dispatch().spawned), 1)
        second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual(json.loads(second.stdout)["task_id"], pending["task_id"])
        self.assertEqual(self.dispatch().spawned, [])

    def docs_fixture(self):
        import subprocess

        import hermes_nix_maintenance as m

        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
            ).strip()

        git("init", "-b", "master")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Isolated test")
        (repo / "module.nix").write_text("first")
        git("add", ".")
        git("commit", "-m", "first")
        target = git("rev-parse", "HEAD")
        audit = m.DocsAudit(Path(self.tmp.name) / "docs-state", repo)
        return repo, git, target, audit

    def test_success_ack_tracks_fixed_master_target_and_next_range(self):
        repo, git, target, audit = self.docs_fixture()
        self.assertTrue(hasattr(audit, "reconcile"), "missing native semantic audit handoff")
        created = audit.reconcile(self.kanban)
        self.assertEqual(created["status"], "pending")
        self.assertIsNone(audit.successful_commit())
        self.dispatch()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(audit.reconcile(self.kanban)["task_id"], created["task_id"])
        self.assertEqual(self.dispatch().spawned, [])
        (repo / "module.nix").write_text("second")
        git("add", ".")
        git("commit", "-m", "second")
        self.kanban._run(
            [
                "complete",
                created["task_id"],
                "--summary",
                "Audit complete; no findings",
                "--metadata",
                __import__("json").dumps(
                    {
                        "docs_audit": {
                            **self.audit_header(created["task_id"]),
                            "base": None,
                            "target": target,
                            "findings": [],
                            "result": "success",
                        }
                    }
                ),
            ]
        )
        next_work = audit.reconcile(self.kanban)
        self.assertEqual(audit.successful_commit(), target)
        self.assertNotEqual(next_work["task_id"], created["task_id"])
        self.assertEqual(next_work["status"], "pending")

    def test_completed_without_proof_does_not_advance_marker(self):
        import hermes_nix_maintenance as m

        _repo, _git, _target, audit = self.docs_fixture()
        task_id = audit.reconcile(self.kanban)["task_id"]
        self.dispatch()
        self.kanban._run(["complete", task_id, "--summary", "Incomplete audit"])
        with self.assertRaises(m.WorkflowError):
            audit.reconcile(self.kanban)
        self.assertIsNone(audit.successful_commit())

    def test_explicit_partial_result_cannot_advance_success_marker(self):
        import json
        import hermes_nix_maintenance as m

        _repo, _git, target, audit = self.docs_fixture()
        task_id = audit.reconcile(self.kanban)["task_id"]
        self.dispatch()
        self.kanban._run(
            [
                "complete",
                task_id,
                "--summary",
                "Partial audit, coverage incomplete",
                "--metadata",
                json.dumps(
                    {
                        "docs_audit": {
                            **self.audit_header(task_id),
                            "base": None,
                            "target": target,
                            "result": "partial",
                            "findings": [],
                        }
                    }
                ),
            ]
        )
        with self.assertRaises(m.WorkflowError):
            audit.reconcile(self.kanban)
        self.assertIsNone(audit.successful_commit())

    def test_failed_ack_retries_same_successful_native_handoff(self):
        import json

        _repo, _git, target, audit = self.docs_fixture()
        task_id = audit.reconcile(self.kanban)["task_id"]
        self.dispatch()
        self.kanban._run(
            [
                "complete",
                task_id,
                "--summary",
                "Completed audit",
                "--metadata",
                json.dumps(
                    {
                        "docs_audit": {
                            **self.audit_header(task_id),
                            "base": None,
                            "target": target,
                            "result": "success",
                            "findings": [],
                        }
                    }
                ),
            ]
        )
        with (
            patch.object(
                audit.store, "publish", side_effect=OSError("injected ACK storage failure")
            ),
            self.assertRaises(OSError),
        ):
            audit.reconcile(self.kanban)
        self.assertIsNone(audit.successful_commit())
        self.assertEqual(audit.reconcile(self.kanban)["status"], "unchanged")
        self.assertEqual(audit.successful_commit(), target)
        self.assertEqual(self.dispatch().spawned, [])

    def test_concurrent_polls_create_only_one_native_audit(self):
        from concurrent.futures import ThreadPoolExecutor

        _repo, _git, _target, audit = self.docs_fixture()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: audit.reconcile(self.kanban), range(2)))
        self.assertEqual(results[0]["task_id"], results[1]["task_id"])
        self.assertEqual(len(self.dispatch().spawned), 1)
        self.assertIsNone(audit.successful_commit())


if __name__ == "__main__":
    unittest.main()
