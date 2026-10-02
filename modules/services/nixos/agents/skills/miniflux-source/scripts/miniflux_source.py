#!/usr/bin/env python3
"""Fail-closed source manager for one profile-private Miniflux account."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENDPOINT_FILE = Path("/run/hermes-credentials/services/miniflux/endpoint")
CREDENTIAL_FILE = Path("/run/hermes-credentials/services/miniflux/credential")
TIMEOUT = 15.0
MAX_RETRIES = 2


class MinifluxError(RuntimeError):
    code = "miniflux_error"
    status: int | None = None

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class RuntimeContractError(MinifluxError):
    code = "runtime_contract"


class ConnectivityError(MinifluxError):
    code = "connectivity"


class AuthenticationError(MinifluxError):
    code = "authentication"


class AuthorizationError(MinifluxError):
    code = "authorization"


class NotFoundError(MinifluxError):
    code = "not_found"


class ValidationError(MinifluxError):
    code = "validation"


class ServerError(MinifluxError):
    code = "server"


class OriginViolationError(MinifluxError):
    code = "origin_violation"


class ResponseFormatError(MinifluxError):
    code = "response_format"


class IsolationViolationError(MinifluxError):
    code = "isolation_violation"


class ConfirmationRequiredError(MinifluxError):
    code = "confirmation_required"


class IndeterminateWriteError(MinifluxError):
    code = "indeterminate_write"


ERRORS = {
    400: ValidationError,
    401: AuthenticationError,
    403: AuthorizationError,
    404: NotFoundError,
}


def secure_read(path: Path, label: str) -> str:
    """Read one immutable-looking 0400 regular file without following symlinks."""
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise RuntimeContractError(f"{label} runtime file is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeContractError(f"{label} runtime path is not a regular file")
    if stat.S_IMODE(before.st_mode) != 0o400:
        raise RuntimeContractError(f"{label} runtime file must have mode 0400")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeContractError(f"{label} runtime file cannot be opened safely") from exc
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            raise RuntimeContractError(f"{label} runtime file changed during validation")
        if stat.S_IMODE(after.st_mode) != 0o400:
            raise RuntimeContractError(f"{label} runtime file must have mode 0400")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            value = handle.read().strip()
    finally:
        os.close(fd)
    if not value:
        raise RuntimeContractError(f"{label} runtime file is empty")
    return value


def entry_config(path: Path) -> tuple[int, int]:
    try:
        value = json.loads(secure_read(path, "job source config"))
    except json.JSONDecodeError as exc:
        raise RuntimeContractError("job source config is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"category_id", "limit"}:
        raise RuntimeContractError("job source config must contain category_id and limit")
    category_id = value["category_id"]
    limit = value["limit"]
    if (
        type(category_id) is not int
        or type(limit) is not int
        or not 1 <= category_id <= 2**31 - 1
        or not 1 <= limit <= 100
    ):
        raise RuntimeContractError("job source category and limit are invalid")
    return category_id, limit


def origin(parts) -> tuple[str, str, int]:
    return (
        parts.scheme.lower(),
        (parts.hostname or "").lower(),
        parts.port or (443 if parts.scheme.lower() == "https" else 80),
    )


def is_loopback(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"}


class SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, client: MinifluxClient):
        self.client = client
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        self.client.validate_url(target)
        if req.get_method() not in {"GET", "HEAD"}:
            raise OriginViolationError("write redirects are forbidden")
        return super().redirect_request(req, fp, code, msg, headers, target)


class MinifluxClient:
    """High-level client which never exposes arbitrary API mutation paths."""

    def __init__(
        self,
        *,
        endpoint_file: Path | str = ENDPOINT_FILE,
        credential_file: Path | str = CREDENTIAL_FILE,
        timeout: float = TIMEOUT,
        max_retries: int = MAX_RETRIES,
        backoff: float = 0.25,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0 or max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5")
        endpoint = secure_read(Path(endpoint_file), "endpoint").rstrip("/")
        self.token = secure_read(Path(credential_file), "credential")
        parts = urlsplit(endpoint)
        if parts.username or parts.password or parts.query or parts.fragment:
            raise RuntimeContractError("endpoint must not contain credentials, query, or fragment")
        if not parts.hostname or parts.path not in {"", "/"}:
            raise RuntimeContractError("endpoint must be the canonical Miniflux service root")
        if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback(parts.hostname)):
            raise RuntimeContractError("endpoint must use HTTPS (HTTP is test-only on loopback)")
        self.endpoint = endpoint
        self._origin = origin(parts)
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff = max(0.0, float(backoff))
        self.opener = build_opener(SafeRedirectHandler(self))
        self._me: dict[str, Any] | None = None

    def validate_url(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.username or parts.password or parts.fragment:
            raise OriginViolationError("URL credentials and fragments are forbidden")
        if origin(parts) != self._origin:
            raise OriginViolationError("cross-origin API URL is forbidden")
        if "%" in parts.path or "\\" in parts.path or "\x00" in parts.path:
            raise OriginViolationError("encoded or non-canonical API path is forbidden")
        segments = parts.path.split("/")
        if any(segment in {".", ".."} for segment in segments) or "//" in parts.path:
            raise OriginViolationError("dot segments or repeated separators are forbidden")
        if parts.path != "/v1" and not parts.path.startswith("/v1/"):
            raise OriginViolationError("URL escaped the configured Miniflux API root")
        return url

    def api_url(self, path: str) -> str:
        if not path.startswith("/v1/") or path.startswith("//"):
            raise OriginViolationError("API path must be absolute under /v1/")
        return self.validate_url(self.endpoint + path)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, Any]:
        method = method.upper()
        url = self.api_url(path)
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Accept": "application/json",
            "User-Agent": "hermes-managed-miniflux-source/1",
            "X-Auth-Token": self.token,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        retryable = method in {"GET", "HEAD"}
        for attempt in range(self.max_retries + 1):
            try:
                response = self.opener.open(
                    Request(url, data=body, headers=headers, method=method),
                    timeout=self.timeout,
                )
                raw = response.read()
                status = response.status
                break
            except OriginViolationError:
                raise
            except HTTPError as exc:
                error = ERRORS.get(exc.code, ServerError if exc.code >= 500 else MinifluxError)
                if (
                    retryable
                    and (exc.code == 429 or exc.code >= 500)
                    and attempt < self.max_retries
                ):
                    time.sleep(self.backoff * (2**attempt))
                    continue
                if not retryable and exc.code >= 500:
                    raise IndeterminateWriteError(
                        "write received a server failure after dispatch; reconcile before retrying",
                        status=exc.code,
                    ) from exc
                raise error(f"Miniflux returned HTTP {exc.code}", status=exc.code) from exc
            except (TimeoutError, URLError, OSError) as exc:
                if retryable and attempt < self.max_retries:
                    time.sleep(self.backoff * (2**attempt))
                    continue
                if not retryable:
                    raise IndeterminateWriteError(
                        "write transport failed after dispatch; reconcile before retrying"
                    ) from exc
                raise ConnectivityError(
                    "Miniflux request failed at the transport boundary"
                ) from exc
        else:  # pragma: no cover
            raise ConnectivityError("Miniflux request exhausted retries")
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if not retryable:
                raise IndeterminateWriteError(
                    "write returned an unparseable response after dispatch; reconcile before retrying",
                    status=status,
                ) from exc
            raise ResponseFormatError("Miniflux returned a non-JSON response") from exc

    def me(self) -> dict[str, Any]:
        if self._me is None:
            _status, value = self.request("GET", "/v1/me")
            if not isinstance(value, dict) or not isinstance(value.get("id"), int):
                raise ResponseFormatError("Miniflux /v1/me response is malformed")
            if value.get("is_admin") is not False:
                raise RuntimeContractError(
                    "source management requires a non-admin Miniflux account"
                )
            self._me = value
        return self._me

    @property
    def user_id(self) -> int:
        return int(self.me()["id"])

    def require_owned(self, value: Any, kind: str) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("id"), int):
            raise ResponseFormatError(f"Miniflux {kind} response is malformed")
        if value.get("user_id") != self.user_id:
            raise IsolationViolationError(f"Miniflux returned a {kind} owned by another user")
        return value

    def require_owned_source(
        self,
        value: Any,
        category_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        source = self.require_owned(value, "feed")
        category = source.get("category")
        if not isinstance(category, dict) or not isinstance(category.get("id"), int):
            raise ResponseFormatError("Miniflux feed category is malformed")
        if category.get("user_id") not in (None, self.user_id):
            raise IsolationViolationError("Miniflux returned a feed with a foreign category owner")
        owned_ids = category_ids
        if owned_ids is None:
            owned_ids = {item["id"] for item in self.categories()}
        if category["id"] not in owned_ids:
            raise IsolationViolationError("Miniflux returned a feed in a foreign category")
        return source

    @staticmethod
    def source_value(source: Mapping[str, Any], key: str) -> Any:
        if key == "category_id":
            category = source.get("category")
            return category.get("id") if isinstance(category, dict) else None
        return source.get(key)

    def preflight(self) -> dict[str, Any]:
        me = self.me()
        return {"authenticated": True, "user_id": me["id"], "is_admin": False}

    def categories(self) -> list[dict[str, Any]]:
        _status, values = self.request("GET", "/v1/categories")
        if not isinstance(values, list):
            raise ResponseFormatError("Miniflux categories response is not a list")
        return [self.require_owned(value, "category") for value in values]

    def category(self, category_id: int) -> dict[str, Any]:
        found = [value for value in self.categories() if value["id"] == category_id]
        if len(found) != 1:
            raise NotFoundError("category is not present in the current account", status=404)
        return found[0]

    def create_category(self, title: str, *, confirm: str) -> dict[str, Any]:
        expected = f"CREATE CATEGORY {title}"
        if confirm != expected:
            raise ConfirmationRequiredError(f"exact confirmation required: {expected}")
        existing = [value for value in self.categories() if value.get("title") == title]
        if existing:
            return existing[0]
        _status, value = self.request("POST", "/v1/categories", payload={"title": title})
        created = self.require_owned(value, "category")
        return self.category(created["id"])

    def remove_category(self, category_id: int, *, confirm: str) -> dict[str, Any]:
        category = self.category(category_id)
        attached = [
            feed for feed in self.sources() if feed.get("category", {}).get("id") == category_id
        ]
        if attached:
            raise ValidationError("category still contains sources and cannot be removed")
        expected = f"REMOVE CATEGORY {category_id} {category['title']}"
        if confirm != expected:
            raise ConfirmationRequiredError(f"exact confirmation required: {expected}")
        self.request("DELETE", f"/v1/categories/{category_id}")
        if any(value["id"] == category_id for value in self.categories()):
            raise IsolationViolationError("removed category still exists after readback")
        return {"removed": category_id, "title": category["title"]}

    def sources(self) -> list[dict[str, Any]]:
        category_ids = {category["id"] for category in self.categories()}
        _status, values = self.request("GET", "/v1/feeds")
        if not isinstance(values, list):
            raise ResponseFormatError("Miniflux feeds response is not a list")
        return [self.require_owned_source(value, category_ids) for value in values]

    def entries(self, category_id: int, limit: int) -> list[dict[str, Any]]:
        if category_id <= 0 or limit <= 0 or limit > 100:
            raise ValidationError("entries category and limit must be bounded positive integers")
        self.category(category_id)
        query = urlencode(
            {
                "category_id": category_id,
                "direction": "desc",
                "limit": limit,
                "order": "published_at",
            }
        )
        _status, payload = self.request("GET", f"/v1/entries?{query}")
        values = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(values, list) or len(values) > limit:
            raise ResponseFormatError("Miniflux entries response is malformed or unbounded")
        result: list[dict[str, Any]] = []
        for value in values:
            feed = value.get("feed") if isinstance(value, dict) else None
            if not isinstance(value, dict) or not isinstance(feed, dict):
                raise ResponseFormatError("Miniflux entry lacks feed provenance")
            if not isinstance(value.get("id"), int) or not isinstance(feed.get("id"), int):
                raise ResponseFormatError("Miniflux entry identity is malformed")
            category = feed.get("category")
            if not isinstance(category, dict) or category.get("id") != category_id:
                raise IsolationViolationError(
                    "Miniflux returned an entry outside the requested category"
                )
            strings = {
                "title": value.get("title"),
                "url": value.get("url"),
                "published_at": value.get("published_at"),
                "feed_title": feed.get("title"),
                "feed_url": feed.get("feed_url"),
                "site_url": feed.get("site_url"),
            }
            if not all(isinstance(item, str) and item for item in strings.values()):
                raise ResponseFormatError("Miniflux entry provenance fields are malformed")
            safe_strings = cast(dict[str, str], strings)
            content = value.get("content", "")
            if not isinstance(content, str):
                raise ResponseFormatError("Miniflux entry content is malformed")
            result.append(
                {
                    "content": content[:20000],
                    "content_truncated": len(content) > 20000,
                    "feed_id": feed["id"],
                    "feed_title": safe_strings["feed_title"][:500],
                    "feed_url": safe_strings["feed_url"],
                    "id": value["id"],
                    "published_at": safe_strings["published_at"],
                    "site_url": safe_strings["site_url"],
                    "title": safe_strings["title"][:1000],
                    "url": safe_strings["url"],
                }
            )
        return result

    def source(self, feed_id: int) -> dict[str, Any]:
        _status, value = self.request("GET", f"/v1/feeds/{feed_id}")
        return self.require_owned_source(value)

    def discover(self, url: str) -> list[dict[str, Any]]:
        _status, values = self.request("POST", "/v1/discover", payload={"url": url})
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            raise ResponseFormatError("Miniflux discovery response is malformed")
        return values

    def add_source(
        self,
        feed_url: str,
        category_id: int | None = None,
    ) -> dict[str, Any]:
        """Add idempotently and always disabled. Enable is a separate confirmed action."""
        existing = [feed for feed in self.sources() if feed.get("feed_url") == feed_url]
        if existing:
            if len(existing) != 1:
                raise ValidationError("multiple existing sources use the same URL")
            current = existing[0]
            same_category = (
                category_id is None or self.source_value(current, "category_id") == category_id
            )
            if current.get("disabled") is True and same_category:
                return current
            raise ValidationError(
                "source already exists but does not satisfy the disabled/category contract"
            )
        if category_id is not None:
            self.category(category_id)
        payload: dict[str, Any] = {
            "feed_url": feed_url,
            "disabled": True,
        }
        if category_id is not None:
            payload["category_id"] = category_id
        try:
            _status, value = self.request("POST", "/v1/feeds", payload=payload)
        except IndeterminateWriteError:
            matches = [feed for feed in self.sources() if feed.get("feed_url") == feed_url]
            if (
                len(matches) != 1
                or matches[0].get("disabled") is not True
                or (
                    category_id is not None
                    and self.source_value(matches[0], "category_id") != category_id
                )
            ):
                raise
            return matches[0]
        if not isinstance(value, dict) or not isinstance(value.get("feed_id"), int):
            raise ResponseFormatError("Miniflux create-feed response has no feed_id")
        created = self.source(value["feed_id"])
        if (
            created.get("feed_url") != feed_url
            or created.get("disabled") is not True
            or (
                category_id is not None and self.source_value(created, "category_id") != category_id
            )
        ):
            raise IsolationViolationError(
                "created source readback did not preserve disabled ownership contract"
            )
        return created

    def update_source(
        self,
        feed_id: int,
        changes: Mapping[str, Any],
        *,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "blocklist_rules",
            "category_id",
            "crawler",
            "disabled",
            "fetch_via_proxy",
            "ignore_http_cache",
            "keeplist_rules",
            "rewrite_rules",
            "scraper_rules",
            "user_agent",
        }
        if not changes or set(changes) - allowed:
            raise ValidationError("source changes are empty or contain unsupported fields")
        before = self.source(feed_id)
        if "category_id" in changes:
            self.category(int(changes["category_id"]))
        safe_disable = changes == {"disabled": True}
        canonical_changes = json.dumps(dict(changes), sort_keys=True, separators=(",", ":"))
        expected = f"CONFIGURE {feed_id} {canonical_changes}"
        if not safe_disable and confirm != expected:
            raise ConfirmationRequiredError(f"exact confirmation required: {expected}")
        try:
            _status, value = self.request("PUT", f"/v1/feeds/{feed_id}", payload=changes)
        except IndeterminateWriteError:
            value = self.source(feed_id)
            if not all(self.source_value(value, key) == item for key, item in changes.items()):
                raise
        response = self.require_owned_source(value)
        if response["id"] != before["id"]:
            raise IsolationViolationError("source mutation response changed resource identity")
        result = self.source(feed_id)
        if not all(self.source_value(result, key) == item for key, item in changes.items()):
            raise IsolationViolationError("source mutation readback does not match the request")
        return result

    def refresh_source(self, feed_id: int, *, confirm: str) -> dict[str, Any]:
        self.source(feed_id)
        expected = f"REFRESH {feed_id}"
        if confirm != expected:
            raise ConfirmationRequiredError(f"exact confirmation required: {expected}")
        self.request("PUT", f"/v1/feeds/{feed_id}/refresh")
        return self.source(feed_id)

    def remove_source(self, feed_id: int, *, confirm: str) -> dict[str, Any]:
        source = self.source(feed_id)
        expected = f"REMOVE {feed_id} {source['feed_url']}"
        if confirm != expected:
            raise ConfirmationRequiredError(f"exact confirmation required: {expected}")
        try:
            self.request("DELETE", f"/v1/feeds/{feed_id}")
        except IndeterminateWriteError:
            try:
                self.source(feed_id)
            except NotFoundError:
                return {"removed": feed_id, "feed_url": source["feed_url"]}
            raise
        try:
            self.source(feed_id)
        except NotFoundError:
            return {"removed": feed_id, "feed_url": source["feed_url"]}
        raise IsolationViolationError("removed source still exists after readback")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--timeout", type=float, default=TIMEOUT)
    result.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("categories")
    sub.add_parser("sources")
    entries = sub.add_parser("entries")
    entries.add_argument("--config", type=Path, required=True)
    discover = sub.add_parser("discover")
    discover.add_argument("url")
    add = sub.add_parser("add")
    add.add_argument("url")
    add.add_argument("--category-id", type=int)
    create_category = sub.add_parser("category-create")
    create_category.add_argument("title")
    create_category.add_argument("--confirm", required=True)
    remove_category = sub.add_parser("category-remove")
    remove_category.add_argument("id", type=int)
    remove_category.add_argument("--confirm", required=True)
    disable = sub.add_parser("disable")
    disable.add_argument("id", type=int)
    enable = sub.add_parser("enable")
    enable.add_argument("id", type=int)
    enable.add_argument("--confirm", required=True)
    configure = sub.add_parser("configure")
    configure.add_argument("id", type=int)
    configure.add_argument("--category-id", type=int)
    configure.add_argument("--crawler", choices=["on", "off"])
    configure.add_argument("--scraper-rules")
    configure.add_argument("--rewrite-rules")
    configure.add_argument("--blocklist-rules")
    configure.add_argument("--keeplist-rules")
    configure.add_argument("--user-agent")
    configure.add_argument("--ignore-http-cache", choices=["on", "off"])
    configure.add_argument("--fetch-via-proxy", choices=["on", "off"])
    configure.add_argument("--confirm", required=True)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("id", type=int)
    refresh.add_argument("--confirm", required=True)
    remove = sub.add_parser("remove")
    remove.add_argument("id", type=int)
    remove.add_argument("--confirm", required=True)
    return result


def configuration_changes(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    fields = {
        "category_id": args.category_id,
        "scraper_rules": args.scraper_rules,
        "rewrite_rules": args.rewrite_rules,
        "blocklist_rules": args.blocklist_rules,
        "keeplist_rules": args.keeplist_rules,
        "user_agent": args.user_agent,
    }
    changes.update({key: value for key, value in fields.items() if value is not None})
    for name in ("crawler", "ignore_http_cache", "fetch_via_proxy"):
        value = getattr(args, name)
        if value is not None:
            changes[name] = value == "on"
    return changes


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        client = MinifluxClient(timeout=args.timeout, max_retries=args.max_retries)
        if args.command == "preflight":
            output = client.preflight()
        elif args.command == "categories":
            output = client.categories()
        elif args.command == "sources":
            output = client.sources()
        elif args.command == "entries":
            category_id, limit = entry_config(args.config)
            output = client.entries(category_id, limit)
        elif args.command == "discover":
            output = client.discover(args.url)
        elif args.command == "add":
            output = client.add_source(args.url, args.category_id)
        elif args.command == "category-create":
            output = client.create_category(args.title, confirm=args.confirm)
        elif args.command == "category-remove":
            output = client.remove_category(args.id, confirm=args.confirm)
        elif args.command == "disable":
            output = client.update_source(args.id, {"disabled": True})
        elif args.command == "enable":
            output = client.update_source(args.id, {"disabled": False}, confirm=args.confirm)
        elif args.command == "configure":
            output = client.update_source(
                args.id, configuration_changes(args), confirm=args.confirm
            )
        elif args.command == "refresh":
            output = client.refresh_source(args.id, confirm=args.confirm)
        else:
            output = client.remove_source(args.id, confirm=args.confirm)
        json.dump(output, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except (MinifluxError, OSError, ValueError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, MinifluxError) else "input_error"
        status = exc.status if isinstance(exc, MinifluxError) else None
        json.dump({"error": code, "status": status, "message": str(exc)}, sys.stderr)
        sys.stderr.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
