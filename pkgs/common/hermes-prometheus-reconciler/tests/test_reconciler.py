from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
PKG = HERE.parents[1]
FOUNDATION = HERE.parents[2] / "hermes-workflow-state" / "src"
sys.path[:0] = [str(PKG / "src"), str(FOUNDATION)]

from hermes_prometheus_reconciler import (  # noqa: E402
    ContractError,
    KanbanCLI,
    PrometheusClient,
    Reconciler,
    _new_state,
    alert_identity,
    render_metrics,
    run_external_watchdog,
)


def alert(
    *,
    host="abra",
    state="firing",
    partial=None,
    summary="Backup failed",
    alertname="OpsJobFailed",
    job="asset_backup_garage",
):
    labels = {
        "__name__": "ALERTS",
        "alertname": alertname,
        "alertstate": state,
        "actionable": "true",
        "notify_agent": "true",
        "severity": "critical",
        "domain": "backup",
        "host": host,
        "job": job,
    }
    if partial is not None:
        labels["phase"] = partial
    return {
        "metric": labels,
        "value": [1_787_800_000, "1"],
        "annotations": {"summary": summary, "description": "The latest run failed."},
    }


class FakePrometheus:
    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.calls = 0

    def firing_alerts(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.rows)

    def ready(self):
        if self.error:
            raise self.error
        return True


class FakeKanban:
    def __init__(self):
        self.by_key = {}
        self.tasks = {}
        self.creates = 0
        self.comments = 0

    def ensure_task(self, *, idempotency_key, title, body):
        if idempotency_key not in self.by_key:
            task_id = f"t_{len(self.by_key) + 1:08d}"
            self.by_key[idempotency_key] = task_id
            self.tasks[task_id] = {
                "id": task_id,
                "idempotency_key": idempotency_key,
                "title": title,
                "body": body,
                "comments": [],
            }
            self.creates += 1
        return self.by_key[idempotency_key]

    def ensure_comment(self, task_id, marker, body):
        task = self.tasks[task_id]
        if not any(marker in row for row in task["comments"]):
            task["comments"].append(f"{marker}\n{body}")
            self.comments += 1


class FlakyKanban(FakeKanban):
    def __init__(self):
        super().__init__()
        self.lose_first_create_response = True

    def ensure_task(self, *, idempotency_key, title, body):
        task_id = super().ensure_task(idempotency_key=idempotency_key, title=title, body=body)
        if self.lose_first_create_response:
            self.lose_first_create_response = False
            raise RuntimeError("simulated response loss after atomic create")
        return task_id


class FlakyCommentKanban(FakeKanban):
    def __init__(self):
        super().__init__()
        self.lose_first_comment_response = True

    def ensure_comment(self, task_id, marker, body):
        super().ensure_comment(task_id, marker, body)
        if self.lose_first_comment_response:
            self.lose_first_comment_response = False
            raise RuntimeError("simulated lost comment response")


class SlowKanban(FakeKanban):
    def ensure_task(self, *, idempotency_key, title, body):
        import time

        time.sleep(0.05)
        return super().ensure_task(idempotency_key=idempotency_key, title=title, body=body)


class ReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.metrics = Path(self.tmp.name) / "metrics.prom"
        self.kanban = FakeKanban()
        self.now = 1_787_800_000

    def tearDown(self):
        self.tmp.cleanup()

    def reconciler(self, rows):
        return Reconciler(
            state_dir=self.state,
            metrics_path=self.metrics,
            prometheus=FakePrometheus(rows),
            kanban=self.kanban,
            clock=lambda: self.now,
        )

    def test_firing_repeat_restart_and_resolution_are_one_lifecycle(self):
        first = self.reconciler([alert()]).run()
        self.assertEqual(first["created"], 1)
        self.assertEqual(self.kanban.creates, 1)

        self.now += 60
        repeated = self.reconciler([alert()]).run()
        self.assertEqual(repeated["created"], 0)
        self.assertEqual(self.kanban.creates, 1)

        self.now += 60
        resolved = self.reconciler([]).run()
        self.assertEqual(resolved["resolved"], 1)
        self.assertEqual(self.kanban.comments, 1)

        self.now += 60
        self.reconciler([]).run()
        self.assertEqual(self.kanban.comments, 1)

        self.now += 60
        recurrence = self.reconciler([alert()]).run()
        self.assertEqual(recurrence["created"], 1)
        self.assertEqual(self.kanban.creates, 2)
        self.assertEqual(len(self.kanban.by_key), 2)

    def test_stable_identity_ignores_routing_labels_and_label_order(self):
        left = alert()
        right = alert()
        right["metric"] = dict(reversed(list(right["metric"].items())))
        right["metric"]["severity"] = "warning"
        self.assertEqual(alert_identity(left), alert_identity(right))

    def test_resource_dimensions_distinguish_partial_failures(self):
        self.assertNotEqual(
            alert_identity(alert(partial="dump")), alert_identity(alert(partial="upload"))
        )

    def test_no_change_has_no_kanban_mutation_or_model_wake(self):
        self.reconciler([]).run()
        self.reconciler([]).run()
        self.assertEqual(self.kanban.creates, 0)
        self.assertEqual(self.kanban.comments, 0)
        self.assertNotIn("model", self.metrics.read_text())

    def test_failed_prometheus_query_never_infers_recovery(self):
        self.reconciler([alert()]).run()
        broken = Reconciler(
            state_dir=self.state,
            metrics_path=self.metrics,
            prometheus=FakePrometheus(error=RuntimeError("query unavailable")),
            kanban=self.kanban,
            clock=lambda: self.now + 60,
        )
        with self.assertRaises(RuntimeError):
            broken.run()
        self.assertEqual(self.kanban.comments, 0)

    def test_lost_create_response_reuses_native_idempotency_key_on_restart(self):
        self.kanban = FlakyKanban()
        with self.assertRaises(RuntimeError):
            self.reconciler([alert()]).run()
        self.assertEqual(self.kanban.creates, 1)
        recovered = self.reconciler([]).run()
        self.assertEqual(recovered["created"], 1)
        self.assertEqual(recovered["resolved"], 1)
        self.assertEqual(self.kanban.creates, 1)
        self.assertEqual(self.kanban.comments, 1)

    def test_stale_missing_running_too_long_and_partial_failure_contracts(self):
        rows = [
            alert(alertname="OpsJobStale", job="backup"),
            alert(alertname="OpsJobMetricsMissing", job="all-ops-metrics"),
            alert(alertname="OpsJobRunningTooLong", job="rebuild"),
            alert(alertname="OpsJobFailed", job="database-backup", partial="dump"),
        ]
        result = self.reconciler(rows).run()
        self.assertEqual(result["created"], 4)
        self.assertEqual(self.kanban.creates, 4)

    def test_unactionable_and_ntfy_transport_rows_are_denied(self):
        ignored = alert()
        ignored["metric"]["actionable"] = "false"
        ntfy = alert()
        ntfy["metric"]["source"] = "ntfy"
        with self.assertRaises(ContractError):
            self.reconciler([ignored]).run()
        with self.assertRaises(ContractError):
            self.reconciler([ntfy]).run()
        self.assertEqual(self.kanban.creates, 0)

    def test_duplicate_identity_fails_closed_without_mutation(self):
        with self.assertRaises(ContractError):
            self.reconciler([alert(), alert()]).run()
        self.assertEqual(self.kanban.creates, 0)

    def test_bounded_cardinality_and_label_contract(self):
        rows = [alert(host=f"host-{index}") for index in range(501)]
        with self.assertRaises(ContractError):
            self.reconciler(rows).run()
        malformed = alert()
        malformed["metric"]["bad label"] = "x"
        with self.assertRaises(ContractError):
            self.reconciler([malformed]).run()

    def test_metrics_have_fixed_label_free_names(self):
        text = render_metrics({"runs_total": 2, "kanban_mutations_total": 1})
        self.assertIn("hermes_prometheus_reconciler_runs_total 2\n", text)
        self.assertNotIn("{", text)
        with self.assertRaises(ContractError):
            render_metrics({"per_incident": 1})

    def test_generation_retention_is_bounded_on_no_change_runs(self):
        for _ in range(12):
            self.reconciler([]).run()
        generations = [
            path for path in (self.state / "generations").iterdir() if path.name.startswith("g-")
        ]
        self.assertLessEqual(len(generations), 3)

    def test_resolved_history_is_pruned_before_new_identity(self):
        state = _new_state()
        for index in range(500):
            digest, identity = alert_identity(alert(host=f"resolved-{index}"))
            state["incidents"][digest] = {
                "identity": [list(item) for item in identity],
                "epoch": 1,
                "status": "resolved",
                "card_id": f"t_{index:08d}",
                "first_seen": index,
                "last_seen": index,
                "resolved_at": index,
                "recovery_marker": f"prometheus-recovery:{digest}:1",
            }
        seeded = self.reconciler([])
        seeded.store.publish({"state.json": state})
        result = self.reconciler([alert(host="new-host")]).run()
        self.assertEqual(result["created"], 1)
        persisted = seeded.store.read().documents["state.json"]
        self.assertEqual(len(persisted["incidents"]), 500)

    def test_concurrent_runs_share_one_authoritative_run_lock(self):
        kanban = SlowKanban()

        def run_once():
            return Reconciler(
                state_dir=self.state,
                metrics_path=self.metrics,
                prometheus=FakePrometheus([alert()]),
                kanban=kanban,
                clock=lambda: self.now,
            ).run()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run_once(), range(2)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(kanban.creates, 1)


class BoundaryTests(unittest.TestCase):
    def test_prometheus_client_accepts_established_http_transport(self):
        client = PrometheusClient("http://192.168.20.100:9090", Path("/u"), Path("/p"))
        self.assertEqual(client.endpoint, "http://192.168.20.100:9090")

    def test_prometheus_client_uses_exact_canonical_query(self):
        client = PrometheusClient("https://prometheus.test", Path("/u"), Path("/p"))
        expected = 'ALERTS{alertstate="firing",actionable="true",notify_agent="true"}'
        with mock.patch.object(
            client,
            "_get_json",
            return_value={"status": "success", "data": {"resultType": "vector", "result": []}},
        ) as get_json:
            self.assertEqual(client.firing_alerts(), [])
        get_json.assert_called_once_with("/api/v1/query", {"query": expected})

    def test_prometheus_credentials_are_not_forwarded_across_redirects(self):
        class TargetHandler(BaseHTTPRequestHandler):
            authorization = None

            def do_GET(self):
                type(self).authorization = self.headers.get("Authorization")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                return

        target = HTTPServer(("127.0.0.1", 0), TargetHandler)

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/capture")
                self.end_headers()

            def log_message(self, *_args):
                return

        redirect = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (target, redirect)
        ]
        for thread in threads:
            thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                username = Path(directory) / "username"
                password = Path(directory) / "password"
                username.write_text("reader\n", encoding="utf-8")
                password.write_text("secret\n", encoding="utf-8")
                username.chmod(0o400)
                password.chmod(0o400)
                client = PrometheusClient(
                    f"http://127.0.0.1:{redirect.server_port}",
                    username,
                    password,
                )
                with self.assertRaises(ContractError):
                    client._get_json("/api/v1/query", {"query": "up"})
            self.assertIsNone(TargetHandler.authorization)
        finally:
            redirect.shutdown()
            target.shutdown()
            redirect.server_close()
            target.server_close()
            for thread in threads:
                thread.join(timeout=1)

    def test_prometheus_runtime_password_rejects_broad_mode_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            username = root / "username"
            password = root / "password"
            username.write_text("reader\n")
            password.write_text("secret\n")
            username.chmod(0o444)
            password.chmod(0o644)
            client = PrometheusClient("https://prometheus.test", username, password)
            with self.assertRaisesRegex(ContractError, "unsafe type or mode"):
                client._auth_header()
            password.chmod(0o400)
            link = root / "password-link"
            link.symlink_to(password)
            linked = PrometheusClient("https://prometheus.test", username, link)
            with self.assertRaisesRegex(ContractError, "unsafe type or mode"):
                linked._auth_header()

    def test_reconciler_freshness_requires_one_recent_fixed_metric(self):
        client = PrometheusClient("https://prometheus.test", Path("/u"), Path("/p"))
        payload = {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "30"]}]},
        }
        with mock.patch.object(client, "_get_json", return_value=payload) as get_json:
            self.assertTrue(client.reconciler_recent(180))
        expected_metric = "hermes_prometheus_reconciler_last_success_timestamp_seconds"
        get_json.assert_called_once_with("/api/v1/query", {"query": f"time() - {expected_metric}"})
        self.assertIn(
            f"{expected_metric} 1\n",
            render_metrics({"last_success_timestamp_seconds": 1}),
        )
        payload["data"]["result"][0]["value"][1] = "181"
        with mock.patch.object(client, "_get_json", return_value=payload):
            self.assertFalse(client.reconciler_recent(180))
        payload["data"]["result"] = []
        with mock.patch.object(client, "_get_json", return_value=payload):
            self.assertFalse(client.reconciler_recent(180))
        missing = PrometheusClient(
            "https://prometheus.test", Path("/missing-username"), Path("/missing-password")
        )
        self.assertFalse(missing.ready())

    def test_native_kanban_cli_uses_explicit_board_and_no_shell(self):
        cli = KanbanCLI("/bin/hermes", "homelab-devops", "system-admin", "homelab-devops")
        digest, identity = alert_identity(alert())
        _, title, body = Reconciler._card(digest, identity, 1)
        self.assertIn("\n", body)
        created = {"task": {"id": "t_123", "idempotency_key": "prometheus:abc:1"}}
        shown = {"task": {"id": "t_123", "idempotency_key": "prometheus:abc:1", "comments": []}}
        completed = [
            mock.Mock(returncode=0, stdout=json.dumps(created)),
            mock.Mock(returncode=0, stdout=json.dumps(shown)),
        ]
        with mock.patch(
            "hermes_prometheus_reconciler.subprocess.run", side_effect=completed
        ) as run:
            task_id = cli.ensure_task(idempotency_key="prometheus:abc:1", title=title, body=body)
        self.assertEqual(task_id, "t_123")
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[:4], ["/bin/hermes", "kanban", "--board", "homelab-devops"])
        self.assertIn("--idempotency-key", command)
        self.assertEqual(command[command.index("--body") + 1], body)
        self.assertFalse(run.call_args_list[0].kwargs.get("shell", False))

    def test_native_kanban_rejects_controls_and_multiline_titles(self):
        cli = KanbanCLI("/bin/hermes", "homelab-devops", "system-admin", "homelab-devops")
        for control in ("\x00", "\x1b", "\r", "\t"):
            with self.subTest(control=repr(control)), mock.patch.object(cli, "_run") as run:
                with self.assertRaises(ContractError):
                    cli.ensure_task(
                        idempotency_key="key", title="Incident", body=f"Bad{control}body"
                    )
                with self.assertRaises(ContractError):
                    cli.ensure_comment("t_123", "marker", f"Bad{control}body")
                run.assert_not_called()
        with mock.patch.object(cli, "_run") as run:
            with self.assertRaises(ContractError):
                cli.ensure_task(idempotency_key="key", title="Two\nlines", body="Evidence")
            run.assert_not_called()

    def test_native_comment_uses_supported_cli_and_top_level_comment_readback(self):
        cli = KanbanCLI("/bin/hermes", "homelab-devops", "system-admin", "homelab-devops")
        before = {"task": {"id": "t_123"}, "comments": []}
        after = {"task": {"id": "t_123"}, "comments": [{"body": "recovery:abc\nRecovered"}]}
        completed = [
            mock.Mock(returncode=0, stdout=json.dumps(before)),
            mock.Mock(returncode=0, stdout="Comment added"),
            mock.Mock(returncode=0, stdout=json.dumps(after)),
        ]
        with mock.patch(
            "hermes_prometheus_reconciler.subprocess.run", side_effect=completed
        ) as run:
            cli.ensure_comment("t_123", "recovery:abc", "Recovered\nPrometheus is healthy.")
        comment_command = run.call_args_list[1].args[0]
        self.assertEqual(
            comment_command[:5], ["/bin/hermes", "kanban", "--board", "homelab-devops", "comment"]
        )
        self.assertNotIn("--json", comment_command)
        self.assertEqual(comment_command[-1], "recovery:abc\nRecovered\nPrometheus is healthy.")


class ExternalWatchdogTests(unittest.TestCase):
    def test_external_watchdog_is_metrics_only_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics = Path(directory) / "watchdog.prom"
            prometheus = mock.Mock()
            prometheus.ready.return_value = True
            prometheus.reconciler_recent.return_value = True
            result = run_external_watchdog(
                prometheus,
                metrics,
                180,
                clock=lambda: 1_787_800_000,
            )
            self.assertTrue(result["healthy"])
            self.assertIn("hermes_prometheus_external_watchdog_healthy 1", metrics.read_text())

            prometheus.ready.return_value = False
            result = run_external_watchdog(
                prometheus,
                metrics,
                180,
                clock=lambda: 1_787_800_060,
            )
            self.assertFalse(result["healthy"])
            self.assertIn("hermes_prometheus_external_watchdog_healthy 0", metrics.read_text())
            prometheus.reconciler_recent.assert_called_once_with(180)

    def test_obsolete_same_host_watchdog_surface_is_absent(self):
        import hermes_prometheus_reconciler as module

        self.assertFalse(hasattr(module, "SSHControlPlaneProbe"))
        self.assertFalse(hasattr(module, "Watchdog"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            module.main(["watchdog"])


if __name__ == "__main__":
    unittest.main()
