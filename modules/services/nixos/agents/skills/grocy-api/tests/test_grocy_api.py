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
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).parents[1] / "scripts" / "grocy_api.py"
SPEC = importlib.util.spec_from_file_location("grocy_api", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load helper module from {SCRIPT}")
grocy_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(grocy_api)


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    products = []
    stock_amount = 0
    requests = []
    fail_once = set()
    product_post_delay = 0.0
    schema = {
        "openapi": "3.1.0",
        "info": {"title": "Grocy REST API", "version": "4.6.0"},
        "paths": {
            "/user": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/objects/{entity}": {
                "get": {
                    "parameters": [
                        {"name": "limit", "in": "query"},
                        {"name": "offset", "in": "query"},
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Product~1Mutation"}
                            }
                        }
                    },
                    "responses": {"200": {"description": "created"}},
                },
            },
            "/objects/{entity}/{objectId}": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "put": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Product~1Mutation"}
                            }
                        }
                    },
                    "responses": {"204": {"description": "updated"}},
                },
            },
            "/stock": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/stock/products/{productId}/add": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"amount": {"type": "number"}},
                                    "required": ["amount"],
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/stock/shoppinglist/add-product": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "product_id": {"type": "integer"},
                                        "amount": {"type": "number"},
                                        "shopping_list_id": {"type": "integer"},
                                    },
                                    "required": ["product_id", "amount"],
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
        "components": {
            "schemas": {
                "Product/Mutation": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": "number"},
                        "tags": {"type": "array", "items": {"type": "integer"}},
                        "qu_id_purchase": {"type": "integer"},
                        "qu_id_stock": {"type": "integer"},
                    },
                }
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

    def _send_raw(self, status, data, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError):
            self.wfile.write(data)

    def _record(self, body=None):
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "key": self.headers.get("GROCY-API-KEY"),
                "body": body,
            }
        )

    def do_GET(self):
        self._record()
        split = urlsplit(self.path)
        query = parse_qs(split.query)
        if split.path == "/api/openapi/specification":
            self._send(200, self.__class__.schema)
        elif split.path == "/api/user":
            self._send(200, {"id": 3, "username": "private"})
        elif split.path == "/api/objects/products":
            values = self.__class__.products or [{"id": i, "name": f"p{i}"} for i in range(1, 6)]
            filters = query.get("query[]", [])
            for condition in filters:
                if "=" in condition:
                    key, value = condition.split("=", 1)
                    values = [r for r in values if str(r.get(key)) == value]
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", [str(len(values) or 1)])[0])
            self._send(200, values[offset : offset + limit])
        elif split.path.startswith("/api/objects/products/"):
            item_id = int(split.path.rsplit("/", 1)[-1])
            item = next((r for r in self.__class__.products if r["id"] == item_id), None)
            self._send(200, item) if item else self._send(404, {"error_message": "missing"})
        elif split.path == "/api/stock":
            self._send(200, [{"product_id": 1, "amount": self.__class__.stock_amount}])
        elif split.path == "/api/retry":
            if "retry" not in self.__class__.fail_once:
                self.__class__.fail_once.add("retry")
                self._send(503, {"error_message": "temporary"})
            else:
                self._send(200, {"ok": True})
        elif split.path == "/api/evil-redirect":
            self._send(302, headers={"Location": "https://evil.example/api/stock"})
        elif split.path == "/api/encoded-traversal":
            self._send(302, headers={"Location": "/api/%252e%252e/admin"})
        elif split.path == "/api/auth":
            self._send(401, {"error_message": "bad key"})
        elif split.path == "/api/forbidden":
            self._send(403, {"error_message": "denied"})
        else:
            self._send(404, {"error_message": "missing"})

    def do_POST(self):
        payload = json.loads(self._body() or b"{}")
        self._record(payload)
        if self.path == "/api/objects/products":
            time.sleep(self.__class__.product_post_delay)
            record = {"id": len(self.__class__.products) + 1, **payload}
            self.__class__.products.append(record)
            if payload["name"] == "Beans":
                time.sleep(0.2)
                self._send(200, {"created_object_id": record["id"]})
            elif payload["name"] == "Committed":
                self._send(500, {"error_message": "failed after commit"})
            elif payload["name"] == "Redirected":
                self._send(
                    302,
                    {"error_message": "moved after commit"},
                    {"Location": f"/api/objects/products/{record['id']}"},
                )
            elif payload["name"] == "Malformed":
                self._send_raw(200, b"not-json")
            else:
                self._send(200, {"created_object_id": record["id"]})
        elif self.path == "/api/stock/products/1/add":
            self.__class__.stock_amount += payload["amount"]
            self._send(200, {"ok": True, **payload})
        elif self.path == "/api/stock/shoppinglist/add-product":
            self._send(200, {"ok": True, **payload})
        else:
            self._send(404, {"error_message": "missing"})

    def do_PUT(self):
        payload = json.loads(self._body() or b"{}")
        self._record(payload)
        item_id = int(self.path.rsplit("/", 1)[-1])
        record = next(r for r in self.__class__.products if r["id"] == item_id)
        record.update(payload)
        self._send(204)


class GrocyApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/api"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        FixtureHandler.products = []
        FixtureHandler.stock_amount = 0
        FixtureHandler.requests = []
        FixtureHandler.fail_once = set()
        FixtureHandler.product_post_delay = 0.0
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.endpoint = root / "endpoint"
        self.credential = root / "credential"
        self.lock_root = root / "locks"
        self.endpoint.write_text(self.base + "\n")
        self.credential.write_text("synthetic-key\n")
        os.chmod(self.endpoint, 0o400)
        os.chmod(self.credential, 0o400)

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, **kwargs):
        return grocy_api.GrocyClient(
            endpoint_file=self.endpoint,
            credential_file=self.credential,
            timeout=kwargs.pop("timeout", 1),
            backoff=0,
            lock_root=self.lock_root,
            **kwargs,
        )

    def test_runtime_files_are_file_only_and_key_is_header_only(self):
        self.assertEqual(self.client().preflight(), {"status": 200, "authenticated": True})
        self.assertEqual(FixtureHandler.requests[-1]["key"], "synthetic-key")
        self.assertNotIn("synthetic-key", FixtureHandler.requests[-1]["path"])
        os.environ["GROCY_API_TOKEN"] = "must-not-be-used"
        try:
            self.credential.unlink()
            with self.assertRaises(grocy_api.RuntimeContractError):
                self.client()
        finally:
            os.environ.pop("GROCY_API_TOKEN")

    def test_runtime_contract_rejects_bad_mode_symlink_and_noncanonical_endpoint(self):
        os.chmod(self.credential, 0o600)
        with self.assertRaises(grocy_api.RuntimeContractError):
            self.client()
        self.credential.unlink()
        self.credential.symlink_to(self.endpoint)
        with self.assertRaises(grocy_api.RuntimeContractError):
            self.client()
        self.credential.unlink()
        self.credential.write_text("x")
        os.chmod(self.credential, 0o400)
        os.chmod(self.endpoint, 0o600)
        self.endpoint.write_text("https://example.test/not-api")
        os.chmod(self.endpoint, 0o400)
        with self.assertRaises(grocy_api.RuntimeContractError):
            self.client()

    def test_live_schema_version_and_exact_operation_gate(self):
        schema = self.client().check_schema("POST", "/objects/products", expected_version="4.6.0")
        self.assertEqual(schema["openapi"], "3.1.0")
        with self.assertRaises(grocy_api.VersionDriftError):
            self.client().check_schema("GET", "/stock", expected_version="old")
        with self.assertRaises(grocy_api.SchemaDriftError):
            self.client().check_schema("POST", "/missing")

    def test_live_request_schema_validates_declared_top_level_fields(self):
        client = self.client()
        path = "/objects/products/1"
        for payload in (
            {"tags": [1]},
            {"name": 7},
            {"name": "Rice", "quantity": []},
            {"name": "Rice", "unknown": True},
        ):
            with self.subTest(payload=payload), self.assertRaises(grocy_api.ValidationError):
                client.preview("PUT", path, payload, authority="grocy:pantry")
        preview = client.preview(
            "PUT",
            path,
            {"name": "Rice", "quantity": 2, "tags": [1]},
            authority="grocy:pantry",
        )
        self.assertEqual(preview["payload"]["quantity"], 2)

    def test_bounded_schema_rejects_unsupported_nested_shapes_before_confirmation(self):
        client = self.client()
        original_schema = FixtureHandler.schema
        cases = {
            "malformed-array-items": (
                {"type": "array", "items": "not-a-schema"},
                [],
                grocy_api.SchemaDriftError,
            ),
            "array-item-type-mismatch": (
                {"type": "array", "items": {"type": "integer"}},
                ["not-an-integer"],
                grocy_api.ValidationError,
            ),
            "nested-object-contract": (
                {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
                {"count": "not-an-integer"},
                grocy_api.SchemaDriftError,
            ),
        }
        try:
            for name, (field_schema, value, error) in cases.items():
                schema = json.loads(json.dumps(original_schema))
                schema["components"]["schemas"]["Product/Mutation"]["properties"]["probe"] = (
                    field_schema
                )
                FixtureHandler.schema = schema
                with self.subTest(name=name), self.assertRaises(error):
                    client.preview(
                        "PUT",
                        "/objects/products/1",
                        {"name": "Rice", "probe": value},
                        authority="grocy:pantry",
                    )
            self.assertFalse(
                any(
                    request["method"] in {"POST", "PUT", "PATCH"}
                    for request in FixtureHandler.requests
                )
            )
        finally:
            FixtureHandler.schema = original_schema

    def test_bounded_schema_rejects_unsupported_request_root_keywords_before_confirmation(self):
        client = self.client()
        original_schema = FixtureHandler.schema
        try:
            schema = json.loads(json.dumps(original_schema))
            schema["components"]["schemas"]["Product/Mutation"]["minProperties"] = 2
            FixtureHandler.schema = schema
            with self.assertRaises(grocy_api.SchemaDriftError):
                client.preview(
                    "POST",
                    "/objects/products",
                    {"name": "Rice"},
                    authority="grocy:pantry",
                    reconcile={
                        "path": "/objects/products",
                        "query": {"query[]": ["name=Rice"]},
                        "match": {"name": "Rice"},
                    },
                )
            self.assertFalse(
                any(
                    request["method"] in {"POST", "PUT", "PATCH"}
                    for request in FixtureHandler.requests
                )
            )
        finally:
            FixtureHandler.schema = original_schema

    def test_composed_request_schema_and_ref_siblings_fail_closed(self):
        client = self.client()
        base = json.loads(json.dumps(FixtureHandler.schema))
        operation = base["paths"]["/objects/{entity}/{objectId}"]["put"]
        media = operation["requestBody"]["content"]["application/json"]
        cases = {
            "impossible-all-of": {"allOf": [{"type": "object"}, {"type": "string"}]},
            "unmatched-any-of": {"anyOf": [{"type": "string"}, {"type": "array"}]},
            "ambiguous-one-of": {"oneOf": [{"type": "object"}, {"required": ["name"]}]},
            "ref-sibling-conflict": {
                "$ref": "#/components/schemas/Product~1Mutation",
                "type": "string",
            },
        }
        for name, request_schema in cases.items():
            schema = json.loads(json.dumps(base))
            schema["paths"]["/objects/{entity}/{objectId}"]["put"]["requestBody"]["content"][
                "application/json"
            ]["schema"] = request_schema
            with self.subTest(name=name), self.assertRaises(grocy_api.SchemaDriftError):
                client._validate_payload_schema(
                    schema, "PUT", "/objects/products/1", {"name": "Rice"}
                )
        media["schema"] = {"$ref": "#/components/schemas/Missing"}
        with self.assertRaises(grocy_api.SchemaDriftError):
            client._validate_payload_schema(base, "PUT", "/objects/products/1", {"name": "Rice"})
        self.assertFalse(hasattr(client, "_value_matches_schema"))

    def test_offset_pagination_and_cap(self):
        values = self.client().list_all("/objects/products", page_size=2, cap=5)
        self.assertEqual([v["id"] for v in values], [1, 2, 3, 4, 5])
        with self.assertRaises(grocy_api.PaginationError):
            self.client().list_all("/objects/products", page_size=2, cap=4)
        self.assertEqual(self.client().list_all("/stock", cap=1)[0]["product_id"], 1)

    def test_redirects_typed_errors_and_bounded_read_retry(self):
        client = self.client(max_retries=1)
        self.assertTrue(client.get_json("/retry")["ok"])
        with self.assertRaises(grocy_api.OriginViolationError):
            client.get_json("/evil-redirect")
        with self.assertRaises(grocy_api.OriginViolationError):
            client.get_json("/encoded-traversal")
        with self.assertRaises(grocy_api.OriginViolationError):
            client.get_json("/%2e%2e/admin")
        with self.assertRaises(grocy_api.AuthenticationError):
            self.client(max_retries=0).get_json("/auth")
        with self.assertRaises(grocy_api.AuthorizationError):
            self.client(max_retries=0).get_json("/forbidden")

    def test_mutation_freezes_approved_data_before_waiting_for_lock(self):
        client = self.client()
        payload = {"name": "Approved"}
        reconcile = {
            "path": "/objects/products",
            "query": {"query[]": ["name=Approved"]},
            "match": {"name": "Approved"},
        }
        confirmation = client.preview(
            "POST",
            "/objects/products",
            payload,
            authority="grocy:pantry",
            reconcile=reconcile,
        )["confirmation"]
        blocked = threading.Event()
        results = []
        errors = []
        original_flock = grocy_api.fcntl.flock

        def traced_flock(fd, operation):
            if (
                operation == grocy_api.fcntl.LOCK_EX
                and threading.current_thread() is not threading.main_thread()
            ):
                blocked.set()
            return original_flock(fd, operation)

        def apply():
            try:
                results.append(
                    client.mutate(
                        "POST",
                        "/objects/products",
                        payload,
                        authority="grocy:pantry",
                        confirm=confirmation,
                        reconcile=reconcile,
                    )
                )
            except Exception as exc:  # noqa: BLE001  # thread result is asserted below
                errors.append(exc)

        worker = threading.Thread(target=apply)
        with client._mutation_lock("POST", "/objects/products", reconcile):
            grocy_api.fcntl.flock = traced_flock
            try:
                worker.start()
                self.assertTrue(blocked.wait(timeout=2), "worker did not block on mutation lock")
                payload["name"] = "UNAPPROVED"
                reconcile["query"]["query[]"] = ["name=UNAPPROVED"]
                reconcile["match"]["name"] = "UNAPPROVED"
            finally:
                grocy_api.fcntl.flock = original_flock
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(results[0]["name"], "Approved")
        posts = [request for request in FixtureHandler.requests if request["method"] == "POST"]
        self.assertEqual([request["body"] for request in posts], [{"name": "Approved"}])

    def test_product_duplicate_detection_preview_confirmation_and_readback(self):
        client = self.client()
        payload = {"name": "Rice", "qu_id_purchase": 1, "qu_id_stock": 1}
        reconcile = {
            "path": "/objects/products",
            "query": {"query[]": ["name=Rice"]},
            "match": payload,
        }
        preview = client.preview(
            "POST", "/objects/products", payload, authority="grocy:pantry", reconcile=reconcile
        )
        self.assertRegex(
            preview["confirmation"],
            r"^APPLY GROCY:PANTRY POST /objects/products VERSION 4\.6\.0 SHA256 [0-9a-f]{64}$",
        )
        with self.assertRaises(grocy_api.ConfirmationRequiredError):
            client.mutate(
                "POST",
                "/objects/products",
                payload,
                authority="grocy:pantry",
                confirm="yes",
                reconcile=reconcile,
            )
        first = client.mutate(
            "POST",
            "/objects/products",
            payload,
            authority="grocy:pantry",
            confirm=preview["confirmation"],
            reconcile=reconcile,
        )
        second = client.mutate(
            "POST",
            "/objects/products",
            payload,
            authority="grocy:pantry",
            confirm=preview["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(FixtureHandler.products), 1)

    def test_mismatched_product_filter_cannot_duplicate_sequential_replay(self):
        client = self.client()
        payload = {"name": "Stable"}
        reconcile = {
            "path": "/objects/products",
            "query": {"query[]": ["name=Never matches"]},
            "match": payload,
        }
        confirmation = client.preview(
            "POST",
            "/objects/products",
            payload,
            authority="grocy:pantry",
            reconcile=reconcile,
        )["confirmation"]
        first = client.mutate(
            "POST",
            "/objects/products",
            payload,
            authority="grocy:pantry",
            confirm=confirmation,
            reconcile=reconcile,
        )
        second = client.mutate(
            "POST",
            "/objects/products",
            payload,
            authority="grocy:pantry",
            confirm=confirmation,
            reconcile=reconcile,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(FixtureHandler.products), 1)

    def test_product_match_variants_share_stable_mutation_lock(self):
        FixtureHandler.product_post_delay = 0.1
        payload = {"name": "Concurrent", "qu_id_purchase": 1, "qu_id_stock": 1}
        reconciles = [
            {
                "path": "/objects/products",
                "query": {"query[]": ["name=Concurrent"]},
                "match": {"name": "Concurrent", "qu_id_purchase": 1, "qu_id_stock": 1},
            },
            {
                "path": "/objects/products",
                "query": {"query[]": ["name=Concurrent", "qu_id_stock=1"]},
                "match": dict(payload),
            },
        ]
        clients = [self.client(), self.client()]
        confirmations = [
            client.preview(
                "POST",
                "/objects/products",
                payload,
                authority="grocy:pantry",
                reconcile=reconcile,
            )["confirmation"]
            for client, reconcile in zip(clients, reconciles)
        ]
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def apply(index):
            try:
                barrier.wait(timeout=2)
                results.append(
                    clients[index].mutate(
                        "POST",
                        "/objects/products",
                        payload,
                        authority="grocy:pantry",
                        confirm=confirmations[index],
                        reconcile=reconciles[index],
                    )
                )
            except Exception as exc:  # noqa: BLE001  # thread result is asserted below
                errors.append(exc)

        threads = [threading.Thread(target=apply, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual(len(FixtureHandler.products), 1)
        self.assertEqual({result["id"] for result in results}, {1})

    def test_update_and_stock_write_require_readback_contract(self):
        FixtureHandler.products = [{"id": 1, "name": "Rice"}]
        client = self.client()
        payload = {"name": "Brown Rice"}
        updated = client.mutate(
            "PUT",
            "/objects/products/1",
            payload,
            authority="grocy:pantry",
            confirm=client.preview("PUT", "/objects/products/1", payload, authority="grocy:pantry")[
                "confirmation"
            ],
        )
        self.assertEqual(updated["name"], "Brown Rice")
        with self.assertRaises(grocy_api.UnsupportedOperationError):
            payload = {"amount": 2}
            client.preview("POST", "/stock/products/1/add", payload, authority="grocy:pantry")
        payload = {"amount": 2}
        reconcile = {
            "path": "/stock",
            "query": {"query[]": ["product_id=1"]},
            "before": {"product_id": 1, "amount": 0},
            "after": {"product_id": 1, "amount": 2},
        }
        stock = client.mutate(
            "POST",
            "/stock/products/1/add",
            payload,
            authority="grocy:pantry",
            confirm=client.preview(
                "POST",
                "/stock/products/1/add",
                payload,
                authority="grocy:pantry",
                reconcile=reconcile,
            )["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(stock["amount"], 2)
        self.assertEqual(FixtureHandler.stock_amount, 2)
        repeat = client.mutate(
            "POST",
            "/stock/products/1/add",
            payload,
            authority="grocy:pantry",
            confirm=client.preview(
                "POST",
                "/stock/products/1/add",
                payload,
                authority="grocy:pantry",
                reconcile=reconcile,
            )["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(repeat["amount"], 2)
        self.assertEqual(FixtureHandler.stock_amount, 2)
        with self.assertRaises(grocy_api.UnsupportedOperationError):
            client.preview(
                "POST",
                "/stock/products/1/add",
                payload,
                authority="grocy:pantry",
                reconcile={"path": "/stock", "query": {}, "match": {"product_id": 1, "amount": 2}},
            )

    def test_indeterminate_write_reconciles_once_or_stays_indeterminate(self):
        client = self.client(timeout=0.05, max_retries=0)
        beans = {"name": "Beans"}
        reconcile = {
            "path": "/objects/products",
            "query": {"query[]": ["name=Beans"]},
            "match": {"name": "Beans"},
        }
        result = client.mutate(
            "POST",
            "/objects/products",
            beans,
            authority="grocy:pantry",
            confirm=client.preview(
                "POST", "/objects/products", beans, authority="grocy:pantry", reconcile=reconcile
            )["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(result["name"], "Beans")
        client = self.client(max_retries=0)
        committed = {"name": "Committed"}
        reconcile = {
            "path": "/objects/products",
            "query": {"query[]": ["name=Committed"]},
            "match": {"name": "Committed"},
        }
        result = client.mutate(
            "POST",
            "/objects/products",
            committed,
            authority="grocy:pantry",
            confirm=client.preview(
                "POST",
                "/objects/products",
                committed,
                authority="grocy:pantry",
                reconcile=reconcile,
            )["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(result["name"], "Committed")
        for name in ("Redirected", "Malformed"):
            payload = {"name": name}
            reconcile = {
                "path": "/objects/products",
                "query": {"query[]": [f"name={name}"]},
                "match": payload,
            }
            with self.subTest(name=name):
                result = client.mutate(
                    "POST",
                    "/objects/products",
                    payload,
                    authority="grocy:pantry",
                    confirm=client.preview(
                        "POST",
                        "/objects/products",
                        payload,
                        authority="grocy:pantry",
                        reconcile=reconcile,
                    )["confirmation"],
                    reconcile=reconcile,
                )
                self.assertEqual(result["name"], name)
        before = len(FixtureHandler.products)
        with self.assertRaises(grocy_api.UnsupportedOperationError):
            unknown = {"name": "Unknown"}
            client.preview("POST", "/objects/products", unknown, authority="grocy:pantry")
        self.assertEqual(len(FixtureHandler.products), before)

    def test_authority_and_delete_fail_closed(self):
        with self.assertRaises(grocy_api.AuthorityError):
            self.client().preview(
                "POST", "/objects/products", {"name": "x"}, authority="mealie:pantry"
            )
        with self.assertRaises(grocy_api.UnsupportedOperationError):
            self.client().mutate(
                "DELETE", "/objects/products/1", {}, authority="grocy:pantry", confirm="x"
            )

    def test_schema_scope_confirmation_payload_and_reconciliation_are_bound(self):
        client = self.client()
        with self.assertRaises(grocy_api.UnsupportedOperationError):
            client.preview("POST", "/objects/users", {"name": "x"}, authority="grocy:pantry")
        approved = {"name": "Approved"}
        changed = {"name": "Changed"}
        approved_reconcile = {"path": "/objects/products", "query": {}, "match": approved}
        changed_reconcile = {"path": "/objects/products", "query": {}, "match": changed}
        confirmation = client.preview(
            "POST",
            "/objects/products",
            approved,
            authority="grocy:pantry",
            reconcile=approved_reconcile,
        )["confirmation"]
        with self.assertRaises(grocy_api.ConfirmationRequiredError):
            client.mutate(
                "POST",
                "/objects/products",
                changed,
                authority="grocy:pantry",
                confirm=confirmation,
                reconcile=changed_reconcile,
            )
        with self.assertRaises(grocy_api.UnsupportedOperationError):
            client.preview(
                "POST",
                "/objects/products",
                approved,
                authority="grocy:pantry",
                reconcile={"path": "/stock", "query": {}, "match": {"name": "Approved"}},
            )
        with self.assertRaises(grocy_api.UnsupportedOperationError):
            client.preview(
                "POST",
                "/objects/products",
                {"name": "Full", "qu_id_stock": 1},
                authority="grocy:pantry",
                reconcile={"path": "/objects/products", "query": {}, "match": {"name": "Full"}},
            )

    def test_null_is_not_missing_and_post_dispatch_readback_failure_is_indeterminate(self):
        client = self.client(max_retries=0)
        with self.assertRaises(grocy_api.ReadbackError):
            client._verify_readback({}, {"requested_null": None})
        self.assertEqual(client._exact([{}], {"requested_null": None}), [])
        payload = {"name": "Readback Down"}
        reconcile = {"path": "/objects/products", "query": {}, "match": payload}
        confirmation = client.preview(
            "POST", "/objects/products", payload, authority="grocy:pantry", reconcile=reconcile
        )["confirmation"]
        original_request = client._request

        def request_then_break_readback(*args, **kwargs):
            result = original_request(*args, **kwargs)
            if str(args[0]).upper() in {"POST", "PUT", "PATCH"}:
                client.get_json = lambda *_a, **_k: (_ for _ in ()).throw(
                    grocy_api.ConnectivityError("down")
                )
            return result

        client._request = request_then_break_readback
        with self.assertRaises(grocy_api.IndeterminateWriteError):
            client.mutate(
                "POST",
                "/objects/products",
                payload,
                authority="grocy:pantry",
                confirm=confirmation,
                reconcile=reconcile,
            )


if __name__ == "__main__":
    unittest.main()
