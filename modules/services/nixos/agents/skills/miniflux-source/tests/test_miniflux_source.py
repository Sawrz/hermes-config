import contextlib
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock
from urllib.parse import urlsplit

SCRIPT = Path(__file__).parents[1] / "scripts" / "miniflux_source.py"
SPEC = importlib.util.spec_from_file_location("miniflux_source", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import {SCRIPT}")
miniflux_source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(miniflux_source)


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    user: ClassVar[dict[str, Any]] = {"id": 11, "username": "assistant-a", "is_admin": False}
    categories: ClassVar[list[dict[str, Any]]] = [{"id": 21, "user_id": 11, "title": "News"}]
    feeds: ClassVar[list[dict[str, Any]]] = [
        {
            "id": 31,
            "user_id": 11,
            "feed_url": "https://a.example/feed.xml",
            "disabled": False,
            "crawler": False,
            "category": {"id": 21, "title": "News", "user_id": 11},
        }
    ]
    entries: ClassVar[list[dict[str, Any]]] = [
        {
            "id": 41,
            "title": "Senior AI Engineer",
            "url": "https://jobs.example/41",
            "content": "Build reliable AI systems",
            "published_at": "2026-08-29T08:00:00Z",
            "feed": {
                "id": 31,
                "title": "Example Careers",
                "feed_url": "https://a.example/feed.xml",
                "site_url": "https://jobs.example",
                "category": {"id": 21, "title": "News"},
            },
        }
    ]
    requests: ClassVar[list[dict[str, Any]]] = []
    include_foreign_in_list = False
    delay_create_once = False
    delay_delete_once = False

    def log_message(self, _format, *_args):
        pass

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(size) if size else b""

    def send_json(self, status, value=None, headers=None):
        raw = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, content in (headers or {}).items():
            self.send_header(name, content)
        self.end_headers()
        if raw:
            with contextlib.suppress(BrokenPipeError):
                self.wfile.write(raw)

    def record(self, body=None):
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "token": self.headers.get("X-Auth-Token"),
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )

    def require_token(self):
        if self.headers.get("X-Auth-Token") != "profile-private-token":
            self.send_json(401, {"error_message": "bad token"})
            return False
        return True

    @classmethod
    def foreign_feed(cls):
        return {
            "id": 99,
            "user_id": 12,
            "feed_url": "https://foreign.example/feed.xml",
            "disabled": False,
            "crawler": False,
            "category": {"id": 88, "title": "Private", "user_id": 12},
        }

    def do_GET(self):
        self.record()
        if not self.require_token():
            return
        path = urlsplit(self.path).path
        if path == "/v1/me":
            self.send_json(200, self.__class__.user)
        elif path == "/v1/categories":
            self.send_json(200, self.__class__.categories)
        elif path == "/v1/feeds":
            values = list(self.__class__.feeds)
            if self.__class__.include_foreign_in_list:
                values.append(self.__class__.foreign_feed())
            self.send_json(200, values)
        elif path == "/v1/entries":
            self.send_json(200, {"entries": self.__class__.entries})
        elif path == "/v1/feeds/99":
            self.send_json(200, self.__class__.foreign_feed())
        elif path.startswith("/v1/feeds/"):
            try:
                feed_id = int(path.split("/")[3])
                value = next(feed for feed in self.__class__.feeds if feed["id"] == feed_id)
            except (ValueError, StopIteration):
                self.send_json(404, {"error_message": "not found"})
            else:
                self.send_json(200, value)
        elif path == "/v1/cross-origin":
            self.send_json(302, headers={"Location": "https://evil.example/v1/feeds"})
        elif path == "/v1/outside-api":
            self.send_json(302, headers={"Location": "/admin/"})
        elif path == "/v1/encoded-traversal":
            self.send_json(302, headers={"Location": "/v1/%2e%2e/admin"})
        else:
            self.send_json(404, {"error_message": "not found"})

    def do_POST(self):
        payload = json.loads(self.body() or b"{}")
        self.record(payload)
        if not self.require_token():
            return
        path = urlsplit(self.path).path
        if path == "/v1/discover":
            self.send_json(200, [{"url": payload["url"], "title": "Discovered"}])
        elif path == "/v1/categories":
            value = {
                "id": max([item["id"] for item in self.__class__.categories] + [20]) + 1,
                "user_id": 11,
                "title": payload["title"],
            }
            self.__class__.categories.append(value)
            self.send_json(201, value)
        elif path == "/v1/feeds":
            value = {
                "id": max([item["id"] for item in self.__class__.feeds] + [30]) + 1,
                "user_id": 11,
                "feed_url": payload["feed_url"],
                "disabled": payload["disabled"],
                "crawler": payload.get("crawler", False),
                "scraper_rules": payload.get("scraper_rules", ""),
                "category": next(
                    (
                        category
                        for category in self.__class__.categories
                        if category["id"] == payload.get("category_id")
                    ),
                    self.__class__.categories[0],
                ),
            }
            self.__class__.feeds.append(value)
            if self.__class__.delay_create_once:
                self.__class__.delay_create_once = False
                time.sleep(0.15)
            self.send_json(201, {"feed_id": value["id"]})
        else:
            self.send_json(404, {"error_message": "not found"})

    def do_PUT(self):
        payload = json.loads(self.body() or b"{}")
        self.record(payload)
        if not self.require_token():
            return
        path = urlsplit(self.path).path
        if path == "/v1/feeds/99":
            self.send_json(404, {"error_message": "not found"})
            return
        try:
            feed_id = int(path.split("/")[3])
            value = next(feed for feed in self.__class__.feeds if feed["id"] == feed_id)
        except (ValueError, IndexError, StopIteration):
            self.send_json(404, {"error_message": "not found"})
            return
        if path.endswith("/refresh"):
            self.send_json(204)
            return
        changes = dict(payload)
        category_id = changes.pop("category_id", None)
        value.update(changes)
        if category_id is not None:
            value["category"] = next(
                category for category in self.__class__.categories if category["id"] == category_id
            )
        self.send_json(201, value)

    def do_DELETE(self):
        self.record()
        if not self.require_token():
            return
        path = urlsplit(self.path).path
        if path.startswith("/v1/feeds/"):
            feed_id = int(path.split("/")[3])
            before = len(self.__class__.feeds)
            self.__class__.feeds = [feed for feed in self.__class__.feeds if feed["id"] != feed_id]
            if self.__class__.delay_delete_once:
                self.__class__.delay_delete_once = False
                time.sleep(0.1)
            self.send_json(204 if len(self.__class__.feeds) != before else 404)
        elif path.startswith("/v1/categories/"):
            category_id = int(path.split("/")[3])
            self.__class__.categories = [
                category for category in self.__class__.categories if category["id"] != category_id
            ]
            self.send_json(204)
        else:
            self.send_json(404)


class MinifluxSourceTest(unittest.TestCase):
    def test_write_http_5xx_is_indeterminate_not_a_definite_server_rejection(self):
        client = self.client()
        client.opener.open = mock.Mock(
            side_effect=urllib.error.HTTPError(
                client.endpoint + "/v1/feeds/31", 500, "server error", Message(), None
            )
        )
        with self.assertRaises(miniflux_source.IndeterminateWriteError):
            client.request("PUT", "/v1/feeds/31", payload={"disabled": True})

    def test_write_malformed_success_body_is_indeterminate(self):
        class MalformedResponse:
            status = 200

            def read(self):
                return b"not-json"

        client = self.client()
        client.opener.open = mock.Mock(return_value=MalformedResponse())
        with self.assertRaises(miniflux_source.IndeterminateWriteError):
            client.request("PUT", "/v1/feeds/31", payload={"disabled": True})

    def test_same_origin_redirect_rejects_encoded_dot_segments(self):
        with self.assertRaises(miniflux_source.OriginViolationError):
            self.client().request("GET", "/v1/encoded-traversal")

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
        FixtureHandler.user = {"id": 11, "username": "assistant-a", "is_admin": False}
        FixtureHandler.categories = [{"id": 21, "user_id": 11, "title": "News"}]
        FixtureHandler.feeds = [
            {
                "id": 31,
                "user_id": 11,
                "feed_url": "https://a.example/feed.xml",
                "disabled": False,
                "crawler": False,
                "category": {"id": 21, "title": "News", "user_id": 11},
            }
        ]
        FixtureHandler.entries = [
            {
                "id": 41,
                "title": "Senior AI Engineer",
                "url": "https://jobs.example/41",
                "content": "Build reliable AI systems",
                "published_at": "2026-08-29T08:00:00Z",
                "feed": {
                    "id": 31,
                    "title": "Example Careers",
                    "feed_url": "https://a.example/feed.xml",
                    "site_url": "https://jobs.example",
                    "category": {"id": 21, "title": "News"},
                },
            }
        ]
        FixtureHandler.requests = []
        FixtureHandler.include_foreign_in_list = False
        FixtureHandler.delay_create_once = False
        FixtureHandler.delay_delete_once = False
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.endpoint = root / "endpoint"
        self.credential = root / "credential"
        self.endpoint.write_text(self.base + "\n")
        self.credential.write_text("profile-private-token\n")
        os.chmod(self.endpoint, 0o400)
        os.chmod(self.credential, 0o400)

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, **kwargs):
        return miniflux_source.MinifluxClient(
            endpoint_file=self.endpoint,
            credential_file=self.credential,
            timeout=kwargs.pop("timeout", 1),
            backoff=0,
            **kwargs,
        )

    def test_runtime_contract_and_header_only_authentication(self):
        self.assertEqual(
            self.client().preflight(),
            {"authenticated": True, "user_id": 11, "is_admin": False},
        )
        request = FixtureHandler.requests[-1]
        self.assertEqual(request["token"], "profile-private-token")
        self.assertIsNone(request["authorization"])
        self.assertNotIn("profile-private-token", request["path"])

    def test_entries_are_bounded_category_scoped_and_provenance_preserving(self):
        source_config = Path(self.tmp.name) / "job-sources.json"
        source_config.write_text(json.dumps({"category_id": 21, "limit": 10}))
        os.chmod(source_config, 0o400)
        self.assertEqual(miniflux_source.entry_config(source_config), (21, 10))
        entries = self.client().entries(21, 10)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["feed_id"], 31)
        self.assertEqual(entries[0]["url"], "https://jobs.example/41")
        self.assertFalse(entries[0]["content_truncated"])
        request = FixtureHandler.requests[-1]
        self.assertIn("category_id=21", request["path"])
        self.assertIn("limit=10", request["path"])
        FixtureHandler.entries[0]["feed"]["category"]["id"] = 22
        with self.assertRaises(miniflux_source.IsolationViolationError):
            self.client().entries(21, 10)
        with self.assertRaises(miniflux_source.ValidationError):
            self.client().entries(21, 101)

    def test_runtime_files_fail_closed_without_environment_fallback(self):
        os.chmod(self.credential, 0o600)
        with self.assertRaisesRegex(miniflux_source.RuntimeContractError, "0400"):
            self.client()
        os.chmod(self.credential, 0o400)
        self.credential.unlink()
        self.credential.symlink_to(self.endpoint)
        os.environ["MINIFLUX_API_KEY"] = "must-not-be-used"
        try:
            with self.assertRaisesRegex(miniflux_source.RuntimeContractError, "regular file"):
                self.client()
        finally:
            os.environ.pop("MINIFLUX_API_KEY")

    def test_endpoint_requires_service_root_and_https_or_loopback(self):
        for endpoint in [
            "https://example.test/v1",
            "http://example.test",
            "file:///tmp/miniflux",
            "https://name:secret@example.test",
        ]:
            os.chmod(self.endpoint, 0o600)
            self.endpoint.write_text(endpoint)
            os.chmod(self.endpoint, 0o400)
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaises(miniflux_source.RuntimeContractError),
            ):
                self.client()

    def test_admin_account_is_rejected(self):
        FixtureHandler.user["is_admin"] = True
        with self.assertRaisesRegex(miniflux_source.RuntimeContractError, "non-admin"):
            self.client().preflight()

    def test_list_and_direct_lookup_fail_closed_on_foreign_resources(self):
        FixtureHandler.include_foreign_in_list = True
        with self.assertRaises(miniflux_source.IsolationViolationError):
            self.client().sources()
        with self.assertRaises(miniflux_source.IsolationViolationError):
            self.client().source(99)

        FixtureHandler.include_foreign_in_list = False
        FixtureHandler.feeds[0]["category"]["user_id"] = 12
        with self.assertRaises(miniflux_source.IsolationViolationError):
            self.client().source(31)

    def test_categories_are_owned_and_cross_account_category_is_unavailable(self):
        self.assertEqual(self.client().category(21)["title"], "News")
        with self.assertRaises(miniflux_source.NotFoundError):
            self.client().category(88)

    def test_discovery_is_read_only(self):
        found = self.client().discover("https://site.example")
        self.assertEqual(found[0]["title"], "Discovered")
        self.assertEqual(len(FixtureHandler.feeds), 1)

    def test_add_is_disabled_idempotent_and_reads_back(self):
        client = self.client()
        first = client.add_source("https://new.example/feed.xml", 21)
        second = client.add_source("https://new.example/feed.xml", 21)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["disabled"])
        self.assertEqual(len(FixtureHandler.feeds), 2)
        create = next(request for request in FixtureHandler.requests if request["method"] == "POST")
        self.assertIs(create["body"]["disabled"], True)
        self.assertNotIn("crawler", create["body"])

    def test_add_rejects_preexisting_enabled_or_wrong_category_source(self):
        client = self.client()
        with self.assertRaisesRegex(miniflux_source.ValidationError, "disabled/category"):
            client.add_source("https://a.example/feed.xml", 21)
        FixtureHandler.feeds[0]["disabled"] = True
        with self.assertRaisesRegex(miniflux_source.ValidationError, "disabled/category"):
            client.add_source("https://a.example/feed.xml", 999)

    def test_ambiguous_add_reconciles_without_duplicate(self):
        FixtureHandler.delay_create_once = True
        result = self.client(timeout=0.03, max_retries=0).add_source(
            "https://slow.example/feed.xml"
        )
        self.assertEqual(result["feed_url"], "https://slow.example/feed.xml")
        self.assertEqual(
            len([feed for feed in FixtureHandler.feeds if feed["feed_url"] == result["feed_url"]]),
            1,
        )

    def test_enable_and_policy_changes_require_exact_confirmation(self):
        client = self.client()
        with self.assertRaises(miniflux_source.ConfirmationRequiredError):
            client.update_source(31, {"disabled": False}, confirm="yes")
        enabled = client.update_source(
            31,
            {"disabled": False},
            confirm='CONFIGURE 31 {"disabled":false}',
        )
        self.assertFalse(enabled["disabled"])
        with self.assertRaises(miniflux_source.ConfirmationRequiredError):
            client.update_source(31, {"crawler": True}, confirm="CONFIGURE 99")
        changed = client.update_source(
            31,
            {"crawler": True},
            confirm='CONFIGURE 31 {"crawler":true}',
        )
        self.assertTrue(changed["crawler"])

        FixtureHandler.categories.append({"id": 22, "user_id": 11, "title": "Tech"})
        moved = client.update_source(
            31,
            {"category_id": 22},
            confirm='CONFIGURE 31 {"category_id":22}',
        )
        self.assertEqual(moved["category"]["id"], 22)
        self.assertNotIn("category_id", moved)

    def test_disable_is_safe_default_and_has_mandatory_readback(self):
        result = self.client().update_source(31, {"disabled": True})
        self.assertTrue(result["disabled"])
        self.assertEqual(FixtureHandler.requests[-1]["method"], "GET")

    def test_unknown_configuration_fields_are_rejected_before_network_write(self):
        with self.assertRaises(miniflux_source.ValidationError):
            self.client().update_source(31, {"password": "secret"}, confirm="CONFIGURE 31")
        self.assertFalse(any(request["method"] == "PUT" for request in FixtureHandler.requests))

    def test_source_removal_requires_url_bound_confirmation_and_absence_readback(self):
        client = self.client()
        with self.assertRaises(miniflux_source.ConfirmationRequiredError):
            client.remove_source(31, confirm="REMOVE 31")
        result = client.remove_source(31, confirm="REMOVE 31 https://a.example/feed.xml")
        self.assertEqual(result["removed"], 31)
        self.assertEqual(FixtureHandler.requests[-1]["method"], "GET")

    def test_ambiguous_source_removal_reconciles_absence(self):
        FixtureHandler.delay_delete_once = True
        result = self.client(timeout=0.03, max_retries=0).remove_source(
            31,
            confirm="REMOVE 31 https://a.example/feed.xml",
        )
        self.assertEqual(result["removed"], 31)

    def test_category_lifecycle_is_confirmed_and_refuses_nonempty_removal(self):
        client = self.client()
        with self.assertRaises(miniflux_source.ConfirmationRequiredError):
            client.create_category("Tech", confirm="yes")
        created = client.create_category("Tech", confirm="CREATE CATEGORY Tech")
        self.assertEqual(created["user_id"], 11)
        with self.assertRaises(miniflux_source.ValidationError):
            client.remove_category(21, confirm="REMOVE CATEGORY 21 News")
        removed = client.remove_category(
            created["id"], confirm=f"REMOVE CATEGORY {created['id']} Tech"
        )
        self.assertEqual(removed["title"], "Tech")

    def test_refresh_requires_confirmation(self):
        with self.assertRaises(miniflux_source.ConfirmationRequiredError):
            self.client().refresh_source(31, confirm="yes")
        refreshed = self.client().refresh_source(31, confirm="REFRESH 31")
        self.assertEqual(refreshed["id"], 31)
        self.assertEqual(FixtureHandler.requests[-1]["method"], "GET")

    def test_redirects_are_same_origin_and_api_root_only(self):
        with self.assertRaises(miniflux_source.OriginViolationError):
            self.client().request("GET", "/v1/cross-origin")
        with self.assertRaises(miniflux_source.OriginViolationError):
            self.client().request("GET", "/v1/outside-api")


if __name__ == "__main__":
    unittest.main()
