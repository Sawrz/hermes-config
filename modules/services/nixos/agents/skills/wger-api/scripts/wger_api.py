#!/usr/bin/env python3
"""Fail-closed interactive client for a profile-private wger API binding.

Only Python's standard library is used so the helper travels with the managed
skill.  The endpoint and permanent token are read from the module-owned runtime
files; there is deliberately no environment or command-line credential path.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_ENDPOINT_FILE = Path("/run/hermes-credentials/services/wger/endpoint")
DEFAULT_CREDENTIAL_FILE = Path("/run/hermes-credentials/services/wger/credential")
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_PAGE_CAP = 1000
WRITE_OPERATIONS = {
    "POST": re.compile(r"^/weightentry/$"),
    "PUT": re.compile(r"^/weightentry/[1-9][0-9]*/$"),
    "PATCH": re.compile(r"^/weightentry/[1-9][0-9]*/$"),
}


class WgerError(RuntimeError):
    code = "wger_error"
    retryable = False
    indeterminate = False

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class RuntimeContractError(WgerError):
    code = "runtime_contract"


class ConnectivityError(WgerError):
    code = "connectivity"
    retryable = True


class AuthenticationError(WgerError):
    code = "authentication"


class AuthorizationError(WgerError):
    code = "authorization"


class NotFoundError(WgerError):
    code = "not_found"


class ValidationError(WgerError):
    code = "validation"


class ConflictError(WgerError):
    code = "conflict"


class RateLimitError(WgerError):
    code = "rate_limit"
    retryable = True


class ServerError(WgerError):
    code = "server"
    retryable = True


class ResponseFormatError(WgerError):
    code = "response_format"


class SchemaDriftError(WgerError):
    code = "schema_drift"


class VersionDriftError(WgerError):
    code = "version_drift"


class PaginationError(WgerError):
    code = "pagination"


class OriginViolationError(WgerError):
    code = "origin_violation"


class ConfirmationRequiredError(WgerError):
    code = "confirmation_required"


class UnsupportedOperationError(WgerError):
    code = "unsupported_operation"


class ReadbackError(WgerError):
    code = "readback"


class IndeterminateWriteError(WgerError):
    code = "indeterminate_write"
    indeterminate = True


ERROR_BY_STATUS = {
    400: ValidationError,
    401: AuthenticationError,
    403: AuthorizationError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitError,
}


def _secure_read(path: Path, label: str) -> str:
    """Read one 0400 regular file without following a symlink."""
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


def _origin(parts) -> tuple[str, str, int]:
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _is_loopback(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"}


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, client: WgerClient):
        self.client = client
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        self.client._validate_url(target)
        if req.get_method() not in {"GET", "HEAD"}:
            raise OriginViolationError("write redirects are forbidden")
        return super().redirect_request(req, fp, code, msg, headers, target)


class WgerClient:
    """Small synchronous client for deliberate interactive wger operations."""

    def __init__(
        self,
        *,
        endpoint_file: Path | str = DEFAULT_ENDPOINT_FILE,
        credential_file: Path | str = DEFAULT_CREDENTIAL_FILE,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff: float = 0.25,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0 or max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5")
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff = max(0.0, float(backoff))
        self.endpoint = _secure_read(Path(endpoint_file), "endpoint").rstrip("/")
        self._credential_file = Path(credential_file)
        self._binding_directory = self._credential_file.parent
        self._credential = _secure_read(self._credential_file, "credential")

        parts = urlsplit(self.endpoint)
        if parts.username or parts.password or parts.query or parts.fragment:
            raise RuntimeContractError("endpoint must not contain credentials, query, or fragment")
        if not parts.hostname or parts.path.rstrip("/") != "/api/v2":
            raise RuntimeContractError("endpoint must be the canonical /api/v2 root")
        if parts.scheme != "https" and not (
            parts.scheme == "http" and _is_loopback(parts.hostname)
        ):
            raise RuntimeContractError("endpoint must use HTTPS (HTTP is test-only on loopback)")
        self._origin = _origin(parts)
        self._api_path = "/api/v2"
        self._opener = build_opener(_SafeRedirectHandler(self))

    @contextmanager
    def _profile_mutation_lock(self):
        """Serialize creates on this stable profile-private service binding."""
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            fd = os.open(self._binding_directory, flags)
        except OSError as exc:
            raise RuntimeContractError("credential binding cannot be opened safely") from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise RuntimeContractError("credential binding cannot be locked") from exc
            yield
        finally:
            os.close(fd)

    def _validate_url(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.username or parts.password or parts.fragment:
            raise OriginViolationError("URL credentials and fragments are forbidden")
        if _origin(parts) != self._origin:
            raise OriginViolationError("cross-origin API URL is forbidden")
        if "%" in parts.path or "\\" in parts.path or "\x00" in parts.path:
            raise OriginViolationError("encoded or non-canonical API path is forbidden")
        segments = parts.path.split("/")
        if any(segment in {".", ".."} for segment in segments) or "//" in parts.path:
            raise OriginViolationError("dot segments or repeated separators are forbidden")
        normalized_path = parts.path.rstrip("/")
        if normalized_path != self._api_path and not parts.path.startswith(self._api_path + "/"):
            raise OriginViolationError("URL escaped the configured /api/v2 root")
        return url

    def _url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        if urlsplit(path).scheme:
            url = self._validate_url(path)
        else:
            if not path.startswith("/") or path.startswith("//"):
                raise OriginViolationError("API paths must be absolute within /api/v2")
            url = self.endpoint + path
            self._validate_url(url)
        if query:
            parts = urlsplit(url)
            if parts.query:
                raise OriginViolationError("query must be supplied separately")
            url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), "")
            )
        return url

    def _error_for(self, status: int) -> type[WgerError]:
        if status in ERROR_BY_STATUS:
            return ERROR_BY_STATUS[status]
        if 500 <= status <= 599:
            return ServerError
        return WgerError

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        authenticate: bool = True,
    ) -> tuple[int, Any, Mapping[str, str]]:
        method = method.upper()
        url = self._url(path, query)
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Accept": "application/json",
            "User-Agent": "hermes-managed-wger-api/1",
        }
        if authenticate:
            headers["Authorization"] = f"Token {self._credential}"
        if body is not None:
            headers["Content-Type"] = "application/json"

        retryable_method = method in {"GET", "HEAD"}
        for attempt in range(self.max_retries + 1):
            request = Request(url, data=body, headers=headers, method=method)
            try:
                response = self._opener.open(request, timeout=self.timeout)
                raw = response.read()
                status = response.status
                response_headers = dict(response.headers.items())
                break
            except OriginViolationError:
                raise
            except HTTPError as exc:
                status = exc.code
                error_type = self._error_for(status)
                retryable_status = status == 429 or 500 <= status <= 599
                if not retryable_method and retryable_status:
                    raise IndeterminateWriteError(
                        f"write received HTTP {status} after dispatch; reconcile before retrying",
                        status=status,
                    ) from exc
                if retryable_method and retryable_status and attempt < self.max_retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = (
                            min(5.0, max(0.0, float(retry_after)))
                            if retry_after
                            else self.backoff * (2**attempt)
                        )
                    except ValueError:
                        delay = self.backoff * (2**attempt)
                    time.sleep(delay)
                    continue
                raise error_type(f"wger returned HTTP {status}", status=status) from exc
            except (TimeoutError, URLError, OSError) as exc:
                if retryable_method and attempt < self.max_retries:
                    time.sleep(self.backoff * (2**attempt))
                    continue
                if not retryable_method:
                    raise IndeterminateWriteError(
                        "write transport failed after dispatch; reconcile before retrying"
                    ) from exc
                raise ConnectivityError("wger request failed at the transport boundary") from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise ConnectivityError("wger request exhausted retries")

        if not raw:
            data: Any = None
        else:
            try:
                data = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if not retryable_method:
                    raise IndeterminateWriteError(
                        "write returned an unreadable response; reconcile before retrying"
                    ) from exc
                raise ResponseFormatError("wger returned a non-JSON response") from exc
        return status, data, response_headers

    def get_json(self, path: str, *, query: Mapping[str, Any] | None = None) -> Any:
        _status, data, _headers = self._request("GET", path, query=query)
        return data

    def preflight(self) -> dict[str, Any]:
        status, _private_body, _headers = self._request("GET", "/userprofile/")
        return {"status": status, "authenticated": status == 200}

    def _operation(
        self, schema: Mapping[str, Any], path: str, method: str
    ) -> Mapping[str, Any] | None:
        canonical_path = self._api_path + path
        paths = schema.get("paths", {})
        operation = paths.get(canonical_path, {}).get(method.lower())
        if isinstance(operation, dict):
            return operation
        for template, item in paths.items():
            pieces = re.split(r"(\{[^/{}]+\})", template)
            pattern = "".join(
                "[^/]+" if piece.startswith("{") else re.escape(piece) for piece in pieces
            )
            if re.fullmatch(pattern, canonical_path):
                candidate = item.get(method.lower()) if isinstance(item, dict) else None
                if isinstance(candidate, dict):
                    return candidate
        return None

    def _resolve_schema(
        self, document: Mapping[str, Any], node: Mapping[str, Any], seen: set[str] | None = None
    ) -> dict[str, Any]:
        seen = set() if seen is None else seen
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
                raise SchemaDriftError("request schema has an unsafe or cyclic reference")
            target: Any = document
            try:
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError) as exc:
                raise SchemaDriftError("request schema reference cannot be resolved") from exc
            if not isinstance(target, dict):
                raise SchemaDriftError("request schema reference is not an object")
            return self._resolve_schema(document, target, seen | {ref})
        if "allOf" in node:
            merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
            for part in node["allOf"]:
                resolved = self._resolve_schema(document, part, seen)
                merged["properties"].update(resolved.get("properties", {}))
                merged["required"] = sorted(set(merged["required"] + resolved.get("required", [])))
                if resolved.get("additionalProperties") is False:
                    merged["additionalProperties"] = False
            return merged
        return dict(node)

    def _validate_payload_schema(
        self,
        document: Mapping[str, Any],
        operation: Mapping[str, Any],
        method: str,
        payload: Mapping[str, Any],
    ) -> None:
        node = (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        if not isinstance(node, dict):
            raise SchemaDriftError("live write operation has no JSON request schema")
        request_schema = self._resolve_schema(document, node)
        properties = request_schema.get("properties", {})
        if properties:
            unknown = sorted(set(payload) - set(properties))
            if unknown:
                raise SchemaDriftError(
                    "payload fields are absent from the live request schema: " + ", ".join(unknown)
                )
        if method in {"POST", "PUT"}:
            missing = sorted(set(request_schema.get("required", [])) - set(payload))
            if missing:
                raise SchemaDriftError(
                    "payload is missing live-schema required fields: " + ", ".join(missing)
                )
        python_types = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        for name, value in payload.items():
            field = properties.get(name)
            if not isinstance(field, dict):
                continue
            field = self._resolve_schema(document, field)
            if value is None and field.get("nullable"):
                continue
            type_name = field.get("type")
            expected = python_types.get(type_name) if isinstance(type_name, str) else None
            if expected is not None and (
                not isinstance(value, expected)
                or type_name in {"integer", "number"}
                and isinstance(value, bool)
            ):
                raise SchemaDriftError(f"payload field {name!r} has the wrong live-schema type")
            if "enum" in field and value not in field["enum"]:
                raise SchemaDriftError(f"payload field {name!r} is outside the live-schema enum")

    def check_schema(
        self,
        path: str,
        method: str,
        *,
        expected_version: str | None = None,
    ) -> Mapping[str, Any]:
        _status, schema, _headers = self._request(
            "GET", "/schema", query={"format": "json"}, authenticate=False
        )
        if not isinstance(schema, dict) or not str(schema.get("openapi", "")).startswith("3."):
            raise SchemaDriftError("live wger schema is not supported OpenAPI 3.x")
        version = (
            schema.get("info", {}).get("version") if isinstance(schema.get("info"), dict) else None
        )
        if expected_version is not None and version != expected_version:
            raise VersionDriftError(
                f"live wger version differs from required version {expected_version!r}"
            )
        canonical_path = self._api_path + path
        operation = self._operation(schema, path, method)
        if not isinstance(operation, dict):
            raise SchemaDriftError(f"live schema does not expose {method.upper()} {canonical_path}")
        if method.upper() in {"POST", "PUT", "PATCH"}:
            content = operation.get("requestBody", {}).get("content", {})
            if "application/json" not in content:
                raise SchemaDriftError("live write operation does not accept application/json")
        return schema

    def _validate_next(self, url: str) -> str:
        return self._validate_url(url)

    def _consume_page(self, page: Any, current: int, cap: int) -> tuple[list[Any], str | None]:
        if not isinstance(page, dict) or not {"count", "next", "previous", "results"}.issubset(
            page
        ):
            raise PaginationError("paginated response is missing count/next/previous/results")
        if not isinstance(page["results"], list):
            raise PaginationError("paginated results is not a list")
        if current + len(page["results"]) > cap:
            raise PaginationError(f"pagination cap {cap} would be exceeded")
        next_url = page["next"]
        if next_url is not None and not isinstance(next_url, str):
            raise PaginationError("pagination next is neither a URL nor null")
        return page["results"], next_url

    def list_all(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        cap: int = DEFAULT_PAGE_CAP,
    ) -> list[Any]:
        if cap < 1:
            raise ValueError("cap must be positive")
        self.check_schema(path, "GET")
        url = self._url(path, query)
        seen: set[str] = set()
        records: list[Any] = []
        while url:
            if url in seen:
                raise PaginationError("pagination loop detected")
            seen.add(url)
            page = self.get_json(url)
            values, next_url = self._consume_page(page, len(records), cap)
            records.extend(values)
            url = self._validate_next(next_url) if next_url else ""
        return records

    def preview(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        *,
        expected_version: str | None = None,
        reconcile: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        allowed_path = WRITE_OPERATIONS.get(method)
        if allowed_path is None or not allowed_path.fullmatch(path):
            raise UnsupportedOperationError(
                "writes are limited to reviewed weight-entry operations"
            )
        if not isinstance(payload, Mapping) or not payload:
            raise UnsupportedOperationError("write payload must be a non-empty JSON object")
        try:
            payload_snapshot = json.loads(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise UnsupportedOperationError("write payload must be finite JSON") from exc
        if not isinstance(payload_snapshot, dict) or not payload_snapshot:
            raise UnsupportedOperationError("write payload must be a non-empty JSON object")
        reconcile_snapshot = self._validate_reconcile_contract(
            method, path, payload_snapshot, reconcile
        )
        schema = self.check_schema(path, method, expected_version=expected_version)
        operation = self._operation(schema, path, method)
        if not isinstance(operation, dict):  # check_schema already guarantees this
            raise SchemaDriftError("live write operation disappeared during validation")
        self._validate_payload_schema(schema, operation, method, payload_snapshot)
        approval_sha256 = hashlib.sha256(
            json.dumps(
                {"payload": payload_snapshot, "reconcile": reconcile_snapshot},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return {
            "method": method,
            "path": path,
            "payload": payload_snapshot,
            "reconcile": reconcile_snapshot,
            "confirmation": f"APPLY {method} {path} SHA256 {approval_sha256}",
        }

    def _validate_reconcile_contract(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        contract: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        if contract is None:
            if method == "POST":
                raise UnsupportedOperationError("POST requires a reconciliation contract")
            return None
        if not isinstance(contract, Mapping) or set(contract) != {"path", "query", "match"}:
            raise UnsupportedOperationError("reconcile requires exactly path, query, and match")
        reconcile_path = contract["path"]
        query = contract["query"]
        match = contract["match"]
        if method != "POST" or not isinstance(reconcile_path, str) or reconcile_path != path:
            raise UnsupportedOperationError(
                "reconcile path must be the canonical POST collection path"
            )
        self._url(reconcile_path)
        if not isinstance(query, Mapping) or not query:
            raise UnsupportedOperationError("reconcile query must be a non-empty object")
        if not isinstance(match, Mapping) or not match:
            raise UnsupportedOperationError("reconcile match must be a non-empty object")
        if len(query) > 20 or len(match) > 20:
            raise UnsupportedOperationError("reconcile query and match are limited to 20 fields")

        def bounded_scalar(value: Any) -> bool:
            if isinstance(value, str):
                return 0 < len(value) <= 256
            if isinstance(value, bool):
                return True
            if isinstance(value, int):
                return len(str(value)) <= 256
            return isinstance(value, float) and math.isfinite(value)

        if any(
            not isinstance(name, str) or not name or not bounded_scalar(value)
            for name, value in query.items()
        ):
            raise UnsupportedOperationError("reconcile query must contain bounded scalar fields")
        if any(
            not isinstance(name, str) or not name or not bounded_scalar(value)
            for name, value in match.items()
        ):
            raise UnsupportedOperationError("reconcile match must contain bounded scalar fields")
        if any(name not in payload or payload[name] != value for name, value in match.items()):
            raise UnsupportedOperationError("reconcile match must be bound to the POST payload")
        if dict(query) != dict(match):
            raise UnsupportedOperationError(
                "reconcile query and match must be the same payload-bound identity"
            )
        return {"path": reconcile_path, "query": dict(query), "match": dict(match)}

    def _reconcile(self, contract: Mapping[str, Any] | None) -> Any | None:
        if contract is None:
            return None
        allowed = {"path", "query", "match"}
        if set(contract) != allowed:
            raise UnsupportedOperationError("reconcile requires exactly path, query, and match")
        match = contract["match"]
        if not isinstance(match, Mapping) or not match:
            raise UnsupportedOperationError("reconcile match must be a non-empty object")
        try:
            found = self.list_all(str(contract["path"]), query=contract["query"], cap=2)
        except PaginationError as exc:
            raise IndeterminateWriteError(
                "reconciliation returned more than the bounded match set"
            ) from exc
        exact = [
            record
            for record in found
            if isinstance(record, dict) and all(record.get(k) == v for k, v in match.items())
        ]
        if len(exact) > 1:
            raise IndeterminateWriteError("reconciliation found multiple matching resources")
        return exact[0] if exact else None

    def _readback_path(self, method: str, path: str, data: Any, headers: Mapping[str, str]) -> str:
        if method in {"PUT", "PATCH"}:
            return path
        location = headers.get("Location") or headers.get("location")
        if location:
            absolute = self._validate_url(urljoin(self.endpoint + "/", location))
            parts = urlsplit(absolute)
            if parts.query:
                raise OriginViolationError("write Location must not contain a query")
            relative = parts.path[len(self._api_path) :]
            if not relative.startswith("/") or relative.startswith(self._api_path + "/"):
                raise OriginViolationError("write Location is not a canonical API-relative path")
            return relative
        if isinstance(data, dict) and data.get("id") is not None:
            return path.rstrip("/") + f"/{data['id']}/"
        raise ReadbackError("write response has neither a safe Location nor an id")

    def _verify_readback(self, record: Any, payload: Mapping[str, Any]) -> Any:
        if not isinstance(record, dict):
            raise ReadbackError("readback is not a JSON object")
        mismatched = [key for key, value in payload.items() if record.get(key) != value]
        if mismatched:
            raise ReadbackError(
                "readback does not match requested fields: " + ", ".join(sorted(mismatched))
            )
        return record

    def mutate(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        *,
        confirm: str,
        expected_version: str | None = None,
        reconcile: Mapping[str, Any] | None = None,
    ) -> Any:
        preview = self.preview(
            method,
            path,
            payload,
            expected_version=expected_version,
            reconcile=reconcile,
        )
        if confirm != preview["confirmation"]:
            raise ConfirmationRequiredError(
                f"exact confirmation required: {preview['confirmation']}"
            )
        method = method.upper()
        approved_payload = preview["payload"]
        approved_reconcile = preview["reconcile"]

        lock = self._profile_mutation_lock() if method == "POST" else nullcontext()
        with lock:
            if method == "POST":
                existing = self._reconcile(approved_reconcile)
                if existing is not None:
                    return self._verify_readback(existing, approved_payload)

            try:
                _status, data, headers = self._request(method, path, payload=approved_payload)
            except IndeterminateWriteError as exc:
                if method == "POST":
                    existing = self._reconcile(approved_reconcile)
                else:
                    try:
                        existing = self.get_json(path)
                    except WgerError:
                        raise exc
                if existing is None:
                    raise
                try:
                    return self._verify_readback(existing, approved_payload)
                except ReadbackError:
                    raise exc

            try:
                readback_path = self._readback_path(method, path, data, headers)
                record = self.get_json(readback_path)
                return self._verify_readback(record, approved_payload)
            except WgerError as exc:
                raise IndeterminateWriteError(
                    "write response could not be proven by readback; reconcile before retrying"
                ) from exc


def _load_json_object(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def _parse_query(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError("query entries must be key=value")
        key, item = value.split("=", 1)
        if not key:
            raise ValueError("query key must not be empty")
        result[key] = item
    return result


class _WriteContractParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if (
            getattr(parsed, "command", None) in {"preview", "apply"}
            and str(getattr(parsed, "method", "")).upper() == "POST"
            and not getattr(parsed, "reconcile_file", None)
        ):
            self.error("POST requires --reconcile-file")
        return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _WriteContractParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")

    schema = sub.add_parser("schema-check")
    schema.add_argument("method")
    schema.add_argument("path")
    schema.add_argument("--expected-version")

    get = sub.add_parser("get")
    get.add_argument("path")
    get.add_argument("--query", action="append", default=[])

    listing = sub.add_parser("list")
    listing.add_argument("path")
    listing.add_argument("--query", action="append", default=[])
    listing.add_argument("--cap", type=int, default=DEFAULT_PAGE_CAP)

    preview = sub.add_parser("preview")
    preview.add_argument("method")
    preview.add_argument("path")
    preview.add_argument("--payload-file", required=True)
    preview.add_argument("--expected-version")
    preview.add_argument("--reconcile-file")

    apply = sub.add_parser("apply")
    apply.add_argument("method")
    apply.add_argument("path")
    apply.add_argument("--payload-file", required=True)
    apply.add_argument("--confirm", required=True)
    apply.add_argument("--expected-version")
    apply.add_argument("--reconcile-file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = WgerClient(timeout=args.timeout, max_retries=args.max_retries)
        if args.command == "preflight":
            output = client.preflight()
        elif args.command == "schema-check":
            client.check_schema(args.path, args.method, expected_version=args.expected_version)
            output = {"schema": "compatible", "method": args.method.upper(), "path": args.path}
        elif args.command == "get":
            client.check_schema(args.path, "GET")
            output = client.get_json(args.path, query=_parse_query(args.query))
        elif args.command == "list":
            output = client.list_all(args.path, query=_parse_query(args.query), cap=args.cap)
        elif args.command == "preview":
            reconcile = _load_json_object(args.reconcile_file) if args.reconcile_file else None
            output = client.preview(
                args.method,
                args.path,
                _load_json_object(args.payload_file),
                expected_version=args.expected_version,
                reconcile=reconcile,
            )
        else:
            reconcile = _load_json_object(args.reconcile_file) if args.reconcile_file else None
            output = client.mutate(
                args.method,
                args.path,
                _load_json_object(args.payload_file),
                confirm=args.confirm,
                expected_version=args.expected_version,
                reconcile=reconcile,
            )
        json.dump(output, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except (WgerError, OSError, ValueError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, WgerError) else "input_error"
        status = exc.status if isinstance(exc, WgerError) else None
        json.dump({"error": code, "status": status, "message": str(exc)}, sys.stderr)
        sys.stderr.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
