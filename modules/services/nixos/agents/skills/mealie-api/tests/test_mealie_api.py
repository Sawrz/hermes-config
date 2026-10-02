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

SCRIPT = Path(__file__).parents[1] / "scripts" / "mealie_api.py"
SPEC = importlib.util.spec_from_file_location("mealie_api", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load helper module from {SCRIPT}")
mealie_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mealie_api)


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    recipes = []
    mealplans = []
    shopping_lists = []
    shopping_items = []
    requests = []
    fail_once = set()
    recipe_post_delay = 0.0
    shopping_post_delay = 0.0
    schema = {
        "openapi": "3.1.0",
        "info": {"title": "Mealie", "version": "3.24.0"},
        "paths": {
            "/api/users/self": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/api/recipes": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Recipe"}}
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                },
            },
            "/api/recipes/{slug}": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "put": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Recipe"}}
                        }
                    },
                    "responses": {"200": {"description": "updated"}},
                },
            },
            "/api/recipes/create/url": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"url": {"type": "string"}},
                                    "required": ["url"],
                                }
                            }
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                }
            },
            "/api/households/mealplans": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "date": {"type": "string"},
                                        "entryType": {"type": "string"},
                                        "title": {"type": "string"},
                                    },
                                    "required": ["date", "entryType", "title"],
                                }
                            }
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                },
            },
            "/api/households/shopping/lists": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                    "required": ["name"],
                                }
                            }
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                },
            },
            "/api/households/shopping/items": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "shopping_list_id": {"type": "string"},
                                        "note": {"type": "string"},
                                    },
                                    "required": ["shopping_list_id", "note"],
                                }
                            }
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                },
            },
            "/api/households/shopping/lists/{list_id}": {
                "put": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ShoppingListUpdate"}
                            }
                        }
                    },
                    "responses": {"200": {"description": "updated"}},
                }
            },
        },
        "components": {
            "schemas": {
                "Recipe": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "slug": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["name"],
                },
                "ShoppingListUpdate": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "group_id": {"type": "string"},
                        "user_id": {"type": "string"},
                        "list_items": {"type": "array"},
                        "name": {"type": "string"},
                    },
                    "required": ["id", "group_id", "user_id", "list_items", "name"],
                },
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
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )

    def do_GET(self):
        self._record()
        split = urlsplit(self.path)
        query = parse_qs(split.query)
        if split.path == "/openapi.json":
            self._send(200, self.__class__.schema)
        elif split.path == "/api/users/self":
            self._send(200, {"id": "private", "admin": False})
        elif split.path == "/api/recipes":
            if query.get("queryFilter"):
                needle = query["queryFilter"][0].split('"')[1]
                found = [r for r in self.__class__.recipes if r.get("name") == needle]
                self._page(found, query, "/recipes")
            else:
                self._page(
                    list(self.__class__.recipes)
                    or [{"slug": "one"}, {"slug": "two"}, {"slug": "three"}],
                    query,
                    "/recipes",
                )
        elif split.path == "/api/households/mealplans":
            self._page(list(self.__class__.mealplans), query, "/households/mealplans")
        elif split.path == "/api/households/shopping/lists":
            self._page(
                list(self.__class__.shopping_lists),
                query,
                "/households/shopping/lists",
            )
        elif split.path == "/api/households/shopping/items":
            self._page(
                list(self.__class__.shopping_items),
                query,
                "/households/shopping/items",
            )
        elif split.path.startswith("/api/recipes/"):
            slug = split.path.rsplit("/", 1)[-1]
            found = next((r for r in self.__class__.recipes if r.get("slug") == slug), None)
            self._send(200, found) if found else self._send(404, {"detail": "missing"})
        elif split.path == "/api/retry":
            if "retry" not in self.__class__.fail_once:
                self.__class__.fail_once.add("retry")
                self._send(503, {"detail": "temporary"})
            else:
                self._send(200, {"ok": True})
        elif split.path == "/api/evil-redirect":
            self._send(302, headers={"Location": "https://evil.example/api/recipes"})
        elif split.path == "/api/encoded-traversal":
            self._send(302, headers={"Location": "/api/%252e%252e/admin"})
        elif split.path == "/api/auth":
            self._send(401, {"detail": "bad token"})
        elif split.path == "/api/forbidden":
            self._send(403, {"detail": "denied"})
        else:
            self._send(404, {"detail": "missing"})

    def _page(self, records, query, collection):
        page = int(query.get("page", ["1"])[0])
        values = records[(page - 1) * 2 : page * 2]
        total_pages = max(1, (len(records) + 1) // 2)
        next_path = f"{collection}?page={page + 1}&perPage=2" if page < total_pages else None
        self._send(
            200,
            {
                "page": page,
                "per_page": 2,
                "total": len(records),
                "total_pages": total_pages,
                "items": values,
                "next": next_path,
                "previous": None,
            },
        )

    def do_POST(self):
        payload = json.loads(self._body() or b"{}")
        self._record(payload)
        if self.path == "/api/recipes":
            time.sleep(self.__class__.recipe_post_delay)
            slug = payload.get("slug") or payload["name"].lower().replace(" ", "-")
            record = {"slug": slug, **payload}
            self.__class__.recipes.append(record)
            if payload["name"] == "Late":
                time.sleep(0.2)
                self._send(201, record)
            elif payload.get("name") == "Committed":
                self._send(500, {"detail": "failed after commit"})
            elif payload.get("name") == "Redirected":
                self._send(
                    302, {"detail": "moved after commit"}, {"Location": f"/api/recipes/{slug}"}
                )
            elif payload.get("name") == "Malformed":
                self._send_raw(200, b"not-json")
            else:
                self._send(201, record, {"Location": f"/api/recipes/{slug}"})
        elif self.path == "/api/recipes/create/url":
            slug = "imported-recipe"
            self.__class__.recipes.append({"slug": slug, "name": "Imported Recipe"})
            self._send(201, slug)
        elif self.path == "/api/households/mealplans":
            record = {"id": len(self.__class__.mealplans) + 1, **payload}
            self.__class__.mealplans.append(record)
            self._send(201, record)
        elif self.path == "/api/households/shopping/lists":
            time.sleep(self.__class__.shopping_post_delay)
            record = {"id": len(self.__class__.shopping_lists) + 1, **payload}
            self.__class__.shopping_lists.append(record)
            self._send(201, record)
        else:
            self._send(404, {"detail": "missing"})

    def do_PUT(self):
        payload = json.loads(self._body() or b"{}")
        self._record(payload)
        slug = self.path.rsplit("/", 1)[-1]
        record = next(r for r in self.__class__.recipes if r["slug"] == slug)
        record.update(payload)
        self._send(200, record)


class MealieApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        FixtureHandler.recipes = []
        FixtureHandler.mealplans = []
        FixtureHandler.shopping_lists = []
        FixtureHandler.shopping_items = []
        FixtureHandler.requests = []
        FixtureHandler.fail_once = set()
        FixtureHandler.recipe_post_delay = 0.0
        FixtureHandler.shopping_post_delay = 0.0
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.endpoint = root / "endpoint"
        self.credential = root / "credential"
        self.lock_root = root / "locks"
        self.endpoint.write_text(self.base + "\n")
        self.credential.write_text("synthetic-token\n")
        os.chmod(self.endpoint, 0o400)
        os.chmod(self.credential, 0o400)

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, **kwargs):
        return mealie_api.MealieClient(
            endpoint_file=self.endpoint,
            credential_file=self.credential,
            timeout=kwargs.pop("timeout", 1),
            backoff=0,
            lock_root=self.lock_root,
            **kwargs,
        )

    def test_runtime_files_are_file_only_and_bearer_header_only(self):
        result = self.client().preflight()
        self.assertEqual(result, {"status": 200, "authenticated": True})
        self.assertEqual(FixtureHandler.requests[-1]["authorization"], "Bearer synthetic-token")
        self.assertNotIn("synthetic-token", FixtureHandler.requests[-1]["path"])
        os.environ["MEALIE_API_TOKEN"] = "must-not-be-used"
        try:
            self.credential.unlink()
            with self.assertRaises(mealie_api.RuntimeContractError):
                self.client()
        finally:
            os.environ.pop("MEALIE_API_TOKEN")

    def test_runtime_contract_rejects_bad_mode_symlink_and_non_https_remote(self):
        os.chmod(self.credential, 0o600)
        with self.assertRaises(mealie_api.RuntimeContractError):
            self.client()
        self.credential.unlink()
        self.credential.symlink_to(self.endpoint)
        with self.assertRaises(mealie_api.RuntimeContractError):
            self.client()
        self.credential.unlink()
        self.credential.write_text("x")
        os.chmod(self.credential, 0o400)
        os.chmod(self.endpoint, 0o600)
        self.endpoint.write_text("http://example.test")
        os.chmod(self.endpoint, 0o400)
        with self.assertRaises(mealie_api.RuntimeContractError):
            self.client()

    def test_live_schema_version_and_exact_operation_gate(self):
        schema = self.client().check_schema("GET", "/api/recipes", expected_version="3.24.0")
        self.assertEqual(schema["openapi"], "3.1.0")
        with self.assertRaises(mealie_api.VersionDriftError):
            self.client().check_schema("GET", "/api/recipes", expected_version="old")
        with self.assertRaises(mealie_api.SchemaDriftError):
            self.client().check_schema("POST", "/api/missing")

    def test_shopping_items_fixture_starts_empty(self):
        self.assertEqual(self.client().list_all("/api/households/shopping/items"), [])

    def test_pagination_consumes_relative_next_and_enforces_cap(self):
        values = self.client().list_all("/api/recipes", cap=3)
        self.assertEqual([v["slug"] for v in values], ["one", "two", "three"])
        with self.assertRaises(mealie_api.PaginationError):
            self.client().list_all("/api/recipes", cap=2)

    def test_origin_safe_redirect_typed_errors_and_bounded_read_retry(self):
        client = self.client(max_retries=1)
        self.assertTrue(client.get_json("/api/retry")["ok"])
        with self.assertRaises(mealie_api.OriginViolationError):
            client.get_json("/api/evil-redirect")
        with self.assertRaises(mealie_api.OriginViolationError):
            client.get_json("/api/encoded-traversal")
        with self.assertRaises(mealie_api.OriginViolationError):
            client.get_json("/api/%2e%2e/admin")
        with self.assertRaises(mealie_api.AuthenticationError):
            self.client(max_retries=0).get_json("/api/auth")
        with self.assertRaises(mealie_api.AuthorizationError):
            self.client(max_retries=0).get_json("/api/forbidden")

    def test_mutation_freezes_approved_data_before_waiting_for_post_lock(self):
        client = self.client()
        payload = {"name": "Approved"}
        reconcile = {
            "path": "/api/recipes",
            "query": {"queryFilter": 'name = "Approved"'},
            "match": {"name": "Approved"},
        }
        confirmation = client.preview(
            "POST",
            "/api/recipes",
            payload,
            authority="mealie:recipes",
            reconcile=reconcile,
        )["confirmation"]
        blocked = threading.Event()
        results = []
        errors = []
        original_flock = mealie_api.fcntl.flock

        def traced_flock(fd, operation):
            if (
                operation == mealie_api.fcntl.LOCK_EX
                and threading.current_thread() is not threading.main_thread()
            ):
                blocked.set()
            return original_flock(fd, operation)

        def apply():
            try:
                results.append(
                    client.mutate(
                        "POST",
                        "/api/recipes",
                        payload,
                        authority="mealie:recipes",
                        confirm=confirmation,
                        reconcile=reconcile,
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        worker = threading.Thread(target=apply)
        with client._mutation_lock("/api/recipes", reconcile):
            mealie_api.fcntl.flock = traced_flock
            try:
                worker.start()
                self.assertTrue(
                    blocked.wait(timeout=2),
                    "worker did not block on the mutation lock",
                )
                payload["name"] = "UNAPPROVED"
                reconcile["query"]["queryFilter"] = 'name = "UNAPPROVED"'
                reconcile["match"]["name"] = "UNAPPROVED"
            finally:
                mealie_api.fcntl.flock = original_flock
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(results[0]["name"], "Approved")
        posts = [request for request in FixtureHandler.requests if request["method"] == "POST"]
        self.assertEqual([request["body"] for request in posts], [{"name": "Approved"}])

    def test_mismatched_reconciliation_filter_cannot_duplicate_sequential_replay(self):
        client = self.client()
        payload = {"name": "Duplicate"}
        reconcile = {
            "path": "/api/recipes",
            "query": {"queryFilter": 'name = "Never matches"'},
            "match": {"name": "Duplicate"},
        }
        confirmation = client.preview(
            "POST",
            "/api/recipes",
            payload,
            authority="mealie:recipes",
            reconcile=reconcile,
        )["confirmation"]

        first = client.mutate(
            "POST",
            "/api/recipes",
            payload,
            authority="mealie:recipes",
            confirm=confirmation,
            reconcile=reconcile,
        )
        second = client.mutate(
            "POST",
            "/api/recipes",
            payload,
            authority="mealie:recipes",
            confirm=confirmation,
            reconcile=reconcile,
        )

        self.assertEqual(first["slug"], second["slug"])
        self.assertEqual(len(FixtureHandler.recipes), 1)

    def test_reconciliation_identity_must_match_recipe_payload(self):
        payload = {"name": "Sequential duplicate"}
        reconcile = {
            "path": "/api/recipes",
            "query": {},
            "match": {"name": "Never created"},
        }
        with self.assertRaises(mealie_api.UnsupportedOperationError):
            self.client().preview(
                "POST",
                "/api/recipes",
                payload,
                authority="mealie:recipes",
                reconcile=reconcile,
            )
        self.assertEqual(FixtureHandler.recipes, [])

    def test_recipe_reconciliation_match_variants_share_one_lock_identity(self):
        FixtureHandler.recipe_post_delay = 0.1
        payload = {"name": "Concurrent exact duplicate", "description": "same"}
        reconciles = [
            {
                "path": "/api/recipes",
                "query": {},
                "match": {"name": payload["name"]},
            },
            {
                "path": "/api/recipes",
                "query": {"queryFilter": 'name = "Concurrent exact duplicate"'},
                "match": dict(payload),
            },
        ]
        clients = [self.client(), self.client()]
        confirmations = [
            client.preview(
                "POST",
                "/api/recipes",
                payload,
                authority="mealie:recipes",
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
                        "/api/recipes",
                        payload,
                        authority="mealie:recipes",
                        confirm=confirmations[index],
                        reconcile=reconciles[index],
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=apply, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual(len(FixtureHandler.recipes), 1)
        self.assertEqual({result["slug"] for result in results}, {"concurrent-exact-duplicate"})

    def test_recipe_naming_duplicate_detection_preview_confirmation_and_readback(self):
        client = self.client()
        payload = {"name": "Family Soup", "description": "good"}
        reconcile = {
            "path": "/api/recipes",
            "query": {"queryFilter": 'name = "Family Soup"'},
            "match": {"name": "Family Soup"},
        }
        preview = client.preview(
            "POST", "/api/recipes", payload, authority="mealie:recipes", reconcile=reconcile
        )
        self.assertRegex(
            preview["confirmation"],
            r"^APPLY MEALIE:RECIPES POST /api/recipes VERSION 3\.24\.0 SHA256 [0-9a-f]{64}$",
        )
        self.assertEqual(FixtureHandler.recipes, [])
        with self.assertRaises(mealie_api.ConfirmationRequiredError):
            client.mutate(
                "POST",
                "/api/recipes",
                payload,
                authority="mealie:recipes",
                confirm="yes",
                reconcile=reconcile,
            )
        first = client.mutate(
            "POST",
            "/api/recipes",
            payload,
            authority="mealie:recipes",
            confirm=preview["confirmation"],
            reconcile=reconcile,
        )
        second = client.mutate(
            "POST",
            "/api/recipes",
            payload,
            authority="mealie:recipes",
            confirm=preview["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(first["slug"], second["slug"])
        self.assertEqual(len(FixtureHandler.recipes), 1)

    def test_url_import_is_rejected_without_durable_source_identity(self):
        payload = {"url": "https://example.test/recipe"}
        reconcile = {
            "path": "/api/recipes",
            "query": {"queryFilter": 'name = "Imported Recipe"'},
            "match": {"name": "Imported Recipe"},
        }
        with self.assertRaises(mealie_api.UnsupportedOperationError):
            self.client().preview(
                "POST",
                "/api/recipes/create/url",
                payload,
                authority="mealie:recipes",
                reconcile=reconcile,
            )

    def test_meal_plan_write_uses_separate_domain_authority_and_collection(self):
        client = self.client()
        payload = {"date": "2026-08-28", "entryType": "dinner", "title": "Soup"}
        reconcile = {"path": "/api/households/mealplans", "query": {}, "match": payload}
        preview = client.preview(
            "POST",
            "/api/households/mealplans",
            payload,
            authority="mealie:meal_plans",
            reconcile=reconcile,
        )
        result = client.mutate(
            "POST",
            "/api/households/mealplans",
            payload,
            authority="mealie:meal_plans",
            confirm=preview["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(result["title"], "Soup")
        with self.assertRaises(mealie_api.AuthorityError):
            client.preview(
                "POST",
                "/api/households/mealplans",
                payload,
                authority="mealie:recipes",
                reconcile=reconcile,
            )

    def test_shopping_write_requires_contract_supplied_mealie_authority(self):
        payload = {"name": "Weekly shopping"}
        reconcile = {
            "path": "/api/households/shopping/lists",
            "query": {},
            "match": payload,
        }
        preview = self.client().preview(
            "POST",
            "/api/households/shopping/lists",
            payload,
            authority="mealie:shopping",
            reconcile=reconcile,
        )
        self.assertEqual(preview["domain"], "shopping")
        self.assertRegex(
            preview["confirmation"],
            r"^APPLY MEALIE:SHOPPING POST /api/households/shopping/lists ",
        )
        result = self.client().mutate(
            "POST",
            "/api/households/shopping/lists",
            payload,
            authority="mealie:shopping",
            confirm=preview["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(result["name"], "Weekly shopping")
        self.assertEqual(len(FixtureHandler.shopping_lists), 1)
        with self.assertRaises(mealie_api.AuthorityError):
            self.client().preview(
                "POST",
                "/api/households/shopping/lists",
                payload,
                authority="grocy:shopping",
                reconcile=reconcile,
            )

    def test_shopping_duplicate_detection_stays_on_shopping_pages(self):
        FixtureHandler.shopping_lists = [
            {"id": "list-1", "name": "First"},
            {"id": "list-2", "name": "Second"},
            {"id": "list-3", "name": "Weekly shopping"},
        ]
        payload = {"name": "Weekly shopping"}
        reconcile = {
            "path": "/api/households/shopping/lists",
            "query": {"perPage": 2},
            "match": payload,
        }
        client = self.client()
        confirmation = client.preview(
            "POST",
            "/api/households/shopping/lists",
            payload,
            authority="mealie:shopping",
            reconcile=reconcile,
        )["confirmation"]
        result = client.mutate(
            "POST",
            "/api/households/shopping/lists",
            payload,
            authority="mealie:shopping",
            confirm=confirmation,
            reconcile=reconcile,
        )
        self.assertEqual(result["id"], "list-3")
        self.assertEqual(len(FixtureHandler.shopping_lists), 3)

    def test_concurrent_identical_shopping_posts_are_serialized(self):
        FixtureHandler.shopping_post_delay = 0.1
        payload = {"name": "Concurrent shopping"}
        reconciles = [
            {
                "path": "/api/households/shopping/lists",
                "query": {},
                "match": payload,
            },
            {
                "path": "/api/households/shopping/lists",
                "query": {"page": 1},
                "match": payload,
            },
        ]
        confirmations = [
            self.client().preview(
                "POST",
                "/api/households/shopping/lists",
                payload,
                authority="mealie:shopping",
                reconcile=reconcile,
            )["confirmation"]
            for reconcile in reconciles
        ]
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def apply(index):
            try:
                client = self.client()
                barrier.wait(timeout=2)
                results.append(
                    client.mutate(
                        "POST",
                        "/api/households/shopping/lists",
                        payload,
                        authority="mealie:shopping",
                        confirm=confirmations[index],
                        reconcile=reconciles[index],
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=apply, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(FixtureHandler.shopping_lists), 1)
        self.assertEqual({result["id"] for result in results}, {1})

    def test_shopping_put_requires_live_schema_payload(self):
        client = self.client()
        path = "/api/households/shopping/lists/list-1"
        with self.assertRaises(mealie_api.ValidationError):
            client.preview(
                "PUT",
                path,
                {"name": "Incomplete"},
                authority="mealie:shopping",
                expected_version="3.24.0",
            )
        complete = {
            "id": "list-1",
            "group_id": "group-1",
            "user_id": "user-1",
            "list_items": [],
            "name": "Complete",
        }
        with self.assertRaises(mealie_api.ValidationError):
            client.preview(
                "PUT",
                path,
                {key: value for key, value in complete.items() if key != "name"},
                authority="mealie:shopping",
                expected_version="3.24.0",
            )
        with self.assertRaises(mealie_api.ValidationError):
            client.preview(
                "PUT",
                path,
                {**complete, "list_items": "not-an-array"},
                authority="mealie:shopping",
                expected_version="3.24.0",
            )
        preview = client.preview(
            "PUT",
            path,
            complete,
            authority="mealie:shopping",
            expected_version="3.24.0",
        )
        self.assertEqual(preview["payload"], complete)

    def test_payload_contract_is_limited_to_declared_top_level_fields(self):
        client = self.client()
        schema = json.loads(json.dumps(FixtureHandler.schema))
        with self.assertRaises(mealie_api.ValidationError):
            client._validate_payload_schema(
                schema, "POST", "/api/recipes", {"name": "Soup", "unknown": True}
            )
        with self.assertRaises(mealie_api.ValidationError):
            client._validate_payload_schema(schema, "POST", "/api/recipes", {"name": 7})

    def test_bounded_schema_rejects_unsupported_nested_shapes_before_confirmation(self):
        client = self.client()
        original_schema = FixtureHandler.schema
        cases = {
            "malformed-array-items": (
                {"type": "array", "items": "not-a-schema"},
                [],
                mealie_api.SchemaDriftError,
            ),
            "array-item-type-mismatch": (
                {"type": "array", "items": {"type": "integer"}},
                ["not-an-integer"],
                mealie_api.ValidationError,
            ),
            "nested-object-contract": (
                {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
                {"count": "not-an-integer"},
                mealie_api.SchemaDriftError,
            ),
        }
        try:
            for name, (field_schema, value, error) in cases.items():
                schema = json.loads(json.dumps(original_schema))
                schema["components"]["schemas"]["Recipe"]["properties"]["probe"] = field_schema
                FixtureHandler.schema = schema
                with self.subTest(name=name), self.assertRaises(error):
                    client.preview(
                        "PUT",
                        "/api/recipes/example",
                        {"name": "Soup", "probe": value},
                        authority="mealie:recipes",
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
            schema["components"]["schemas"]["Recipe"]["minProperties"] = 2
            FixtureHandler.schema = schema
            with self.assertRaises(mealie_api.SchemaDriftError):
                client.preview(
                    "POST",
                    "/api/recipes",
                    {"name": "Soup"},
                    authority="mealie:recipes",
                    reconcile={
                        "path": "/api/recipes",
                        "query": {"queryFilter": 'name = "Soup"'},
                        "match": {"name": "Soup"},
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

    def test_recursive_local_ref_schema_is_rejected_as_outside_bounded_contract(self):
        client = self.client()
        schema = json.loads(json.dumps(FixtureHandler.schema))
        schema["components"]["schemas"]["RecursiveRecipe"] = {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "children": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/RecursiveRecipe"},
                },
            },
        }
        schema["paths"]["/api/recipes"]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ] = {"$ref": "#/components/schemas/RecursiveRecipe"}

        for payload in ({"name": "root"}, {"name": "root", "children": [{"name": "leaf"}]}):
            with self.subTest(payload=payload), self.assertRaises(mealie_api.SchemaDriftError):
                client._validate_payload_schema(
                    schema,
                    "POST",
                    "/api/recipes",
                    payload,
                )
        self.assertFalse(hasattr(client, "_value_matches_schema"))

    def test_request_schema_root_and_composed_types_are_enforced(self):
        client = self.client()

        cases = {
            "root-string": {"type": "string"},
            "impossible-all-of": {
                "allOf": [
                    {"type": "object"},
                    {"type": "string"},
                ]
            },
            "ref-with-object-siblings": {
                "$ref": "#/components/schemas/StringRequest",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        }
        for name, request_schema in cases.items():
            schema = json.loads(json.dumps(FixtureHandler.schema))
            schema["components"]["schemas"]["StringRequest"] = {"type": "string"}
            schema["paths"]["/api/recipes"]["post"]["requestBody"]["content"]["application/json"][
                "schema"
            ] = request_schema
            with self.subTest(name=name), self.assertRaises(mealie_api.SchemaDriftError):
                client._validate_payload_schema(
                    schema,
                    "POST",
                    "/api/recipes",
                    {"name": "must remain an object"},
                )

    def test_malformed_request_schema_keywords_fail_closed(self):
        client = self.client()
        cases = {
            "explicit-null-type": {"type": None},
            "duplicate-type-union": {"type": ["object", "object"]},
            "duplicate-required-fields": {
                "type": "object",
                "required": ["name", "name"],
            },
            "explicit-null-ref": {"$ref": None},
            "non-schema-array-items": {"type": "array", "items": "not-a-schema"},
            "all-of-explicit-null-type": {
                "allOf": [
                    {"type": "object"},
                    {"type": None},
                ]
            },
        }
        for name, request_schema in cases.items():
            schema = json.loads(json.dumps(FixtureHandler.schema))
            schema["paths"]["/api/recipes"]["post"]["requestBody"]["content"]["application/json"][
                "schema"
            ] = request_schema
            with self.subTest(name=name), self.assertRaises(mealie_api.SchemaDriftError):
                client._validate_payload_schema(
                    schema,
                    "POST",
                    "/api/recipes",
                    {"name": "object payload"},
                )

    def test_indeterminate_write_reconciles_once_or_stays_indeterminate(self):
        client = self.client(timeout=0.05, max_retries=0)
        late = {"name": "Late"}
        reconcile = {
            "path": "/api/recipes",
            "query": {"queryFilter": 'name = "Late"'},
            "match": {"name": "Late"},
        }
        result = client.mutate(
            "POST",
            "/api/recipes",
            late,
            authority="mealie:recipes",
            confirm=client.preview(
                "POST", "/api/recipes", late, authority="mealie:recipes", reconcile=reconcile
            )["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(result["slug"], "late")
        client = self.client(max_retries=0)
        committed = {"name": "Committed"}
        reconcile = {
            "path": "/api/recipes",
            "query": {"queryFilter": 'name = "Committed"'},
            "match": {"name": "Committed"},
        }
        result = client.mutate(
            "POST",
            "/api/recipes",
            committed,
            authority="mealie:recipes",
            confirm=client.preview(
                "POST", "/api/recipes", committed, authority="mealie:recipes", reconcile=reconcile
            )["confirmation"],
            reconcile=reconcile,
        )
        self.assertEqual(result["slug"], "committed")
        for name in ("Redirected", "Malformed"):
            payload = {"name": name}
            reconcile = {
                "path": "/api/recipes",
                "query": {"queryFilter": f'name = "{name}"'},
                "match": payload,
            }
            with self.subTest(name=name):
                result = client.mutate(
                    "POST",
                    "/api/recipes",
                    payload,
                    authority="mealie:recipes",
                    confirm=client.preview(
                        "POST",
                        "/api/recipes",
                        payload,
                        authority="mealie:recipes",
                        reconcile=reconcile,
                    )["confirmation"],
                    reconcile=reconcile,
                )
                self.assertEqual(result["name"], name)
        before = len(FixtureHandler.recipes)
        with self.assertRaises(mealie_api.UnsupportedOperationError):
            unknown = {"name": "Unknown"}
            client.preview("POST", "/api/recipes", unknown, authority="mealie:recipes")
        self.assertEqual(len(FixtureHandler.recipes), before)

    def test_readback_requires_every_requested_field(self):
        with self.assertRaises(mealie_api.ReadbackError):
            self.client()._verify_readback(
                {"name": "Soup"}, {"name": "Soup", "description": "required"}
            )
        with self.assertRaises(mealie_api.ReadbackError):
            self.client()._verify_readback({}, {"requested_null": None})

    def test_post_dispatch_readback_and_reconciliation_failures_stay_indeterminate(self):
        client = self.client(max_retries=0)
        payload = {"name": "Readback Down"}
        reconcile = {"path": "/api/recipes", "query": {}, "match": payload}
        confirmation = client.preview(
            "POST", "/api/recipes", payload, authority="mealie:recipes", reconcile=reconcile
        )["confirmation"]
        original_request = client._request

        def request_then_break_readback(*args, **kwargs):
            result = original_request(*args, **kwargs)
            if str(args[0]).upper() in {"POST", "PUT", "PATCH"}:
                client.get_json = lambda *_a, **_k: (_ for _ in ()).throw(
                    mealie_api.ConnectivityError("down")
                )
            return result

        client._request = request_then_break_readback
        with self.assertRaises(mealie_api.IndeterminateWriteError):
            client.mutate(
                "POST",
                "/api/recipes",
                payload,
                authority="mealie:recipes",
                confirm=confirmation,
                reconcile=reconcile,
            )

        client = self.client(max_retries=0)
        confirmation = client.preview(
            "POST",
            "/api/recipes",
            {"name": "Original"},
            authority="mealie:recipes",
            reconcile={"path": "/api/recipes", "query": {}, "match": {"name": "Original"}},
        )["confirmation"]
        calls = iter([None, mealie_api.ConnectivityError("reconcile down")])
        client._reconcile = lambda _contract: (
            lambda value: (_ for _ in ()).throw(value) if isinstance(value, Exception) else value
        )(next(calls))
        client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            mealie_api.IndeterminateWriteError("original")
        )
        with self.assertRaisesRegex(mealie_api.IndeterminateWriteError, "original"):
            client.mutate(
                "POST",
                "/api/recipes",
                {"name": "Original"},
                authority="mealie:recipes",
                confirm=confirmation,
                reconcile={"path": "/api/recipes", "query": {}, "match": {"name": "Original"}},
            )

    def test_authority_must_be_explicit_and_delete_is_unsupported(self):
        with self.assertRaises(mealie_api.AuthorityError):
            self.client().preview("POST", "/api/recipes", {"name": "x"}, authority="grocy:recipes")
        with self.assertRaises(mealie_api.UnsupportedOperationError):
            self.client().mutate(
                "DELETE", "/api/recipes/x", {}, authority="mealie:recipes", confirm="x"
            )

    def test_schema_scope_confirmation_payload_and_reconciliation_are_bound(self):
        client = self.client()
        with self.assertRaises(mealie_api.UnsupportedOperationError):
            client.preview("POST", "/api/users", {"name": "x"}, authority="mealie:recipes")
        approved = {"name": "Approved"}
        changed = {"name": "Changed"}
        approved_reconcile = {"path": "/api/recipes", "query": {}, "match": {"name": "Approved"}}
        changed_reconcile = {"path": "/api/recipes", "query": {}, "match": {"name": "Changed"}}
        confirmation = client.preview(
            "POST",
            "/api/recipes",
            approved,
            authority="mealie:recipes",
            reconcile=approved_reconcile,
        )["confirmation"]
        with self.assertRaises(mealie_api.ConfirmationRequiredError):
            client.mutate(
                "POST",
                "/api/recipes",
                changed,
                authority="mealie:recipes",
                confirm=confirmation,
                reconcile=changed_reconcile,
            )
        with self.assertRaises(mealie_api.UnsupportedOperationError):
            client.preview(
                "POST",
                "/api/recipes",
                approved,
                authority="mealie:recipes",
                reconcile={"path": "/api/users", "query": {}, "match": {"name": "Approved"}},
            )


if __name__ == "__main__":
    unittest.main()
