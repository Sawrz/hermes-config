"""Container producer uses real Git and Nix; no production sources or writes."""

import json
import http.server
import os
import ssl
import subprocess
import sys
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_nix_maintenance as m


class InventoryProducerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(
            os.environ,
            {
                "NIX_REMOTE": "local?root=" + str(self.root / "store"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "master")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "flake.nix").write_text("""{
          outputs = { self }: {
            sourceRepositories.deployment = {
              root = self.outPath; repository = "nixos/nix-config"; revision = self.rev;
            };
            nixosConfigurations.fixture.options.custom.containers.application.images = {
              definitionsWithLocations = [];
              declarations = [ "${self.outPath}/flake.nix" ];
            };
            nixosConfigurations.fixture.config.custom.containers = {
              application = { enable = true; images = {
                main = { url = "registry.example.test/app"; tag = "1.0.0"; };
                database = { url = "docker.io/library/postgres"; tag = "17.2"; };
              }; };
              disabled = { enable = false; images.main = {
                url = "registry.example.test/disabled"; tag = "9.0.0";
              }; };
            };
          };
        }""")
        self.git("add", ".")
        self.git("commit", "-m", "fixture inventory")
        self.target = self.git("rev-parse", "HEAD")

    def test_unsupported_inventory_structure_is_not_empty_success(self):
        (self.repo / "flake.nix").write_text("{ outputs = _: {}; }")
        self.git("add", "flake.nix")
        self.git("commit", "-m", "unsupported fixture")
        with self.assertRaisesRegex(m.WorkflowError, "inventory Nix evaluation failed"):
            m.refresh_container_inventory(self.repo, self.git("rev-parse", "HEAD"))

    def test_wolf_reference_options_preserve_owners_and_optional_images(self):
        (self.repo / "flake.nix").write_text("""{
          outputs = { self }: let
            option = { definitionsWithLocations = [];
              declarations = [ "${self.outPath}/flake.nix" ]; };
          in {
            sourceRepositories.deployment = {
              root = self.outPath; repository = "nixos/nix-config"; revision = self.rev;
            };
            nixosConfigurations.fixture = {
              options.custom.containers.wolf = {
                image = option; uiImage = option; deviceProfileUi = option;
                drop.baseImage = option;
              };
              config.custom.containers.wolf = {
                enable = true;
                image = "registry.example.test:5000/wolf:1.2";
                uiImage = "registry.example.test/ui@sha256:abcdef";
                deviceProfiles = { desktop = {}; };
                deviceProfileUi.sdkImage = "registry.example.test/sdk:8.0";
                drop = { enable = true; baseImage = "registry.example.test/drop:2.0"; };
              };
            };
          };
        }""")
        self.git("add", ".")
        self.git("commit", "-m", "Wolf reference options")
        target = self.git("rev-parse", "HEAD")
        rows = m.refresh_container_inventory(self.repo, target)["components"]
        self.assertEqual(
            [(row["component"], row["image"], row["current_tag"]) for row in rows],
            [
                ("main", "registry.example.test:5000/wolf", "1.2"),
                ("ui", "registry.example.test/ui", "sha256:abcdef"),
                ("ui-sdk", "registry.example.test/sdk", "8.0"),
                ("drop-base", "registry.example.test/drop", "2.0"),
            ],
        )
        for row in rows:
            self.assertEqual(
                row["change_route"],
                {
                    "repository": "nixos/nix-config",
                    "revision": target,
                    "path": "flake.nix",
                },
            )
        source = self.repo / "flake.nix"
        source.write_text(
            source.read_text()
            .replace(
                'deviceProfileUi.sdkImage = "registry.example.test/sdk:8.0";',
                "deviceProfileUi = null;",
            )
            .replace("drop = { enable = true;", "drop = { enable = false;")
        )
        self.git("add", ".")
        self.git("commit", "-m", "Disable optional Wolf images")
        rows = m.refresh_container_inventory(self.repo, self.git("rev-parse", "HEAD"))["components"]
        self.assertEqual([row["component"] for row in rows], ["main", "ui"])

    def registry(self):
        certificate, key = self.root / "cert.pem", self.root / "key.pem"
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
                str(certificate),
            ],
            check=True,
            capture_output=True,
        )
        fixture = self
        self.tags = ["1.0.0", "1.1.0", "2.0.0-rc1"]
        self.api_status = 200

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if fixture.api_status != 200:
                    self.send_error(fixture.api_status)
                    return
                if self.path == "/v2/":
                    data = {}
                elif self.path.startswith("/v2/app/tags/list"):
                    data = {"name": "app", "tags": fixture.tags}
                elif self.path.startswith("/repos/fixture/app/releases"):
                    data = [
                        {"tag_name": "v1.1.0", "prerelease": False, "draft": False},
                        {"tag_name": "v2.0.0-rc1", "prerelease": True, "draft": False},
                    ]
                else:
                    self.send_error(404)
                    return
                encoded = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Docker-Distribution-API-Version", "registry/2.0")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certificate, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(close)
        env = patch.dict(
            os.environ, {"SSL_CERT_FILE": str(certificate), "NIX_SSL_CERT_FILE": str(certificate)}
        )
        env.start()
        self.addCleanup(env.stop)
        image = f"localhost:{server.server_port}/app"
        self.api_base = f"https://localhost:{server.server_port}"
        inventory = {
            "schema_version": 2,
            "repository": "nixos/nix-config",
            "source_commit": self.target,
            "components": [
                {
                    "host": "fixture",
                    "service": "application",
                    "component": "main",
                    "image": image,
                    "current_tag": "1.0.0",
                    "change_route": {
                        "repository": "nixos/nix-containers",
                        "revision": self.target,
                        "path": "modules/application.nix",
                    },
                }
            ],
        }
        feeds = {
            "components": {
                "application.main": {
                    "image": image,
                    "service": "application",
                    "component": "main",
                    "risk": ["stateful"],
                    "update_policy": "separate-pr",
                    "current_tag_semantics": "versioned-release",
                    "source": {
                        "type": "registry_tags",
                        "image": image,
                        "stable_only": True,
                        "tag_regex": r"^(?P<version>\d+\.\d+\.\d+)$",
                    },
                }
            }
        }
        return inventory, feeds

    def test_unreviewed_feed_is_not_applied_as_approved_curation(self):
        inventory, feeds = self.registry()
        feeds["components"]["application.main"]["source"]["status"] = "inferred-unreviewed"
        with self.assertRaisesRegex(m.WorkflowError, "curation"):
            m.discover_container_updates(inventory, feeds)

    def test_github_release_requires_matching_registry_tag_and_preserves_flavour(self):
        inventory, feeds = self.registry()
        inventory["components"][0]["current_tag"] = "1.0.0-apache"
        self.tags = ["1.0.0-apache", "1.1.0-apache", "1.1.0-fpm"]
        feeds["components"]["application.main"]["source"] = {
            "type": "github_releases",
            "repo": "fixture/app",
            "api_base": self.api_base,
            "stable_only": True,
            "tag_regex": r"^v(?P<version>\d+\.\d+\.\d+)$",
        }
        report = m.discover_container_updates(inventory, feeds)
        self.assertEqual(report["updates"][0]["candidate_tag"], "1.1.0-apache")
        self.tags = ["1.0.0-apache", "1.1.0-fpm"]
        with self.assertRaisesRegex(m.WorkflowError, "registry"):
            m.discover_container_updates(inventory, feeds)

    def test_curation_stages_exact_host_versions_and_policies_in_existing_ledger(self):
        self.assertTrue(hasattr(m, "build_container_intent"), "missing real curation application")
        inventory, feeds = self.registry()
        feeds["components"]["application.main"]["notes"] = "Never switch family"
        report = m.discover_container_updates(inventory, feeds)
        report["period"] = "2026-09"
        intent = m.build_container_intent(report)
        ledger = m.IntentLedger(Path(self.tmp.name) / "intent")
        ledger.stage(intent)
        stored = ledger.store.read().documents["intent.json"]["intent"]
        self.assertEqual(stored["decisions"][0]["candidate"]["current_tag"], "1.0.0")
        self.assertEqual(stored["decisions"][0]["candidate"]["candidate_tag"], "1.1.0")
        self.assertEqual(
            stored["decisions"][0]["candidate"]["policy"]["notes"], "Never switch family"
        )
        self.assertEqual(stored["decisions"][0]["service"], "fixture.application.main")
        ledger.stage(intent)
        self.assertEqual(ledger.store.read().documents["intent.json"]["intent"], stored)
        report["updates"] = []
        self.assertIsNone(m.build_container_intent(report))

    def test_curation_rejects_missing_or_wrong_owner_without_replacing_pending_work(self):
        inventory, feeds = self.registry()
        report = m.discover_container_updates(inventory, feeds)
        report["period"] = "2026-09"
        intent = json.loads(json.dumps(m.build_container_intent(report)))
        # A pre-cut pending intent retains its original digest and attempt history.
        intent["decisions"][0]["candidate"].pop("change_route")
        intent["intent_sha256"] = m.digest(
            {key: value for key, value in intent.items() if key != "intent_sha256"}
        )
        ledger = m.IntentLedger(self.root / "intent")
        ledger.stage(intent)
        ledger.begin(intent["intent_sha256"], "existing-attempt")
        ledger.lost_response(intent["intent_sha256"], "response lost before split")
        before = ledger.store.read()
        for route in (
            None,
            {
                "repository": "nixos/nix-config",
                "revision": self.target,
                "path": "flake.nix",
            },
        ):
            changed = json.loads(json.dumps(report))
            if route is None:
                changed["updates"][0].pop("change_route")
            else:
                changed["updates"][0]["change_route"] = route
            with self.assertRaises(m.WorkflowError):
                ledger.stage(m.build_container_intent(changed))
            after = ledger.store.read()
            self.assertEqual(after.generation, before.generation)
            self.assertEqual(after.documents, before.documents)
        self.assertEqual(ledger._read()["intent"]["intent_sha256"], intent["intent_sha256"])
        self.assertEqual(ledger._read()["state"], "ambiguous")

    def test_curation_cli_applies_snapshot_instead_of_only_validating_input(self):
        inventory, feeds = self.registry()
        report = m.discover_container_updates(inventory, feeds)
        report["period"] = "2026-09"
        root = Path(self.tmp.name)
        store = m.RepositoryStore(
            root / "snapshot", protocol="container-inventory", schema_version=1
        )
        store.publish({"report.json": report, "health.json": {"status": "ok"}})
        command = [
            sys.executable,
            m.__file__,
            "container-curation-apply",
            "--snapshot-state-dir",
            str(root / "snapshot"),
            "--state-dir",
            str(root / "intent"),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        ledger = m.IntentLedger(root / "intent")
        self.assertEqual(ledger._read()["state"], "pending")
        before = ledger.store.read().generation
        store.publish({"report.json": report, "health.json": {"status": "error"}})
        failure = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(failure.returncode, 0)
        self.assertEqual(ledger.store.read().generation, before)

    def test_concurrent_staging_cannot_replace_unacknowledged_work(self):
        inventory, feeds = self.registry()
        report = m.discover_container_updates(inventory, feeds)
        intents = []
        for period in ("2026-09", "2026-10"):
            report["period"] = period
            intents.append(m.build_container_intent(report))
        barrier = threading.Barrier(2)
        outcomes = []

        class RacingLedger(m.IntentLedger):
            def _read(self):
                value = super()._read()
                barrier.wait(timeout=5)
                return value

        def stage(intent):
            try:
                RacingLedger(self.root / "concurrent-intents").stage(intent)
                outcomes.append("staged")
            except m.WorkflowError:
                outcomes.append("preserved-pending")

        threads = [threading.Thread(target=stage, args=(intent,)) for intent in intents]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["staged", "preserved-pending"])

    def test_manual_policy_excludes_host_registry_overrides_without_losing_inventory(self):
        inventory, feeds = self.registry()
        original = dict(inventory["components"][0])
        feeds["components"]["application.main"].update(
            image="upstream.example/app", update_policy="manual-only"
        )
        report = m.discover_container_updates(inventory, feeds)
        self.assertEqual(report["updates"], [])
        self.assertEqual(report["inventory"]["components"], [original])

    def test_curation_preserves_mixed_case_component_identity(self):
        inventory, feeds = self.registry()
        report = m.discover_container_updates(inventory, feeds)
        report["period"] = "2026-09"
        report["updates"][0]["component"] = "machineLearning"
        report["inventory"]["components"][0]["component"] = "machineLearning"
        intent = m.build_container_intent(report)
        self.assertEqual(intent["decisions"][0]["candidate"]["component"], "machineLearning")
        self.assertEqual(intent["decisions"][0]["service"], "fixture.application.machineLearning")

    def test_registry_discovery_preserves_curated_family_and_policy(self):
        inventory, feeds = self.registry()
        report = m.discover_container_updates(inventory, feeds)
        self.assertEqual(len(report["updates"]), 1)
        update = report["updates"][0]
        self.assertEqual(update["candidate_tag"], "1.1.0")
        self.assertEqual(update["current_tag"], "1.0.0")
        self.assertEqual(update["policy"], feeds["components"]["application.main"])
        self.assertEqual(update["host"], "fixture")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def test_missing_feed_cannot_be_reported_as_no_update(self):
        self.assertTrue(hasattr(m, "discover_container_updates"), "missing release discovery")
        inventory = m.refresh_container_inventory(self.repo, self.target)
        with self.assertRaisesRegex(m.WorkflowError, "coverage"):
            m.discover_container_updates(inventory, {"schema_version": 1, "components": {}})

    def test_refresh_cli_produces_durable_inventory_without_model_work(self):
        feed_path = self.root / "feeds.json"
        feed_path.write_text(
            json.dumps(
                {
                    "components": {
                        "application.main": {
                            "image": "registry.example.test/app",
                            "source": {"type": "manual"},
                            "update_policy": "manual-only",
                        },
                        "application.database": {
                            "image": "docker.io/library/postgres",
                            "source": {"type": "manual"},
                            "update_policy": "compatibility-only",
                        },
                    }
                }
            )
        )
        origin = "ssh://git@fixture.invalid/nixos/nix-config.git"
        self.git("remote", "add", "origin", origin)
        identity = self.root / "fixture-key"
        identity.write_text("fixture only; never used for network")
        identity.chmod(0o600)
        known = self.root / "fixture-known-hosts"
        known.write_text("fixture.invalid fixture-key-placeholder\n")
        registry_path = self.root / "registry.json"
        registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "jobs": {
                        "nix-config-pull": {
                            "origin": origin,
                            "repository": "nixos/nix-config",
                            "ref": "refs/heads/master",
                            "destination": str(self.repo),
                            "transport": "ssh",
                            "mode": "immutable-reference-cache",
                            "timeout_seconds": 30,
                            "retries": 0,
                            "lock_timeout_seconds": 5,
                            "ssh_host": "fixture.invalid",
                            "ssh_port": 22,
                            "known_hosts_file": str(known),
                            "identity_file": str(identity),
                        }
                    },
                }
            )
        )
        for directory, dirs, files in os.walk(self.repo, topdown=False):
            for name in files + dirs:
                path = Path(directory) / name
                path.chmod(path.stat().st_mode & ~0o222)
            Path(directory).chmod(0o555)
        command = [
            sys.executable,
            m.__file__,
            "container-refresh",
            "--registry",
            str(registry_path),
            "--feeds",
            str(feed_path),
            "--state-dir",
            str(self.root / "snapshot"),
            "--period",
            "2026-09",
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertFalse(result["wakeAgent"])
        store = m.RepositoryStore(
            self.root / "snapshot", protocol="container-inventory", schema_version=1
        )
        report = store.read().documents["report.json"]
        self.assertEqual(report["inventory"]["source_commit"], self.target)
        self.assertEqual(report["updates"], [])
        before = store.read().generation
        repeated = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertFalse(json.loads(repeated.stdout)["changed"])
        self.assertEqual(store.read().generation, before)
        feed_path.write_text(json.dumps({"components": {}}))
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        after = store.read().documents
        self.assertEqual(after["report.json"], report)
        self.assertEqual(after["health.json"]["status"], "error")

    def test_refresh_produces_effective_enabled_components_from_exact_git_target(self):
        self.assertTrue(
            hasattr(m, "refresh_container_inventory"),
            "inventory refresh must produce data, not validate supplied JSON",
        )
        result = m.refresh_container_inventory(self.repo, self.target)
        self.assertEqual(result["source_commit"], self.target)
        self.assertEqual(result["repository"], "nixos/nix-config")
        self.assertEqual(
            [(row["service"], row["component"]) for row in result["components"]],
            [("application", "database"), ("application", "main")],
        )
        main = result["components"][1]
        self.assertEqual(main["host"], "fixture")
        self.assertEqual(main["image"], "registry.example.test/app")
        self.assertEqual(main["current_tag"], "1.0.0")
        self.assertEqual(self.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
