"""Installed CLIs over a real isolated TLS API, native store and dispatcher.

No production endpoint, board, token, worker or model is used. The only replaced
execution boundary is worker spawn, inherited from NativeFixture.
"""

import datetime as dt
import http.server
import json
import os
import ssl
import subprocess
import threading
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_native_intake import NativeFixture


class HttpsIntakeTests(NativeFixture):
    defer_journal = True

    def setUp(self):
        super().setUp()
        self.journal_cli = Path(os.environ["HERMES_TEST_JOURNAL"])
        self.workflow_cli = Path(os.environ["HERMES_TEST_WORKFLOW"])
        root = Path(self.tmp.name)
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        self.credential = root / "credential"
        self.credential.write_text("isolated-fixture-token")
        self.credential.chmod(0o600)
        self.comments = []
        self.requests = []
        self.fail_path = None
        self.issue_present = True
        self.issue_writes = []
        self.drop_write_response = False
        self.registry_tags = ["1.0.0", "1.1.0"]
        self.source.issue["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path)
                if path.path in {"/v2/", "/v2/app/tags/list"}:
                    payload = (
                        {}
                        if path.path == "/v2/"
                        else {"name": "app", "Tags": fixture.registry_tags}
                    )
                    body = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Docker-Distribution-API-Version", "registry/2.0")
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                path = path.path
                fixture.requests.append(path)
                if self.headers.get("Authorization") != "token isolated-fixture-token":
                    self.send_error(401)
                    return
                if path == fixture.fail_path:
                    self.send_error(503)
                    return
                prefix = "/api/v1/repos/nixos/nix-config"
                payloads = {
                    "/api/v1/user": {"login": "system-admin-agent"},
                    prefix + "/issues": [fixture.source.issue] if fixture.issue_present else [],
                    prefix + "/issues/comments": fixture.comments,
                    prefix + "/issues/12": fixture.source.issue,
                    prefix + "/issues/12/comments": fixture.comments,
                    prefix + "/pulls": [],
                }
                if path not in payloads:
                    self.send_error(404)
                    return
                body = json.dumps(payloads[path]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if (
                    self.path
                    not in {
                        "/api/v1/repos/nixos/nix-config/issues",
                        "/api/v1/repos/nixos/nix-config/issues/12/comments",
                    }
                    or self.headers.get("Authorization") != "token isolated-fixture-token"
                ):
                    self.send_error(403)
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/comments"):
                    row = {
                        "id": 100 + len(fixture.comments),
                        "body": payload["body"],
                        "user": {"login": "system-admin-agent"},
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "issue_url": fixture.endpoint + "/api/v1/repos/nixos/nix-config/issues/12",
                    }
                    row["html_url"] = (
                        fixture.endpoint + f"/nixos/nix-config/issues/12#issuecomment-{row['id']}"
                    )
                    fixture.comments.append(row)
                    body = json.dumps(row).encode()
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                fixture.issue_writes.append(payload)
                fixture.source.issue.update(payload)
                fixture.source.issue.update(
                    user={"login": "system-admin-agent"},
                    html_url=fixture.endpoint + "/nixos/nix-config/issues/12",
                )
                fixture.issue_present = True
                if fixture.drop_write_response:
                    self.close_connection = True
                    return
                body = json.dumps(fixture.source.issue).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join()

        self.addCleanup(close)
        self.endpoint = f"https://localhost:{self.server.server_port}"
        contract_path = Path(os.environ["HERMES_REPOSITORY_INSTANCE"])
        contract = json.loads(contract_path.read_text())
        contract["forgejo_origin"] = self.endpoint
        contract_path.write_text(json.dumps(contract))
        from forgejo_event_journal import Journal
        from forgejo_kanban_workflow import MappingStore

        self.journal = Journal(Path(self.tmp.name) / "journal")
        self.journal.initialize()
        self.mappings = MappingStore(Path(self.tmp.name) / "mappings")
        env = patch.dict(
            os.environ,
            {
                "SSL_CERT_FILE": str(cert),
                "NIX_SSL_CERT_FILE": str(cert),
                "NO_PROXY": "localhost,127.0.0.1",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, args, success=True) -> Any:
        result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=90)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def collect(self, success=True):
        return self.run_cli(
            [
                self.journal_cli,
                "poll",
                "--endpoint",
                self.endpoint,
                "--credential-file",
                self.credential,
                "--state-root",
                Path(self.tmp.name) / "journal",
                "--initial-since",
                "2026-01-01T00:00:00Z",
            ],
            success,
        )

    def reconcile_cli(self, success=True):
        return self.run_cli(
            [
                self.workflow_cli,
                "--endpoint",
                self.endpoint,
                "--credential-file",
                self.credential,
                "reconcile",
                "--hermes",
                self.cli,
                "--hermes-home",
                self.home,
                "--board",
                "default",
                "--journal",
                self.journal_cli,
                "--journal-state-root",
                Path(self.tmp.name) / "journal",
                "--state-root",
                Path(self.tmp.name) / "mappings",
            ],
            success,
        )

    def comment(self, body, author="human"):
        self.comments[:] = [
            {
                "id": 41,
                "body": body,
                "user": {"login": author},
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "issue_url": self.endpoint + "/api/v1/repos/nixos/nix-config/issues/12",
                "html_url": self.endpoint + "/nixos/nix-config/issues/12#issuecomment-41",
            }
        ]

    def container_intent(self, target="1.1.0"):
        import hermes_nix_maintenance as maintenance

        row = {
            "host": "fixture",
            "service": "application",
            "component": "main",
            "image": "registry.example/app",
            "current_tag": "1.0.0",
            "candidate_tag": target,
            "change_route": {
                "repository": "nixos/nix-containers",
                "revision": "b" * 40,
                "path": "modules/services/nixos/containerization/stacks/application.nix",
            },
            "policy": {"update_policy": "separate-pr", "notes": "Preserve family"},
        }
        return maintenance.build_container_intent(
            {
                "inventory": {
                    "schema_version": 2,
                    "repository": "nixos/nix-config",
                    "source_commit": "a" * 40,
                    "components": [row],
                },
                "updates": [row],
                "period": "2026-09",
            }
        )

    def test_container_issue_intent_routes_through_normal_issue_intake(self):
        import forgejo_kanban_workflow as workflow

        self.assertTrue(
            hasattr(workflow.ForgejoClient, "deliver_issue_intent"), "missing F11 issue writer"
        )
        self.issue_present = False
        client = workflow.ForgejoClient(self.endpoint, self.credential)
        intent = self.container_intent()
        proof = client.deliver_issue_intent(intent, allow_write=True)
        self.assertEqual(proof["target_url"], self.endpoint + "/nixos/nix-config/issues/12")
        self.assertEqual(proof["intent_sha256"], intent["intent_sha256"])
        self.assertEqual(len(self.issue_writes), 1)
        self.assertEqual(client.deliver_issue_intent(intent, allow_write=False), proof)
        self.assertEqual(len(self.issue_writes), 1)
        self.collect()
        self.assertEqual(self.reconcile_cli()["created"], 1)
        self.assertEqual(len(self.dispatch().spawned), 1)
        self.collect()
        self.assertEqual(self.reconcile_cli()["events"], 0)
        self.assertEqual(self.dispatch().spawned, [])

    def test_container_ack_loss_recovers_without_repeating_issue_write(self):
        import hermes_nix_maintenance as maintenance
        import forgejo_kanban_workflow as workflow

        self.assertTrue(hasattr(maintenance.IntentLedger, "deliver"), "missing intent ACK consumer")
        self.issue_present = False
        ledger = maintenance.IntentLedger(Path(self.tmp.name) / "intent", origin=self.endpoint)
        ledger.stage(self.container_intent())
        client = workflow.ForgejoClient(self.endpoint, self.credential)
        with patch.object(ledger, "acknowledge", side_effect=RuntimeError("lost local ACK")):
            with self.assertRaisesRegex(RuntimeError, "lost local ACK"):
                ledger.deliver(client)
        self.assertEqual(ledger._read()["state"], "in-flight")
        self.assertEqual(len(self.issue_writes), 1)
        self.assertEqual(ledger.deliver(client)["reason"], "acknowledged")
        self.assertEqual(len(self.issue_writes), 1)
        calls = len(self.requests)
        self.assertFalse(ledger.deliver(client)["changed"])
        self.assertEqual(len(self.requests), calls)

    def test_container_delivery_cli_reconciles_and_acknowledges(self):
        import sys
        import hermes_nix_maintenance as maintenance

        self.issue_present = False
        root = Path(self.tmp.name)
        ledger = maintenance.IntentLedger(root / "intent", origin=self.endpoint)
        ledger.stage(self.container_intent())
        endpoint_file = root / "endpoint"
        endpoint_file.write_text(self.endpoint)
        command = [
            sys.executable,
            maintenance.__file__,
            "container-deliver",
            "--state-dir",
            root / "intent",
            "--endpoint-file",
            endpoint_file,
            "--credential-file",
            self.credential,
        ]
        self.assertEqual(self.run_cli(command)["reason"], "acknowledged")
        self.assertEqual(ledger._read()["state"], "acknowledged")
        calls = len(self.requests)
        self.assertFalse(self.run_cli(command)["changed"])
        self.assertEqual(len(self.requests), calls)
        self.assertEqual(len(self.issue_writes), 1)

    def test_service_consumer_requires_native_job_publication_request(self):
        import hermes_nix_maintenance as maintenance
        import forgejo_kanban_workflow as workflow

        self.assertTrue(
            hasattr(maintenance.IntentLedger, "request_delivery"),
            "missing deterministic native request boundary",
        )
        self.issue_present = False
        ledger = maintenance.IntentLedger(Path(self.tmp.name) / "intent", origin=self.endpoint)
        ledger.stage(self.container_intent())
        client = workflow.ForgejoClient(self.endpoint, self.credential)
        self.assertFalse(ledger.deliver(client, require_request=True)["changed"])
        self.assertEqual(self.requests, [])
        ledger.request_delivery()
        self.assertFalse(ledger.request_delivery()["changed"])
        self.assertEqual(ledger.deliver(client, require_request=True)["reason"], "acknowledged")
        self.assertEqual(len(self.issue_writes), 1)
        ledger.stage(self.container_intent("1.2.0"))
        calls = len(self.requests)
        self.assertFalse(ledger.deliver(client, require_request=True)["changed"])
        self.assertEqual(len(self.requests), calls)
        self.assertEqual(self.comments, [])

    def test_lost_create_response_and_api_retry_preserve_the_intent(self):
        import hermes_nix_maintenance as maintenance
        import forgejo_kanban_workflow as workflow

        self.issue_present = False
        ledger = maintenance.IntentLedger(Path(self.tmp.name) / "intent", origin=self.endpoint)
        ledger.stage(self.container_intent())
        client = workflow.ForgejoClient(self.endpoint, self.credential)
        self.fail_path = "/api/v1/user"
        with self.assertRaises(RuntimeError):
            ledger.deliver(client)
        self.assertEqual(ledger._read()["state"], "pending")
        self.assertEqual(self.issue_writes, [])
        self.fail_path = None
        self.drop_write_response = True
        self.assertEqual(ledger.deliver(client)["reason"], "acknowledged")
        self.assertEqual(len(self.issue_writes), 1)

    def test_concurrent_delivery_processes_publish_only_once(self):
        import sys
        import hermes_nix_maintenance as maintenance

        self.issue_present = False
        root = Path(self.tmp.name)
        ledger = maintenance.IntentLedger(root / "intent", origin=self.endpoint)
        ledger.stage(self.container_intent())
        endpoint = root / "endpoint"
        endpoint.write_text(self.endpoint)
        command = list(
            map(
                str,
                [
                    sys.executable,
                    maintenance.__file__,
                    "container-deliver",
                    "--state-dir",
                    root / "intent",
                    "--endpoint-file",
                    endpoint,
                    "--credential-file",
                    self.credential,
                ],
            )
        )
        processes = [
            subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr + stdout)
        self.assertEqual(len(self.issue_writes), 1)
        self.assertEqual(ledger._read()["state"], "acknowledged")

    def test_legacy_monthly_lineage_is_reused_but_closed_history_blocks_publication(self):
        import forgejo_kanban_workflow as workflow

        intent = self.container_intent()
        period = intent["period"]
        self.source.issue["title"] = f"Monthly container image update report — {period}"
        self.source.issue["body"] = (
            f"<!-- system-admin-container-image-monthly-update:{period} -->\nHuman notes"
        )
        original = self.source.issue["body"]
        self.source.issue["user"] = {"login": "system-admin-agent"}
        self.source.issue["html_url"] = (
            f"{self.endpoint}/nixos/nix-config/issues/{self.source.issue['number']}"
        )
        self.source.issue["state"] = "closed"
        client = workflow.ForgejoClient(self.endpoint, self.credential)
        with self.assertRaises(RuntimeError):
            client.deliver_issue_intent(intent, allow_write=True)
        self.assertEqual(self.issue_writes, [])
        self.assertEqual(self.comments, [])
        self.source.issue["state"] = "open"
        client.deliver_issue_intent(intent, allow_write=True)
        self.assertEqual(self.issue_writes, [])
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.source.issue["body"], original)
        self.assertIn('"inputs"', self.comments[0]["body"])
        self.source.issue["body"] = "Human edit retains only the historical title"
        client.deliver_issue_intent(intent, allow_write=False)
        self.assertEqual(len(self.comments), 1)

    def test_monthly_issue_updates_preserve_human_body_and_append_one_report(self):
        import forgejo_kanban_workflow as workflow

        self.issue_present = False
        client = workflow.ForgejoClient(self.endpoint, self.credential)
        client.deliver_issue_intent(self.container_intent(), allow_write=True)
        self.source.issue["body"] += "\nHuman maintenance-window note.\n"
        human_body = self.source.issue["body"]
        intent = self.container_intent("1.2.0")
        proof = client.deliver_issue_intent(intent, allow_write=True)
        self.assertEqual(self.source.issue["body"], human_body)
        self.assertEqual(len(self.issue_writes), 1)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(client.deliver_issue_intent(intent, allow_write=False), proof)
        self.assertEqual(len(self.comments), 1)

    def test_generated_native_container_pipeline_stays_pre_model_until_planning(self):
        import shlex
        from cron import scheduler
        import hermes_nix_maintenance as maintenance

        wiring = json.loads(Path(os.environ["HERMES_TEST_CONTAINER_WIRING"]).read_text())
        self.assertTrue(all(job["noAgent"] for job in wiring["jobs"].values()))
        self.assertEqual(len(wiring["consumer"]), 1)
        consumer = shlex.split(wiring["consumer"][0])
        self.assertIn("--require-request", consumer)
        self.assertIn("container-deliver", consumer)
        root = Path(self.tmp.name)
        repo = root / "reference"
        repo.mkdir()
        image = "localhost:" + str(self.server.server_port) + "/app"
        (repo / "flake.nix").write_text(
            """{ outputs = { self }: {
          sourceRepositories.deployment = {
            root = self.outPath; repository = "nixos/nix-config"; revision = self.rev;
          };
          nixosConfigurations.fixture.options.custom.containers.application.images = {
            definitionsWithLocations = [];
            declarations = [ "${self.outPath}/flake.nix" ];
          };
          nixosConfigurations.fixture.config.custom.containers.application = {
            enable = true; images.main = { url = "IMAGE"; tag = "1.0.0"; };
          }; }; }""".replace("IMAGE", image)
        )

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(repo), *args], text=True, stderr=subprocess.STDOUT
            ).strip()

        git("init", "-b", "master")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("add", ".")
        git("commit", "-m", "isolated reference input")
        self.reference_registry(repo)
        feeds = self.home / "job-config/container-image-maintenance/feeds.json"
        feeds.parent.mkdir(parents=True)
        feeds.write_text(
            json.dumps(
                {
                    "components": {
                        "application.main": {
                            "image": image,
                            "update_policy": "separate-pr",
                            "notes": "Keep operational constraints",
                            "source": {
                                "type": "registry_tags",
                                "image": image,
                                "status": "curated",
                                "tag_regex": r"^(?P<version>\d+\.\d+\.\d+)$",
                            },
                        }
                    }
                }
            )
        )
        self.issue_present = False
        env = {
            "NIX_REMOTE": "local?root=" + str(root / "nix-store"),
            "XDG_CACHE_HOME": str(root / "cache"),
        }
        intent_root = self.home / "state/container-image-maintenance/update-intent"
        consumer[consumer.index("--instance-config") + 1] = os.environ["HERMES_REPOSITORY_INSTANCE"]
        consumer[consumer.index("--state-dir") + 1] = str(intent_root)
        consumer[consumer.index("--endpoint") + 1] = self.endpoint
        consumer[consumer.index("--credential-file") + 1] = str(self.credential)
        scripts = self.home / "scripts/jobs/.nix-managed"
        scripts.mkdir(parents=True)
        with (
            patch.dict(os.environ, env),
            patch.object(
                scheduler,
                "_resolve_cron_agent_setup",
                side_effect=AssertionError("unexpected provider resolution"),
            ) as model,
            patch.object(
                scheduler, "_construct_cron_agent", side_effect=AssertionError("unexpected agent")
            ) as agent,
        ):
            for name, spec in wiring["jobs"].items():
                # Rebind only the generated launcher's explicit deployment contract
                # to this isolated instance; package source and native runner are unchanged.
                import re
                import shlex

                script = Path(spec["script"]).read_text()
                self.assertIn("--instance-config ", script)
                script = re.sub(
                    r"(--instance-config )[^\s]+",
                    lambda m: m[1] + shlex.quote(os.environ["HERMES_REPOSITORY_INSTANCE"]),
                    script,
                )
                (scripts / (name + ".sh")).write_text(script)
                (scripts / (name + ".sh")).chmod(0o700)

            def run(name):
                spec = wiring["jobs"][name]
                result = scheduler.run_job(
                    {
                        "id": name,
                        "name": name,
                        "no_agent": spec["noAgent"],
                        "script": "jobs/.nix-managed/" + name + ".sh",
                        "deliver": "local",
                        "skills": [],
                    }
                )
                self.assertTrue(result[0], result[3])
                self.assertTrue(scheduler._is_cron_silence_response(result[2]))

            run("container-image-monthly-inventory-refresh")
            run("container-image-monthly-workflow-curation-apply")
            self.assertEqual(self.issue_writes, [])
            run("container-image-monthly-update-issue-gate")
            completed = subprocess.run(consumer, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(self.issue_writes), 1)
            self.assertEqual(
                maintenance.IntentLedger(intent_root, origin=self.endpoint)._read()["state"],
                "acknowledged",
            )
            self.assertEqual(self.spawned, [])
            # Use the installed HTTPS intake, not the inherited in-memory
            # source whose origin predates the isolated TLS server.
            self.collect()
            self.assertEqual(self.reconcile_cli()["created"], 1)
            self.assertEqual(len(self.dispatch().spawned), 1)
            calls = len(self.requests)
            self.assertEqual(subprocess.run(consumer, capture_output=True).returncode, 0)
            self.assertEqual(len(self.requests), calls)
            self.collect()
            self.assertEqual(self.reconcile_cli()["events"], 0)
            self.assertEqual(self.dispatch().spawned, [])
            model.assert_not_called()
            agent.assert_not_called()

    def test_installed_collection_routing_replay_and_handled_echo(self):
        self.collect()
        self.assertEqual(self.reconcile_cli()["created"], 1)
        self.assertEqual(len(self.dispatch().spawned), 1)
        task_id = self.spawned[0]
        for _ in range(2):
            self.collect()
            self.assertEqual(self.reconcile_cli()["events"], 0)
            self.assertEqual(self.dispatch().spawned, [])
        self.comment("New human requirement")
        self.collect()
        self.assertEqual(self.reconcile_cli()["actionable"], 1)
        self.assertEqual(self.dispatch().spawned, [])
        before = self.kanban.show(task_id)["comments"]
        self.comment("Edited human requirement")
        self.collect()
        self.assertEqual(self.reconcile_cli()["actionable"], 1)
        self.assertEqual(len(self.kanban.show(task_id)["comments"]), len(before) + 1)
        self.kanban._run(["complete", task_id, "--summary", "Fixture work completed"])
        self.comment(
            "Handled. <!-- hermes:handled-comment repo=nixos/nix-config "
            "source=comment:40 card=t_abcdef12 -->",
            "system-admin-agent",
        )
        self.collect()
        self.assertEqual(self.reconcile_cli()["actionable"], 0)
        self.assertEqual(self.dispatch().spawned, [])
        self.collect()
        self.assertEqual(self.reconcile_cli()["events"], 0)
        self.assertEqual(self.dispatch().spawned, [])
        self.assertEqual(len(self.spawned), 1)
        self.assertTrue(self.requests)

    def test_source_failure_keeps_cursor_and_pending_routing_retries(self):
        self.collect()
        cursor = self.journal.read()["cursor.json"]
        self.fail_path = "/api/v1/repos/nixos/nix-config/issues/comments"
        self.collect(success=False)
        self.assertEqual(self.journal.read()["cursor.json"], cursor)
        self.assertTrue(self.journal.read()["health.json"]["poll_degraded"])
        self.fail_path = "/api/v1/repos/nixos/nix-config/issues/12"
        self.reconcile_cli(success=False)
        self.assertEqual(len(self.journal.pending()), 1)
        self.assertEqual(self.dispatch().spawned, [])
        self.fail_path = None
        self.assertEqual(self.reconcile_cli()["created"], 1)
        self.assertEqual(len(self.dispatch().spawned), 1)
        self.assertEqual(self.journal.pending(), [])
        self.assertEqual(self.reconcile_cli()["events"], 0)
        self.assertEqual(self.dispatch().spawned, [])

    def test_excluded_issue_and_comment_never_dispatch(self):
        self.source.issue["labels"] = [{"name": "human-only"}]
        self.comment("Manual request")
        self.collect()
        self.assertEqual(self.reconcile_cli()["created"], 0)
        self.assertEqual(self.dispatch().spawned, [])
        self.assertEqual(self.kanban._run(["list", "--json"], expect_json=True), [])
