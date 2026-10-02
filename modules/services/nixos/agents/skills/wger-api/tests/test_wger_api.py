import contextlib
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).parents[1] / "scripts" / "wger_api.py"
SPEC = importlib.util.spec_from_file_location("wger_api", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load helper module from {SCRIPT}")
wger_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wger_api)


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    records = []
    requests = []
    fail_once = set()
    delay_write = False
    reconcile_barrier: ClassVar[threading.Barrier | None] = None

    schema = {
        "openapi": "3.0.3",
        "info": {"title": "wger API", "version": "2.6.0"},
        "paths": {
            "/api/v2/weightentry/": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ItemRequest"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "created"}},
                },
            },
            "/api/v2/weightentry/{id}/": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "patch": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/PatchedItemRequest"}
                            }
                        },
                    },
                    "responses": {"200": {"description": "updated"}},
                },
            },
            "/api/v2/userprofile/": {"get": {"responses": {"200": {"description": "ok"}}}},
        },
        "components": {
            "schemas": {
                "ItemRequest": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "calories": {"type": "integer"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "PatchedItemRequest": {"allOf": [{"$ref": "#/components/schemas/ItemRequest"}]},
            }
        },
    }

    def log_message(self, _format, *_args):
        pass

    def _body(self):
        size = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(size) if size else b""

    def _send(self, status, obj=None, headers=None):
        data = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if data:
            with contextlib.suppress(BrokenPipeError):
                self.wfile.write(data)

    def _record(self, body=None):
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )

    def do_GET(self):
        self._record()
        split = urlsplit(self.path)
        query = parse_qs(split.query)
        if split.path == "/api/v2/schema":
            self._send(200, self.__class__.schema)
        elif split.path == "/api/v2/userprofile/":
            self._send(200, {"id": 9, "private": "not-for-logs"})
        elif split.path == "/api/v2/weightentry/":
            if "name" in query:
                found = [r for r in self.__class__.records if r.get("name") == query["name"][0]]
                if self.__class__.reconcile_barrier is not None:
                    try:
                        self.__class__.reconcile_barrier.wait(timeout=1)
                    except threading.BrokenBarrierError:
                        pass
                self._send(
                    200, {"count": len(found), "next": None, "previous": None, "results": found}
                )
            elif query.get("page") == ["2"]:
                self._send(
                    200, {"count": 3, "next": None, "previous": None, "results": [{"id": 3}]}
                )
            else:
                host = self.headers["Host"]
                self._send(
                    200,
                    {
                        "count": 3,
                        "next": f"http://{host}/api/v2/weightentry/?page=2",
                        "previous": None,
                        "results": [{"id": 1}, {"id": 2}],
                    },
                )
        elif split.path.startswith("/api/v2/weightentry/"):
            try:
                item_id = int(split.path.rstrip("/").split("/")[-1])
                record = next(r for r in self.__class__.records if r["id"] == item_id)
            except (ValueError, StopIteration):
                self._send(404, {"detail": "not found"})
            else:
                self._send(200, record)
        elif split.path == "/api/v2/safe-redirect/":
            self._send(302, headers={"Location": "/api/v2/userprofile/"})
        elif split.path == "/api/v2/evil-redirect/":
            self._send(302, headers={"Location": "https://evil.example/api/v2/userprofile/"})
        elif split.path == "/api/v2/outside-redirect/":
            self._send(302, headers={"Location": "/admin/"})
        elif split.path == "/api/v2/retry/":
            if "retry" not in self.__class__.fail_once:
                self.__class__.fail_once.add("retry")
                self._send(503, {"detail": "temporary"})
            else:
                self._send(200, {"ok": True})
        elif split.path == "/api/v2/auth/":
            self._send(401, {"detail": "bad token"})
        elif split.path == "/api/v2/forbidden/":
            self._send(403, {"detail": "denied"})
        elif split.path == "/api/v2/rate/":
            self._send(429, {"detail": "slow down"}, {"Retry-After": "0"})
        elif split.path == "/api/v2/server/":
            self._send(500, {"detail": "broken"})
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self):
        raw = self._body()
        payload = json.loads(raw or b"{}")
        self._record(payload)
        if self.path == "/api/v2/evil-redirect/":
            self._send(307, headers={"Location": "https://evil.example/api/v2/weightentry/"})
        elif self.path == "/api/v2/weightentry/":
            record = {"id": len(self.__class__.records) + 1, **payload}
            self.__class__.records.append(record)
            if self.__class__.delay_write:
                time.sleep(0.25)
            self._send(201, record, {"Location": f"/api/v2/weightentry/{record['id']}/"})
        else:
            self._send(404, {"detail": "not found"})

    def do_PATCH(self):
        raw = self._body()
        payload = json.loads(raw or b"{}")
        self._record(payload)
        item_id = int(self.path.rstrip("/").split("/")[-1])
        record = next(r for r in self.__class__.records if r["id"] == item_id)
        record.update(payload)
        self._send(200, record)


class WgerApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/api/v2"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        FixtureHandler.records = []
        FixtureHandler.requests = []
        FixtureHandler.fail_once = set()
        FixtureHandler.delay_write = False
        FixtureHandler.reconcile_barrier = None
        FixtureHandler.schema = json.loads(json.dumps(FixtureHandler.schema))
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.endpoint = root / "endpoint"
        self.credential = root / "credential"
        self.endpoint.write_text(self.base + "\n")
        self.credential.write_text("synthetic-secret\n")
        os.chmod(self.endpoint, 0o400)
        os.chmod(self.credential, 0o400)

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, **kwargs):
        return wger_api.WgerClient(
            endpoint_file=self.endpoint,
            credential_file=self.credential,
            timeout=kwargs.pop("timeout", 1),
            backoff=0,
            **kwargs,
        )

    def test_runtime_files_are_canonical_and_token_is_header_only(self):
        result = self.client().get_json("/userprofile/")
        self.assertEqual(result["id"], 9)
        self.assertEqual(FixtureHandler.requests[-1]["authorization"], "Token synthetic-secret")
        self.assertNotIn("synthetic-secret", FixtureHandler.requests[-1]["path"])

    def test_runtime_file_mode_symlink_empty_and_environment_fallback_fail_closed(self):
        os.chmod(self.credential, 0o600)
        with self.assertRaisesRegex(wger_api.RuntimeContractError, "0400"):
            self.client()
        os.chmod(self.credential, 0o600)
        self.credential.write_text("")
        os.chmod(self.credential, 0o400)
        with self.assertRaisesRegex(wger_api.RuntimeContractError, "empty"):
            self.client()
        self.credential.unlink()
        self.credential.symlink_to(self.endpoint)
        with self.assertRaisesRegex(wger_api.RuntimeContractError, "regular file"):
            self.client()
        os.environ["WGER_API_TOKEN"] = "must-not-be-used"
        try:
            self.credential.unlink()
            with self.assertRaises(wger_api.RuntimeContractError):
                self.client()
        finally:
            os.environ.pop("WGER_API_TOKEN")

    def test_endpoint_must_be_api_v2_and_https_or_loopback(self):
        for endpoint in ["https://example.test", "http://example.test/api/v2", "file:///api/v2"]:
            os.chmod(self.endpoint, 0o600)
            self.endpoint.write_text(endpoint)
            os.chmod(self.endpoint, 0o400)
            with self.subTest(endpoint=endpoint), self.assertRaises(wger_api.RuntimeContractError):
                self.client()

    def test_schema_check_accepts_exact_operation_and_version(self):
        schema = self.client().check_schema("/weightentry/", "POST", expected_version="2.6.0")
        self.assertEqual(schema["openapi"], "3.0.3")
        schema_request = next(
            request
            for request in FixtureHandler.requests
            if request["path"].startswith("/api/v2/schema")
        )
        self.assertIsNone(schema_request["authorization"])

    def test_schema_and_version_drift_are_typed(self):
        FixtureHandler.schema["openapi"] = "2.0"
        with self.assertRaises(wger_api.SchemaDriftError):
            self.client().check_schema("/weightentry/", "GET")
        FixtureHandler.schema["openapi"] = "3.0.3"
        with self.assertRaises(wger_api.VersionDriftError):
            self.client().check_schema("/weightentry/", "GET", expected_version="2.7.0")
        with self.assertRaises(wger_api.SchemaDriftError):
            self.client().check_schema("/missing/", "GET")

    def test_preview_validates_live_request_fields_required_values_and_types(self):
        client = self.client()
        invalid = [
            {},
            {"calories": 500},
            {"name": "lunch", "unknown": True},
            {"name": "lunch", "calories": "five hundred"},
            {"name": "lunch", "calories": True},
        ]
        for payload in invalid:
            with (
                self.subTest(payload=payload),
                self.assertRaises((wger_api.SchemaDriftError, wger_api.UnsupportedOperationError)),
            ):
                client.preview("POST", "/weightentry/", payload)

    def test_pagination_consumes_all_pages_and_honors_cap(self):
        self.assertEqual([r["id"] for r in self.client().list_all("/weightentry/")], [1, 2, 3])
        with self.assertRaises(wger_api.PaginationError):
            self.client().list_all("/weightentry/", cap=2)

    def test_pagination_rejects_cross_origin_loop_and_malformed_shape(self):
        client = self.client()
        with self.assertRaises(wger_api.OriginViolationError):
            client._validate_next("https://evil.example/api/v2/weightentry/")
        with self.assertRaises(wger_api.PaginationError):
            client._consume_page({"results": []}, 0, 10)

    def test_redirects_are_same_origin_and_api_root_only(self):
        self.assertEqual(self.client().get_json("/safe-redirect/")["id"], 9)
        with self.assertRaises(wger_api.OriginViolationError):
            self.client().get_json("/evil-redirect/")
        with self.assertRaises(wger_api.OriginViolationError):
            self.client().get_json("/outside-redirect/")

    def test_percent_encoded_or_dot_segment_paths_cannot_escape_api_root(self):
        client = self.client()
        attacks = (
            "/%2e%2e/admin",
            "/%252e%252e/admin",
            "/weightentry/%2fadmin",
            "/weightentry/../admin",
            "/weightentry\\..\\admin",
        )
        for path in attacks:
            with self.subTest(path=path), self.assertRaises(wger_api.OriginViolationError):
                client._url(path)

    def test_typed_http_errors_and_bounded_retry(self):
        self.assertTrue(self.client(max_retries=1).get_json("/retry/")["ok"])
        cases = [
            ("/auth/", wger_api.AuthenticationError),
            ("/forbidden/", wger_api.AuthorizationError),
            ("/rate/", wger_api.RateLimitError),
            ("/server/", wger_api.ServerError),
            ("/missing/", wger_api.NotFoundError),
        ]
        for path, error in cases:
            with self.subTest(path=path), self.assertRaises(error):
                self.client(max_retries=0).get_json(path)

    def test_mutation_requires_schema_preview_and_exact_confirmation(self):
        client = self.client()
        reconcile = {
            "path": "/weightentry/",
            "query": {"name": "lunch"},
            "match": {"name": "lunch"},
        }
        preview = client.preview(
            "POST",
            "/weightentry/",
            {"name": "lunch", "calories": 500},
            reconcile=reconcile,
        )
        self.assertRegex(preview["confirmation"], r"^APPLY POST /weightentry/ SHA256 [0-9a-f]{64}$")
        self.assertEqual(FixtureHandler.records, [])
        with self.assertRaises(wger_api.ConfirmationRequiredError):
            client.mutate(
                "POST",
                "/weightentry/",
                {"name": "lunch"},
                confirm="yes",
                reconcile=reconcile,
            )

    def test_confirmation_is_bound_to_the_exact_payload(self):
        client = self.client()
        reconcile = {
            "path": "/weightentry/",
            "query": {"name": "lunch"},
            "match": {"name": "lunch"},
        }
        first = client.preview(
            "POST", "/weightentry/", {"name": "lunch", "calories": 500}, reconcile=reconcile
        )
        changed = client.preview(
            "POST", "/weightentry/", {"name": "lunch", "calories": 501}, reconcile=reconcile
        )
        self.assertNotEqual(first["confirmation"], changed["confirmation"])
        with self.assertRaises(wger_api.ConfirmationRequiredError):
            client.mutate(
                "POST",
                "/weightentry/",
                {"name": "lunch", "calories": 501},
                confirm=first["confirmation"],
                reconcile=reconcile,
            )

    def test_post_requires_a_canonical_payload_bound_reconciliation_contract(self):
        client = self.client()
        payload = {"name": "lunch", "calories": 500}
        invalid = (
            None,
            {},
            {"path": "/weightentry/", "query": {}, "match": {"name": "lunch"}},
            {
                "path": "/weightentry/1/",
                "query": {"name": "lunch"},
                "match": {"name": "lunch"},
            },
            {
                "path": "/weightentry/",
                "query": {"name": "lunch"},
                "match": {"name": "dinner"},
            },
            {
                "path": "/weightentry/",
                "query": {"name": "dinner"},
                "match": {"name": "lunch"},
            },
            {
                "path": "/weightentry/",
                "query": {"name": ["lunch"]},
                "match": {"name": "lunch"},
            },
            {
                "path": "/weightentry/",
                "query": {"name": "lunch", "calories": 999},
                "match": {"name": "lunch"},
            },
        )
        for reconcile in invalid:
            with (
                self.subTest(reconcile=reconcile),
                self.assertRaises(wger_api.UnsupportedOperationError),
            ):
                client.preview("POST", "/weightentry/", payload, reconcile=reconcile)

        patch = client.preview("PATCH", "/weightentry/1/", {"name": "lunch"})
        self.assertIsNone(patch["reconcile"])

    def test_post_without_contract_is_rejected_before_dispatch_after_response_loss(self):
        client = self.client()
        with mock.patch.object(client, "_request") as request:
            with self.assertRaises(wger_api.UnsupportedOperationError):
                client.mutate(
                    "POST",
                    "/weightentry/",
                    {"name": "lunch"},
                    confirm="irrelevant",
                )
        request.assert_not_called()

    def test_cli_parser_requires_reconcile_file_for_post_only(self):
        parser = wger_api.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["preview", "POST", "/weightentry/", "--payload-file", "payload.json"]
            )
        parsed = parser.parse_args(
            ["preview", "PATCH", "/weightentry/1/", "--payload-file", "payload.json"]
        )
        self.assertIsNone(parsed.reconcile_file)

    def test_confirmation_is_bound_to_the_reconciliation_contract(self):
        client = self.client()
        payload = {"name": "lunch", "calories": 500}
        approved = {
            "path": "/weightentry/",
            "query": {"name": "lunch"},
            "match": {"name": "lunch"},
        }
        changed = {
            "path": "/weightentry/",
            "query": {"calories": 500},
            "match": {"calories": 500},
        }
        confirmation = client.preview("POST", "/weightentry/", payload, reconcile=approved)[
            "confirmation"
        ]

        with self.assertRaises(wger_api.ConfirmationRequiredError):
            client.mutate(
                "POST",
                "/weightentry/",
                payload,
                confirm=confirmation,
                reconcile=changed,
            )

    def test_mutation_uses_the_exact_approved_snapshots_after_caller_changes(self):
        client = self.client()
        payload = {"name": "lunch", "calories": 500}
        reconcile = {
            "path": "/weightentry/",
            "query": {"name": "lunch"},
            "match": {"name": "lunch"},
        }
        confirmation = client.preview("POST", "/weightentry/", payload, reconcile=reconcile)[
            "confirmation"
        ]
        original_preview = client.preview

        def preview_then_change_caller(*args, **kwargs):
            approved = original_preview(*args, **kwargs)
            payload.clear()
            payload.update({"name": "dinner", "calories": 900})
            reconcile["query"]["name"] = "dinner"
            reconcile["match"]["name"] = "dinner"
            return approved

        with mock.patch.object(client, "preview", side_effect=preview_then_change_caller):
            result = client.mutate(
                "POST",
                "/weightentry/",
                payload,
                confirm=confirmation,
                reconcile=reconcile,
            )

        self.assertEqual(result, {"id": 1, "name": "lunch", "calories": 500})
        self.assertEqual(FixtureHandler.records, [result])

    def test_location_readback_is_reduced_to_canonical_api_relative_path(self):
        client = self.client()
        location = client.endpoint + "/weightentry/7/"
        self.assertEqual(
            client._readback_path("POST", "/weightentry/", {}, {"Location": location}),
            "/weightentry/7/",
        )
        with self.assertRaises(wger_api.OriginViolationError):
            client._readback_path(
                "POST", "/weightentry/", {}, {"Location": location + "?unexpected=1"}
            )
        with self.assertRaises(wger_api.OriginViolationError):
            client._readback_path(
                "POST",
                "/weightentry/",
                {},
                {"Location": client.endpoint + "/api/v2/weightentry/7/"},
            )

    def test_post_read_before_write_and_mandatory_readback(self):
        client = self.client()
        payload = {"name": "lunch", "calories": 500}
        reconcile = {
            "path": "/weightentry/",
            "query": {"name": "lunch"},
            "match": {"name": "lunch"},
        }
        confirmation = client.preview("POST", "/weightentry/", payload, reconcile=reconcile)[
            "confirmation"
        ]
        first = client.mutate(
            "POST",
            "/weightentry/",
            payload,
            confirm=confirmation,
            reconcile=reconcile,
        )
        second = client.mutate(
            "POST",
            "/weightentry/",
            payload,
            confirm=confirmation,
            reconcile=reconcile,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(FixtureHandler.records), 1)
        self.assertTrue(
            any(
                r["method"] == "GET" and f"/weightentry/{first['id']}/" in r["path"]
                for r in FixtureHandler.requests
            )
        )

    def test_concurrent_clients_serialize_reconciliation_and_post(self):
        payload = {"name": "lunch", "calories": 500}
        reconcile = {
            "path": "/weightentry/",
            "query": {"name": "lunch"},
            "match": {"name": "lunch"},
        }
        confirmation = self.client().preview("POST", "/weightentry/", payload, reconcile=reconcile)[
            "confirmation"
        ]
        FixtureHandler.reconcile_barrier = threading.Barrier(2)
        start = threading.Barrier(3)
        results = []
        errors = []

        def create():
            client = self.client()
            start.wait()
            try:
                results.append(
                    client.mutate(
                        "POST",
                        "/weightentry/",
                        payload,
                        confirm=confirmation,
                        reconcile=reconcile,
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        workers = [threading.Thread(target=create) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(timeout=5)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual({result["id"] for result in results}, {1})
        self.assertEqual(FixtureHandler.records, [{"id": 1, **payload}])

    def test_patch_reads_back_and_verifies_requested_fields(self):
        FixtureHandler.records = [{"id": 1, "name": "old"}]
        client = self.client()
        payload = {"name": "new"}
        result = client.mutate(
            "PATCH",
            "/weightentry/1/",
            payload,
            confirm=client.preview("PATCH", "/weightentry/1/", payload)["confirmation"],
        )
        self.assertEqual(result["name"], "new")
        self.assertEqual(FixtureHandler.requests[-1]["method"], "GET")

    def test_successful_dispatch_with_failed_readback_is_indeterminate(self):
        client = self.client()
        payload = {"name": "new"}
        preview = client.preview("PATCH", "/weightentry/1/", payload)
        with (
            mock.patch.object(client, "preview", return_value=preview),
            mock.patch.object(
                client,
                "_request",
                return_value=(200, {"id": 1, "name": "new"}, {}),
            ),
            mock.patch.object(
                client,
                "get_json",
                side_effect=wger_api.ConnectivityError("readback unavailable"),
            ),
            self.assertRaises(wger_api.IndeterminateWriteError),
        ):
            client.mutate(
                "PATCH",
                "/weightentry/1/",
                payload,
                confirm=preview["confirmation"],
            )

    def test_ambiguous_post_reconciles_or_stays_indeterminate(self):
        FixtureHandler.delay_write = True
        client = self.client(timeout=0.05, max_retries=0)
        dinner = {"name": "dinner"}
        reconcile = {
            "path": "/weightentry/",
            "query": {"name": "dinner"},
            "match": {"name": "dinner"},
        }
        confirmation = client.preview("POST", "/weightentry/", dinner, reconcile=reconcile)[
            "confirmation"
        ]
        result = client.mutate(
            "POST",
            "/weightentry/",
            dinner,
            confirm=confirmation,
            reconcile=reconcile,
        )
        self.assertEqual(result["name"], "dinner")
        snack = {"name": "snack"}
        with self.assertRaises(wger_api.UnsupportedOperationError):
            client.mutate(
                "POST",
                "/weightentry/",
                snack,
                confirm="irrelevant",
            )

    def test_unreviewed_write_resource_is_rejected(self):
        with self.assertRaises(wger_api.UnsupportedOperationError):
            self.client().preview(
                "POST",
                "/unreviewed-resource/",
                {"name": "unsafe"},
            )

    def test_delete_and_unsafe_write_redirects_are_rejected(self):
        with self.assertRaises(wger_api.UnsupportedOperationError):
            self.client().mutate(
                "DELETE", "/weightentry/1/", {}, confirm="APPLY DELETE /weightentry/1/"
            )
        with self.assertRaises(wger_api.OriginViolationError):
            self.client()._request("POST", "/evil-redirect/", payload={"name": "x"})

    def test_preflight_returns_status_without_private_body(self):
        self.assertEqual(self.client().preflight(), {"status": 200, "authenticated": True})


if __name__ == "__main__":
    unittest.main()
