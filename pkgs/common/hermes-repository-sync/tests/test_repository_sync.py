import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import hermes_repository_sync as sync  # noqa: E402


class RepositorySyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.git("init", "-b", "main", cwd=self.remote)
        self.git("config", "user.name", "Fixture", cwd=self.remote)
        self.git("config", "user.email", "fixture@example.test", cwd=self.remote)
        (self.remote / "tracked.txt").write_text("one\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=self.remote)
        self.git("commit", "-m", "initial", cwd=self.remote)
        self.destination = self.root / "checkouts" / "cv"
        self.state = self.root / "state"
        self.registry = self.root / "registry.json"
        self.write_registry()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def write_registry(self, **overrides):
        job = {
            "origin": self.remote.as_uri(),
            "repository": "fixtures/remote",
            "ref": "refs/heads/main",
            "destination": str(self.destination),
            "transport": "local-test",
            "mode": "immutable-reference-cache",
            "timeout_seconds": 10,
            "retries": 1,
            "lock_timeout_seconds": 0,
        }
        job.update(overrides)
        registry = {"schema_version": 1, "jobs": {"cv-repo-pull": job}}
        self.registry.write_text(json.dumps(registry), encoding="utf-8")

    def run_sync(self, *, job="cv-repo-pull"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = sync.run(self.registry, job, self.state, allow_local_test_transport=True)
        return result, out.getvalue(), err.getvalue()

    @staticmethod
    def make_writable_for_fixture(path):
        for root, directories, files in os.walk(path, topdown=False):
            for name in files:
                candidate = Path(root) / name
                if not candidate.is_symlink():
                    candidate.chmod(0o600)
            for name in directories:
                candidate = Path(root) / name
                if not candidate.is_symlink():
                    candidate.chmod(0o700)
            Path(root).chmod(0o700)

    def test_absent_destination_is_cloned_and_success_is_silent(self):
        result, out, err = self.run_sync()
        self.assertEqual(result, sync.Result.CHANGED)
        self.assertEqual((self.destination / "tracked.txt").read_text(), "one\n")
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        paths = [self.destination, *self.destination.rglob("*")]
        self.assertFalse(any(path.lstat().st_mode & 0o222 for path in paths))
        self.assertEqual(self.git("symbolic-ref", "HEAD", cwd=self.destination), "refs/heads/main")

    def test_current_destination_is_no_change_and_silent(self):
        self.run_sync()
        before = self.git("rev-parse", "HEAD", cwd=self.destination)
        result, out, err = self.run_sync()
        self.assertEqual(result, sync.Result.NO_CHANGE)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), before)
        self.assertEqual((out, err), ("", ""))

    def test_obsidian_job_uses_the_same_generic_path_with_separate_config(self):
        data = json.loads(self.registry.read_text())
        job = data["jobs"].pop("cv-repo-pull")
        job["destination"] = str(self.root / "checkouts" / "obsidian")
        data["jobs"]["obsidian-main-vault-pull"] = job
        self.registry.write_text(json.dumps(data))
        destination = Path(job["destination"])
        result, out, err = self.run_sync(job="obsidian-main-vault-pull")
        self.assertEqual(result, sync.Result.CHANGED)
        self.assertEqual((destination / "tracked.txt").read_text(), "one\n")
        self.assertEqual((out, err), ("", ""))

    def test_system_admin_jobs_clone_and_update_independently(self):
        names = (
            "nix-config-pull",
            "omanix-pull",
            "nix-containers-pull",
            "family-controls-pull",
            "hermes-config-pull",
        )
        template = json.loads(self.registry.read_text())["jobs"]["cv-repo-pull"]
        jobs = {
            name: {**template, "destination": str(self.root / "checkouts" / name)} for name in names
        }
        self.registry.write_text(json.dumps({"schema_version": 1, "jobs": jobs}))
        for name in names:
            with self.subTest(job=name):
                self.assertEqual(self.run_sync(job=name), (sync.Result.CHANGED, "", ""))
                self.assertEqual(self.run_sync(job=name), (sync.Result.NO_CHANGE, "", ""))
        (self.remote / "tracked.txt").write_text("two\n")
        self.git("commit", "-am", "advance", cwd=self.remote)
        for name in names:
            with self.subTest(job=name):
                self.assertEqual(self.run_sync(job=name), (sync.Result.CHANGED, "", ""))
                self.assertEqual(
                    (Path(jobs[name]["destination"]) / "tracked.txt").read_text(), "two\n"
                )

    def test_remote_advance_is_fast_forwarded_by_atomic_replacement(self):
        self.run_sync()
        old_inode = self.destination.stat().st_ino
        (self.remote / "tracked.txt").write_text("two\n", encoding="utf-8")
        self.git("commit", "-am", "advance", cwd=self.remote)
        result, out, err = self.run_sync()
        self.assertEqual(result, sync.Result.CHANGED)
        self.assertEqual((self.destination / "tracked.txt").read_text(), "two\n")
        self.assertNotEqual(self.destination.stat().st_ino, old_inode)
        self.assertEqual((out, err), ("", ""))

    def test_git_execution_never_uses_pull_merge_reset_or_clean(self):
        commands = []
        original_git = sync.git

        def capture(job, args, cwd=None, *, retry=False):
            commands.append(tuple(args))
            return original_git(job, args, cwd, retry=retry)

        with mock.patch.object(sync, "git", side_effect=capture):
            self.run_sync()
            self.run_sync()
            (self.remote / "tracked.txt").write_text("two\n", encoding="utf-8")
            self.git("commit", "-am", "advance", cwd=self.remote)
            self.run_sync()
        forbidden = {"pull", "merge", "reset", "clean"}
        self.assertFalse(any(command and command[0] in forbidden for command in commands))

    def test_dirty_checkout_fails_without_mutation(self):
        self.run_sync()
        self.make_writable_for_fixture(self.destination)
        (self.destination / "tracked.txt").write_text("local\n", encoding="utf-8")
        before = self.git("rev-parse", "HEAD", cwd=self.destination)
        with self.assertRaisesRegex(sync.SyncError, "dirty"):
            self.run_sync()
        self.assertEqual((self.destination / "tracked.txt").read_text(), "local\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), before)

    def test_untracked_file_counts_as_dirty_and_is_preserved(self):
        self.run_sync()
        self.make_writable_for_fixture(self.destination)
        (self.destination / "private.txt").write_text("keep\n", encoding="utf-8")
        with self.assertRaisesRegex(sync.SyncError, "dirty"):
            self.run_sync()
        self.assertEqual((self.destination / "private.txt").read_text(), "keep\n")

    def test_ignored_file_counts_as_local_data_and_is_preserved(self):
        self.run_sync()
        self.make_writable_for_fixture(self.destination)
        exclude = self.destination / ".git" / "info" / "exclude"
        exclude.write_text("private.txt\n", encoding="utf-8")
        (self.destination / "private.txt").write_text("keep\n", encoding="utf-8")
        (self.remote / "tracked.txt").write_text("two\n", encoding="utf-8")
        self.git("commit", "-am", "advance", cwd=self.remote)
        with self.assertRaisesRegex(sync.SyncError, "dirty"):
            self.run_sync()
        self.assertEqual((self.destination / "private.txt").read_text(), "keep\n")
        self.assertEqual((self.destination / "tracked.txt").read_text(), "one\n")

    def test_detached_checkout_fails_without_mutation(self):
        self.run_sync()
        self.make_writable_for_fixture(self.destination)
        head = self.git("rev-parse", "HEAD", cwd=self.destination)
        self.git("checkout", "--detach", cwd=self.destination)
        with self.assertRaisesRegex(sync.SyncError, "detached"):
            self.run_sync()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), head)

    def test_wrong_origin_fails_before_network_or_checkout_mutation(self):
        self.run_sync()
        self.make_writable_for_fixture(self.destination)
        other = self.root / "other"
        self.git("init", "--bare", str(other), cwd=self.root)
        self.git("remote", "set-url", "origin", other.as_uri(), cwd=self.destination)
        before = self.git("rev-parse", "HEAD", cwd=self.destination)
        with self.assertRaisesRegex(sync.SyncError, "origin"):
            self.run_sync()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), before)

    def test_wrong_ref_fails_without_mutation(self):
        self.run_sync()
        self.make_writable_for_fixture(self.destination)
        self.write_registry(ref="refs/heads/other")
        before = self.git("rev-parse", "HEAD", cwd=self.destination)
        with self.assertRaisesRegex(sync.SyncError, "ref"):
            self.run_sync()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), before)

    def test_missing_remote_ref_fails_without_mutation(self):
        self.run_sync()
        before = self.git("rev-parse", "HEAD", cwd=self.destination)
        self.git("branch", "-m", "main", "gone", cwd=self.remote)
        with self.assertRaises(sync.SyncError) as raised:
            self.run_sync()
        self.assertEqual(raised.exception.failure_class, "remote_ref")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), before)

    def test_local_divergence_fails_and_preserves_local_commit(self):
        self.run_sync()
        self.make_writable_for_fixture(self.destination)
        self.git("config", "user.name", "Fixture", cwd=self.destination)
        self.git("config", "user.email", "fixture@example.test", cwd=self.destination)
        (self.destination / "local.txt").write_text("local\n", encoding="utf-8")
        self.git("add", "local.txt", cwd=self.destination)
        self.git("commit", "-m", "local", cwd=self.destination)
        sync._make_read_only(self.destination)
        local_head = self.git("rev-parse", "HEAD", cwd=self.destination)
        (self.remote / "remote.txt").write_text("remote\n", encoding="utf-8")
        self.git("add", "remote.txt", cwd=self.remote)
        self.git("commit", "-m", "remote", cwd=self.remote)
        with self.assertRaisesRegex(sync.SyncError, "diverged"):
            self.run_sync()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), local_head)
        self.assertEqual((self.destination / "local.txt").read_text(), "local\n")

    def test_interruption_before_atomic_exchange_preserves_old_checkout(self):
        self.run_sync()
        (self.remote / "tracked.txt").write_text("two\n", encoding="utf-8")
        self.git("commit", "-am", "advance", cwd=self.remote)
        with (
            mock.patch.object(sync, "atomic_exchange", side_effect=InterruptedError("fixture")),
            self.assertRaises(InterruptedError),
        ):
            self.run_sync()
        self.assertEqual((self.destination / "tracked.txt").read_text(), "one\n")

    def test_publication_fsync_failure_is_reconciled_by_restart(self):
        self.run_sync()
        (self.remote / "tracked.txt").write_text("two\n", encoding="utf-8")
        self.git("commit", "-am", "advance", cwd=self.remote)
        with (
            mock.patch.object(
                sync,
                "_fsync_directory",
                side_effect=[None, None, sync.SyncError("fixture", "durability")],
            ),
            self.assertRaisesRegex(sync.SyncError, "fixture"),
        ):
            self.run_sync()
        self.assertEqual((self.destination / "tracked.txt").read_text(), "two\n")
        result, _, _ = self.run_sync()
        self.assertEqual(result, sync.Result.NO_CHANGE)

    def test_concurrent_write_during_exchange_rolls_back_and_preserves_data(self):
        self.run_sync()
        (self.remote / "tracked.txt").write_text("two\n", encoding="utf-8")
        self.git("commit", "-am", "advance", cwd=self.remote)
        original_exchange = sync.atomic_exchange

        def exchange_then_write(left, right):
            original_exchange(left, right)
            Path(left).chmod(0o700)
            (Path(left) / "concurrent.txt").write_text("preserve\n")

        with (
            mock.patch.object(sync, "atomic_exchange", side_effect=exchange_then_write),
            self.assertRaisesRegex(sync.SyncError, "original cache restored"),
        ):
            self.run_sync()
        self.assertEqual((self.destination / "tracked.txt").read_text(), "one\n")
        self.assertEqual((self.destination / "concurrent.txt").read_text(), "preserve\n")

    def test_lock_contention_fails_without_touching_destination(self):
        lock = sync.lock_path(self.state, "cv-repo-pull")
        lock.parent.mkdir(parents=True)
        with lock.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(sync.SyncError, "lock"):
                self.run_sync()
        self.assertFalse(self.destination.exists())

    def test_symlink_destination_is_rejected(self):
        target = self.root / "target"
        target.mkdir()
        self.destination.parent.mkdir()
        self.destination.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(sync.SyncError, "symlink"):
            self.run_sync()
        self.assertEqual(list(target.iterdir()), [])

    def test_symlink_parent_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.destination.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(sync.SyncError, "symlink"):
            self.run_sync()
        self.assertEqual(list(outside.iterdir()), [])

    def test_state_directory_may_not_overlap_the_checkout(self):
        nested_state = self.destination / ".hermes-sync-state"
        with self.assertRaisesRegex(sync.ConfigError, "overlap"):
            sync.run(
                self.registry,
                "cv-repo-pull",
                nested_state,
                allow_local_test_transport=True,
            )
        self.assertFalse(self.destination.exists())

    def test_unsafe_metrics_target_is_rejected_before_checkout_mutation(self):
        metrics = self.state / "metrics"
        metrics.mkdir(parents=True)
        outside = self.root / "outside.prom"
        outside.write_text("preserve\n")
        (metrics / "cv-repo-pull.prom").symlink_to(outside)
        with self.assertRaisesRegex(sync.SyncError, "metrics"):
            self.run_sync()
        self.assertFalse(self.destination.exists())
        self.assertEqual(outside.read_text(), "preserve\n")

    def test_tracked_symlink_escape_is_rejected_before_promotion(self):
        (self.remote / "escape").symlink_to("/etc/passwd")
        self.git("add", "escape", cwd=self.remote)
        self.git("commit", "-m", "unsafe symlink", cwd=self.remote)
        with self.assertRaisesRegex(sync.SyncError, "symlink"):
            self.run_sync()
        self.assertFalse(self.destination.exists())

    def test_local_transport_requires_explicit_test_gate(self):
        with self.assertRaisesRegex(sync.ConfigError, "local-test"):
            sync.run(self.registry, "cv-repo-pull", self.state, allow_local_test_transport=False)

    def test_unknown_job_and_extra_job_are_rejected(self):
        with self.assertRaisesRegex(sync.ConfigError, "allowlisted"):
            sync.run(self.registry, "attacker", self.state, allow_local_test_transport=True)
        data = json.loads(self.registry.read_text())
        data["jobs"]["attacker"] = data["jobs"]["cv-repo-pull"]
        self.registry.write_text(json.dumps(data))
        with self.assertRaisesRegex(sync.ConfigError, "allowlisted"):
            self.run_sync()

    def test_invalid_job_name_cannot_escape_metrics_directory(self):
        with self.assertRaisesRegex(sync.ConfigError, "allowlisted"):
            sync.run(self.registry, "../../escape", self.state, allow_local_test_transport=True)
        self.assertFalse((self.root / "escape.prom").exists())

    def test_ssh_contract_requires_pinned_host_keys_and_identity(self):
        self.write_registry(
            origin="ssh://git@git.example.test/fixtures/remote.git",
            transport="ssh",
            ssh_host="git.example.test",
            ssh_port=22,
        )
        with self.assertRaisesRegex(sync.ConfigError, "known_hosts_file"):
            sync.load_job(self.registry, "cv-repo-pull", False)

    def test_ssh_origin_must_match_declared_repository_and_host(self):
        known = self.root / "known_hosts"
        identity = self.root / "id_ed25519"
        known.write_text("git.example.test ssh-ed25519 AAAATEST\n")
        identity.write_text("fixture")
        os.chmod(identity, 0o600)
        self.write_registry(
            origin="ssh://git@git.example.test/wrong/repository.git",
            transport="ssh",
            repository="fixtures/remote",
            ssh_host="git.example.test",
            ssh_port=22,
            known_hosts_file=str(known),
            identity_file=str(identity),
        )
        with self.assertRaisesRegex(sync.ConfigError, "repository"):
            sync.load_job(self.registry, "cv-repo-pull", False)

    def test_git_auth_and_host_key_errors_have_bounded_failure_class(self):
        for diagnostic in (
            "Host key verification failed.",
            "git@example.test: Permission denied (publickey).",
        ):
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(sync._classify_git_failure(diagnostic), "auth_or_host_key")

    def test_ssh_origin_rejects_controls_and_invalid_ports_as_config_errors(self):
        known = self.root / "known_hosts"
        identity = self.root / "id_ed25519"
        known.write_text("git.example.test ssh-ed25519 AAAATEST\n")
        identity.write_text("fixture")
        os.chmod(identity, 0o600)
        for origin in (
            "ssh://git@git.example.test/fixtures/remote.git\n",
            "ssh://git@git.exam\nple.test/fixtures/remote.git",
            "SSH://git@git.example.test/fixtures/remote.git",
            "ssh://git@git.example.test:22/fixtures/remote.git",
            "ssh://git@git.example.test/fixtures/remote.git/",
            "ssh://git@git.example.test:bad/fixtures/remote.git",
        ):
            with self.subTest(origin=origin):
                self.write_registry(
                    origin=origin,
                    transport="ssh",
                    repository="fixtures/remote",
                    ssh_host="git.example.test",
                    ssh_port=22,
                    known_hosts_file=str(known),
                    identity_file=str(identity),
                )
                with self.assertRaises(sync.ConfigError):
                    sync.load_job(self.registry, "cv-repo-pull", False)

    def test_immutable_cache_boundary_rejects_writable_checkout_mode(self):
        self.write_registry(mode="replaceable-working-checkout")
        with self.assertRaisesRegex(sync.ConfigError, "mode"):
            sync.load_job(self.registry, "cv-repo-pull", True)

    def test_git_invalid_or_ambiguous_refs_are_rejected(self):
        for ref in (
            "refs/heads/main//nested",
            "refs/heads/main.lock",
            "refs/heads/main..other",
            "refs/heads/main/",
        ):
            with self.subTest(ref=ref):
                self.write_registry(ref=ref)
                with self.assertRaisesRegex(sync.ConfigError, "ref"):
                    sync.load_job(self.registry, "cv-repo-pull", True)

    def test_failed_clone_leaves_destination_absent_and_reports_failure_metric(self):
        self.write_registry(origin=(self.root / "missing").as_uri())
        with self.assertRaises(sync.SyncError):
            self.run_sync()
        self.assertFalse(self.destination.exists())
        metric = self.state / "metrics" / "cv-repo-pull.prom"
        self.assertIn('result="failure"', metric.read_text())

    def test_stale_staging_is_bounded_and_never_deleted_automatically(self):
        self.destination.parent.mkdir(parents=True)
        staging = self.destination.parent / ".cv.hermes-sync.cv-repo-pull.staging"
        staging.mkdir()
        marker = staging / "preserve.txt"
        marker.write_text("uncertain prior run\n")
        with self.assertRaisesRegex(sync.SyncError, "stale staging"):
            self.run_sync()
        self.assertEqual(marker.read_text(), "uncertain prior run\n")
        self.assertFalse(self.destination.exists())

    def test_transient_clone_failure_retries_from_a_clean_temporary_directory(self):
        original_git = sync.git
        attempts = 0

        def fail_once(job, args, cwd=None, *, retry=False):
            nonlocal attempts
            if args[0] == "clone":
                attempts += 1
                if attempts == 1:
                    Path(args[-1]).mkdir()
                    (Path(args[-1]) / "partial").write_text("partial\n")
                    raise sync.SyncError("temporary network failure", "network")
            return original_git(job, args, cwd, retry=retry)

        with mock.patch.object(sync, "git", side_effect=fail_once):
            result, out, err = self.run_sync()
        self.assertEqual(result, sync.Result.CHANGED)
        self.assertEqual(attempts, 2)
        self.assertEqual((out, err), ("", ""))
        self.assertEqual((self.destination / "tracked.txt").read_text(), "one\n")

    def test_metrics_are_bounded_and_successful_no_change_is_recorded(self):
        self.run_sync()
        self.run_sync()
        metrics = (self.state / "metrics" / "cv-repo-pull.prom").read_text()
        self.assertIn('job="cv-repo-pull",result="no_change"', metrics)
        self.assertIn("hermes_repository_sync_last_success_seconds", metrics)
        self.assertNotIn(str(self.destination), metrics)
        self.assertNotIn(str(self.remote), metrics)

    def test_failure_metric_retains_previous_last_success(self):
        self.run_sync()
        metric_path = self.state / "metrics" / "cv-repo-pull.prom"
        success_line = next(
            line
            for line in metric_path.read_text().splitlines()
            if line.startswith("hermes_repository_sync_last_success_seconds{")
        )
        self.make_writable_for_fixture(self.destination)
        (self.destination / "dirty.txt").write_text("preserve\n")
        with self.assertRaises(sync.SyncError):
            self.run_sync()
        failed_metric = metric_path.read_text()
        self.assertIn('result="failure"', failed_metric)
        self.assertIn(success_line, failed_metric)

    def test_native_job_contracts_are_script_only_without_central_cadence(self):
        template = json.loads((SRC.parent / "schedule-templates.json").read_text())
        self.assertEqual(
            set(template["jobs"]),
            sync.ALLOWED_JOBS,
        )
        owners = {
            "cv-repo-pull": "profile:personal-assistant-sandro",
            "obsidian-main-vault-pull": "profile:personal-assistant-sandro",
            "nix-config-pull": "profile:system-admin",
            "omanix-pull": "profile:system-admin",
            "nix-containers-pull": "profile:system-admin",
            "family-controls-pull": "profile:system-admin",
            "hermes-config-pull": "profile:system-admin",
        }
        for name, job in template["jobs"].items():
            self.assertNotIn("schedule", job)
            self.assertEqual(job["cadence_owner"], owners[name])
            self.assertTrue(job["schedule_required_at_materialization"])
            self.assertTrue(job["no_agent"])
            self.assertEqual(job["deliver"], "local")
            self.assertEqual(job["skills"], [])
            self.assertEqual(job["enabled_toolsets"], [])

    def test_registry_removal_has_no_deletion_operation(self):
        self.run_sync()
        self.registry.write_text(json.dumps({"schema_version": 1, "jobs": {}}))
        with self.assertRaises(sync.ConfigError):
            self.run_sync()
        self.assertTrue((self.destination / "tracked.txt").exists())


if __name__ == "__main__":
    unittest.main()
