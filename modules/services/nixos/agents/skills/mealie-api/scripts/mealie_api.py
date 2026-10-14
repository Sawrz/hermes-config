#!/usr/bin/env python3
"""Fail-closed interactive client for a profile-private Mealie API binding."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_ENDPOINT_FILE = Path("/run/hermes-credentials/services/mealie/endpoint")
DEFAULT_CREDENTIAL_FILE = Path("/run/hermes-credentials/services/mealie/credential")
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_PAGE_CAP = 1000
DEFAULT_RECONCILE_CAP = 100
DEFAULT_LOCK_ROOT = (
    Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "locks" / "mealie-api"
)


class MealieError(RuntimeError):
    code = "mealie_error"
    retryable = False
    indeterminate = False

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class RuntimeContractError(MealieError):
    code = "runtime_contract"


class ConnectivityError(MealieError):
    code = "connectivity"
    retryable = True


class AuthenticationError(MealieError):
    code = "authentication"


class AuthorizationError(MealieError):
    code = "authorization"


class NotFoundError(MealieError):
    code = "not_found"


class ValidationError(MealieError):
    code = "validation"


class ConflictError(MealieError):
    code = "conflict"


class RateLimitError(MealieError):
    code = "rate_limit"
    retryable = True


class ServerError(MealieError):
    code = "server"
    retryable = True


class ResponseFormatError(MealieError):
    code = "response_format"


class SchemaDriftError(MealieError):
    code = "schema_drift"


class VersionDriftError(MealieError):
    code = "version_drift"


class PaginationError(MealieError):
    code = "pagination"


class OriginViolationError(MealieError):
    code = "origin_violation"


class ConfirmationRequiredError(MealieError):
    code = "confirmation_required"


class UnsupportedOperationError(MealieError):
    code = "unsupported_operation"


class AuthorityError(MealieError):
    code = "authority"


class ReadbackError(MealieError):
    code = "readback"


class IndeterminateWriteError(MealieError):
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
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


def _loopback(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"}


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, client: MealieClient):
        self.client = client
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        self.client._validate_url(target)
        if req.get_method() not in {"GET", "HEAD"}:
            raise OriginViolationError("write redirects are forbidden")
        return super().redirect_request(req, fp, code, msg, headers, target)


class MealieClient:
    """Small synchronous client for deliberate interactive Mealie operations."""

    def __init__(
        self,
        *,
        endpoint_file: Path | str = DEFAULT_ENDPOINT_FILE,
        credential_file: Path | str = DEFAULT_CREDENTIAL_FILE,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff: float = 0.25,
        lock_root: Path | str = DEFAULT_LOCK_ROOT,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_retries < 0 or max_retries > 5:
            raise ValueError("max_retries must be between 0 and 5")
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff = max(0.0, float(backoff))
        self.lock_root = Path(lock_root)
        self.endpoint = _secure_read(Path(endpoint_file), "endpoint").rstrip("/")
        self._credential = _secure_read(Path(credential_file), "credential")

        parts = urlsplit(self.endpoint)
        if (
            parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise RuntimeContractError(
                "endpoint must be the Mealie origin without credentials, query, fragment, or path"
            )
        if not parts.hostname or (
            parts.scheme != "https" and not (parts.scheme == "http" and _loopback(parts.hostname))
        ):
            raise RuntimeContractError("endpoint must use HTTPS (HTTP is test-only on loopback)")
        self._origin = _origin(parts)
        self._opener = build_opener(_SafeRedirectHandler(self))

    @staticmethod
    def _stable_reconcile_match(contract: Mapping[str, Any]) -> dict[str, Any]:
        match = contract.get("match")
        if not isinstance(match, Mapping) or not match:
            raise UnsupportedOperationError("reconcile match must be a non-empty JSON object")
        if contract.get("path") in {
            "/api/recipes",
            "/api/households/shopping/lists",
        }:
            name = match.get("name")
            if not isinstance(name, str) or not name:
                raise UnsupportedOperationError(
                    "recipe and shopping-list reconciliation requires a non-empty name"
                )
            return {"name": name}
        return dict(match)

    @contextmanager
    def _mutation_lock(
        self,
        _path: str,
        reconcile: Mapping[str, Any] | None,
    ) -> Iterator[None]:
        if (
            reconcile is None
            or set(reconcile) != {"path", "query", "match"}
            or not isinstance(reconcile.get("match"), Mapping)
            or not reconcile["match"]
        ):
            raise UnsupportedOperationError("POST mutation locking requires exact reconciliation")
        identity = json.dumps(
            {
                "collection": reconcile["path"],
                "match": self._stable_reconcile_match(reconcile),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        lock_name = hashlib.sha256(identity).hexdigest() + ".lock"
        self.lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_stat = self.lock_root.lstat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or stat.S_IMODE(root_stat.st_mode) & 0o077
        ):
            raise RuntimeContractError("Mealie lock root must be an owner-private directory")
        lock_path = self.lock_root / lock_name
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        try:
            lock_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.getuid()
                or lock_stat.st_nlink != 1
                or stat.S_IMODE(lock_stat.st_mode) & 0o177
            ):
                raise RuntimeContractError(
                    "Mealie mutation lock is not an owner-private regular file"
                )
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
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
        if any(segment in {".", ".."} for segment in parts.path.split("/")) or "//" in parts.path:
            raise OriginViolationError("dot segments or repeated separators are forbidden")
        if (
            parts.path != "/openapi.json"
            and parts.path != "/api"
            and not parts.path.startswith("/api/")
        ):
            raise OriginViolationError("URL escaped the Mealie API root")
        return url

    def _url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        if urlsplit(path).scheme:
            url = self._validate_url(path)
        else:
            if not path.startswith("/") or path.startswith("//"):
                raise OriginViolationError("API paths must be absolute")
            url = self._validate_url(self.endpoint + path)
        if query:
            parts = urlsplit(url)
            if parts.query:
                raise OriginViolationError("query must be supplied separately")
            url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), "")
            )
        return url

    def _error_for(self, status: int) -> type[MealieError]:
        if status in ERROR_BY_STATUS:
            return ERROR_BY_STATUS[status]
        return ServerError if 500 <= status <= 599 else MealieError

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
        headers = {"Accept": "application/json", "User-Agent": "hermes-managed-mealie-api/1"}
        if authenticate:
            headers["Authorization"] = f"Bearer {self._credential}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        retryable_method = method in {"GET", "HEAD"}
        for attempt in range(self.max_retries + 1):
            try:
                response = self._opener.open(
                    Request(url, data=body, headers=headers, method=method), timeout=self.timeout
                )
                raw = response.read()
                status = response.status
                response_headers = dict(response.headers.items())
                break
            except OriginViolationError as exc:
                if not retryable_method:
                    raise IndeterminateWriteError(
                        "write received an unsafe redirect after dispatch; reconcile before retrying"
                    ) from exc
                raise
            except HTTPError as exc:
                error_type = self._error_for(exc.code)
                if (
                    retryable_method
                    and (exc.code == 429 or 500 <= exc.code <= 599)
                    and attempt < self.max_retries
                ):
                    time.sleep(self.backoff * (2**attempt))
                    continue
                if not retryable_method and 500 <= exc.code <= 599:
                    raise IndeterminateWriteError(
                        "write received a server failure after dispatch; reconcile before retrying",
                        status=exc.code,
                    ) from exc
                raise error_type(f"Mealie returned HTTP {exc.code}", status=exc.code) from exc
            except (TimeoutError, URLError, OSError) as exc:
                if retryable_method and attempt < self.max_retries:
                    time.sleep(self.backoff * (2**attempt))
                    continue
                if not retryable_method:
                    raise IndeterminateWriteError(
                        "write transport failed after dispatch; reconcile before retrying"
                    ) from exc
                raise ConnectivityError("Mealie request failed at the transport boundary") from exc
        else:
            raise ConnectivityError("Mealie request exhausted retries")
        if not raw:
            data: Any = None
        else:
            try:
                data = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if not retryable_method:
                    raise IndeterminateWriteError(
                        "write returned malformed data after dispatch; reconcile before retrying"
                    ) from exc
                raise ResponseFormatError("Mealie returned a non-JSON response") from exc
        return status, data, response_headers

    def get_json(self, path: str, *, query: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", path, query=query)[1]

    def preflight(self) -> dict[str, Any]:
        status, _body, _headers = self._request("GET", "/api/users/self")
        return {"status": status, "authenticated": status == 200}

    @staticmethod
    def _operation(schema: Mapping[str, Any], path: str, method: str) -> Mapping[str, Any] | None:
        item = schema.get("paths", {}).get(path)
        if isinstance(item, dict) and isinstance(item.get(method.lower()), dict):
            return item[method.lower()]
        for template, candidate in schema.get("paths", {}).items():
            pattern = "".join(
                "[^/]+" if part.startswith("{") else re.escape(part)
                for part in re.split(r"(\{[^/{}]+\})", template)
            )
            if (
                re.fullmatch(pattern, path)
                and isinstance(candidate, dict)
                and isinstance(candidate.get(method.lower()), dict)
            ):
                return candidate[method.lower()]
        return None

    def check_schema(
        self, method: str, path: str, *, expected_version: str | None = None
    ) -> Mapping[str, Any]:
        schema = self._request("GET", "/openapi.json", authenticate=False)[1]
        if not isinstance(schema, dict) or not str(schema.get("openapi", "")).startswith("3."):
            raise SchemaDriftError("live Mealie schema is not OpenAPI 3.x")
        version = (
            schema.get("info", {}).get("version") if isinstance(schema.get("info"), dict) else None
        )
        if not isinstance(version, str) or not version:
            raise SchemaDriftError("live Mealie schema has no application version")
        if expected_version is not None and version != expected_version:
            raise VersionDriftError(
                f"live Mealie version differs from required version {expected_version!r}"
            )
        operation = self._operation(schema, path, method)
        if not isinstance(operation, dict):
            raise SchemaDriftError(f"live schema does not expose {method.upper()} {path}")
        if method.upper() in {"POST", "PUT", "PATCH"} and "application/json" not in operation.get(
            "requestBody", {}
        ).get("content", {}):
            raise SchemaDriftError("live write operation does not accept application/json")
        return schema

    @staticmethod
    def _request_validator(schema: Mapping[str, Any], request_schema: Mapping[str, Any]):
        # Import only for writes: --help and read-only operations need no validator.
        from jsonschema import Draft202012Validator, FormatChecker
        from jsonschema.exceptions import SchemaError
        from referencing import Registry
        from uuid import UUID

        checker = FormatChecker(formats=["date", "date-time", "uuid"])

        @checker.checks("uuid4", raises=ValueError)
        def is_uuid4(value):
            return not isinstance(value, str) or UUID(value).version == 4

        annotations = {
            "default",
            "deprecated",
            "description",
            "example",
            "examples",
            "readOnly",
            "title",
            "writeOnly",
        }
        supported = annotations | {
            "$ref",
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "allOf",
            "anyOf",
            "oneOf",
            "enum",
            "const",
            "format",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minLength",
            "maxLength",
            "pattern",
            "minItems",
            "maxItems",
            "uniqueItems",
            "minProperties",
            "maxProperties",
        }
        component_root = schema.get("components", {})
        components = (
            component_root.get("schemas", {}) if isinstance(component_root, Mapping) else {}
        )
        resolved: dict[str, Any] = {}
        count = 0

        def visit(node, depth=0):
            nonlocal count
            count += 1
            if depth > 64 or count > 4096:
                raise SchemaDriftError("write schema exceeds validation limits")
            if isinstance(node, bool):
                return
            if not isinstance(node, Mapping) or set(node) - supported:
                raise SchemaDriftError("write schema contains unsupported keywords")
            if "format" in node and node["format"] not in checker.checkers:
                raise SchemaDriftError("write schema contains an unsupported format")
            if "$ref" in node:
                reference = node["$ref"]
                prefix = "#/components/schemas/"
                if (
                    not isinstance(reference, str)
                    or not reference.startswith(prefix)
                    or "/" in reference[len(prefix) :]
                    or re.search(r"~(?![01])", reference)
                ):
                    raise SchemaDriftError("write schema requires a local component reference")
                name = reference[len(prefix) :].replace("~1", "/").replace("~0", "~")
                if not isinstance(components, Mapping) or name not in components:
                    raise SchemaDriftError("write schema reference is missing")
                if str(schema.get("openapi", "")).startswith("3.0.") and (
                    set(node) - annotations - {"$ref"}
                ):
                    raise SchemaDriftError("OpenAPI 3.0 reference constraints are unsupported")
                if name not in resolved:
                    # Mark before visiting so recursive recipe/ingredient models terminate.
                    Draft202012Validator.check_schema(components[name])
                    resolved[name] = components[name]
                    visit(components[name], depth + 1)
            for child in node.get("properties", {}).values():
                visit(child, depth + 1)
            for keyword in ("items", "additionalProperties"):
                if keyword in node:
                    visit(node[keyword], depth + 1)
            for keyword in ("allOf", "anyOf", "oneOf"):
                for child in node.get(keyword, []):
                    visit(child, depth + 1)

        def declared_fields(node, seen):
            if not isinstance(node, Mapping):
                return set()
            fields = set(node.get("properties", {}))
            reference = node.get("$ref")
            if reference is not None and reference not in seen:
                name = (
                    reference[len("#/components/schemas/") :].replace("~1", "/").replace("~0", "~")
                )
                seen.add(reference)
                fields.update(declared_fields(resolved[name], seen))
            for keyword in ("allOf", "anyOf", "oneOf"):
                for child in node.get(keyword, []):
                    fields.update(declared_fields(child, seen))
            return fields

        try:
            # Check the root first so malformed keyword values never reach traversal.
            Draft202012Validator.check_schema(request_schema)
            # Referenced schemas are checked separately; components is OpenAPI metadata.
            visit(request_schema)
            fields = declared_fields(request_schema, set())
            if not fields:
                raise SchemaDriftError("write schema must declare supported payload fields")
            document = {
                "type": "object",
                "allOf": [request_schema],
                "unevaluatedProperties": False,
                "components": {"schemas": resolved},
            }
            # An empty registry has no network retrieval. Only checked local refs exist.
            return Draft202012Validator(
                document, registry=Registry(), format_checker=checker
            ), fields
        except (SchemaError, TypeError, AttributeError, RecursionError) as exc:
            raise SchemaDriftError(
                "live write schema is malformed or exceeds validation limits"
            ) from exc

    def _validate_payload_schema(
        self,
        schema: Mapping[str, Any],
        method: str,
        path: str,
        payload: Mapping[str, Any],
    ) -> None:
        from jsonschema.exceptions import ValidationError as JsonValidationError
        from referencing.exceptions import Unresolvable

        operation = self._operation(schema, path, method)
        content = operation.get("requestBody", {}).get("content", {}) if operation else {}
        media = content.get("application/json") if isinstance(content, Mapping) else None
        request_schema = media.get("schema") if isinstance(media, Mapping) else None
        if not isinstance(request_schema, Mapping):
            raise SchemaDriftError("live write operation has no JSON request schema")
        validator, fields = self._request_validator(schema, request_schema)
        if set(payload) - fields:
            raise ValidationError(
                "write payload contains fields not declared by the live operation"
            )
        try:
            validator.validate(payload)
        except JsonValidationError:
            # Library messages include payload values; keep private recipe data out of errors.
            raise ValidationError(
                "write payload does not satisfy the live operation schema"
            ) from None
        except (Unresolvable, RecursionError) as exc:
            raise SchemaDriftError(
                "write schema reference cannot be validated within limits"
            ) from exc

    def list_all(
        self, path: str, *, query: Mapping[str, Any] | None = None, cap: int = DEFAULT_PAGE_CAP
    ) -> list[Any]:
        if cap < 1:
            raise ValueError("cap must be positive")
        self.check_schema("GET", path)
        collection_path = urlsplit(path).path
        next_url = self._url(path, query)
        seen: set[str] = set()
        records: list[Any] = []
        while next_url:
            if next_url in seen:
                raise PaginationError("pagination loop detected")
            seen.add(next_url)
            page = self.get_json(next_url)
            required = {"page", "per_page", "total", "total_pages", "items", "next", "previous"}
            if (
                not isinstance(page, dict)
                or not required.issubset(page)
                or not isinstance(page["items"], list)
            ):
                raise PaginationError("Mealie page has an unsupported shape")
            if len(records) + len(page["items"]) > cap:
                raise PaginationError(f"pagination cap {cap} would be exceeded")
            records.extend(page["items"])
            raw_next = page["next"]
            if raw_next is None:
                next_url = ""
            elif not isinstance(raw_next, str):
                raise PaginationError("pagination next is neither a path nor null")
            else:
                raw_parts = urlsplit(raw_next)
                if raw_parts.scheme or raw_parts.netloc:
                    validated_parts = urlsplit(self._url(raw_next))
                    normalized_path = validated_parts.path
                    next_query = validated_parts.query
                else:
                    normalized_path = raw_parts.path
                    next_query = raw_parts.query
                if not normalized_path.startswith("/api/"):
                    normalized_path = "/api" + normalized_path
                if normalized_path != collection_path:
                    raise PaginationError("pagination next changed collection")
                normalized_next = urlunsplit(("", "", normalized_path, next_query, ""))
                next_url = self._url(normalized_next)
        return records

    @staticmethod
    def _write_domain(method: str, path: str) -> str:
        if method == "POST" and path == "/api/recipes":
            return "recipes"
        if method in {"PUT", "PATCH"} and re.fullmatch(r"/api/recipes/[^/]+", path):
            return "recipes"
        if method == "POST" and path == "/api/households/mealplans":
            return "meal_plans"
        if method in {"PUT", "PATCH"} and re.fullmatch(r"/api/households/mealplans/[^/]+", path):
            return "meal_plans"
        if method == "POST" and path in {
            "/api/households/shopping/lists",
            "/api/households/shopping/items",
        }:
            return "shopping"
        if method == "PUT" and (
            re.fullmatch(r"/api/households/shopping/lists/[^/]+", path)
            or re.fullmatch(r"/api/households/shopping/items/[^/]+", path)
        ):
            return "shopping"
        raise UnsupportedOperationError(
            "write path is outside the managed recipe, meal-plan, and shopping contract"
        )

    @staticmethod
    def _snapshot_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
        try:
            snapshot = json.loads(
                json.dumps(
                    value,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label} must be canonical JSON") from exc
        if not isinstance(snapshot, dict):
            raise ValidationError(f"{label} must be a JSON object")
        return snapshot

    @staticmethod
    def _confirmation(
        method: str,
        path: str,
        payload: Mapping[str, Any],
        authority: str,
        version: str,
        reconcile: Mapping[str, Any] | None,
    ) -> str:
        approved = {"payload": payload, "reconcile": reconcile}
        canonical = json.dumps(approved, separators=(",", ":"), sort_keys=True).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        return f"APPLY {authority.upper()} {method} {path} VERSION {version} SHA256 {digest}"

    def preview(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        *,
        authority: str,
        expected_version: str | None = None,
        reconcile: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        if (
            method not in {"POST", "PUT", "PATCH"}
            or not isinstance(payload, Mapping)
            or not payload
        ):
            raise UnsupportedOperationError(
                "only non-empty JSON POST, PUT, and PATCH writes are supported"
            )
        payload_snapshot = self._snapshot_mapping(payload, "payload")
        reconcile_snapshot = (
            self._snapshot_mapping(reconcile, "reconcile") if reconcile is not None else None
        )
        domain = self._write_domain(method, path)
        expected_authority = f"mealie:{domain}"
        if authority != expected_authority:
            raise AuthorityError(
                f"write requires explicit current authority {expected_authority!r}"
            )

        schema = self.check_schema(method, path, expected_version=expected_version)
        self._validate_payload_schema(schema, method, path, payload_snapshot)
        if method == "POST":
            self._validate_reconcile(path, domain, payload_snapshot, reconcile_snapshot)
        elif reconcile_snapshot is not None:
            raise UnsupportedOperationError("reconciliation is supported only for POST")
        version = schema["info"]["version"]
        return {
            "service": "mealie",
            "domain": domain,
            "version": version,
            "method": method,
            "path": path,
            "payload": payload_snapshot,
            "reconcile": reconcile_snapshot,
            "confirmation": self._confirmation(
                method, path, payload_snapshot, authority, version, reconcile_snapshot
            ),
        }

    @staticmethod
    def _reconcile_query(contract: Mapping[str, Any]) -> dict[str, Any]:
        query = contract.get("query")
        if not isinstance(query, Mapping):
            raise UnsupportedOperationError("reconcile query and match must be JSON objects")
        match = MealieClient._stable_reconcile_match(contract)
        derived: dict[str, Any] = {}
        name = match.get("name")
        if (
            contract.get("path") == "/api/recipes"
            and isinstance(name, str)
            and name
            and '"' not in name
            and "\\" not in name
        ):
            derived = {"queryFilter": f'name = "{name}"'}
        return derived

    @staticmethod
    def _validate_reconcile(
        path: str,
        domain: str,
        payload: Mapping[str, Any],
        contract: Mapping[str, Any] | None,
    ) -> None:
        if contract is None:
            raise UnsupportedOperationError(
                "POST requires an exact bounded reconciliation contract"
            )
        if (
            set(contract) != {"path", "query", "match"}
            or not isinstance(contract.get("match"), Mapping)
            or not contract["match"]
        ):
            raise UnsupportedOperationError(
                "reconcile requires exactly path, query, and non-empty match"
            )
        if domain == "recipes":
            expected_path = "/api/recipes"
        elif domain == "meal_plans":
            expected_path = "/api/households/mealplans"
        elif path == "/api/households/shopping/lists":
            expected_path = "/api/households/shopping/lists"
        elif path == "/api/households/shopping/items":
            expected_path = "/api/households/shopping/items"
        else:
            raise UnsupportedOperationError("shopping reconciliation requires a managed collection")
        if contract.get("path") != expected_path:
            raise UnsupportedOperationError(f"reconciliation path must be {expected_path}")
        stable_match = MealieClient._stable_reconcile_match(contract)
        if path in {"/api/recipes", "/api/households/shopping/lists"}:
            if payload.get("name") != stable_match["name"]:
                raise UnsupportedOperationError(
                    "reconciliation name must equal the write payload name"
                )
        elif dict(contract["match"]) != dict(payload):
            raise UnsupportedOperationError(
                "reconciliation match must equal the write payload for this collection"
            )
        MealieClient._reconcile_query(contract)

    def _reconcile(self, contract: Mapping[str, Any] | None) -> Any | None:
        if contract is None:
            return None
        if (
            set(contract) != {"path", "query", "match"}
            or not isinstance(contract.get("match"), Mapping)
            or not contract["match"]
        ):
            raise UnsupportedOperationError(
                "reconcile requires exactly path, query, and non-empty match"
            )
        effective_query = self._reconcile_query(contract)
        try:
            candidates = self.list_all(
                str(contract["path"]),
                query=effective_query,
                cap=DEFAULT_RECONCILE_CAP,
            )
        except PaginationError as exc:
            raise IndeterminateWriteError("reconciliation exceeded the bounded match set") from exc
        stable_match = self._stable_reconcile_match(contract)
        exact = [
            item
            for item in candidates
            if isinstance(item, dict)
            and all(k in item and item[k] == v for k, v in stable_match.items())
        ]
        if len(exact) > 1:
            raise IndeterminateWriteError("reconciliation found multiple matching resources")
        return exact[0] if exact else None

    @staticmethod
    def _verify_readback(record: Any, payload: Mapping[str, Any]) -> Any:
        def matches(actual, expected):
            if isinstance(expected, dict):
                return isinstance(actual, dict) and all(
                    key in actual and matches(actual[key], value) for key, value in expected.items()
                )
            if isinstance(expected, list):
                return (
                    isinstance(actual, list)
                    and len(actual) == len(expected)
                    and all(matches(item, wanted) for item, wanted in zip(actual, expected))
                )
            # JSON booleans must not compare equal to numeric 0 or 1.
            if isinstance(actual, bool) != isinstance(expected, bool):
                return False
            return actual == expected

        if not isinstance(record, dict) or not payload:
            raise ReadbackError("readback is not a verifiable JSON object")
        if not matches(record, payload):
            raise ReadbackError("readback does not match the requested fields")
        return record

    def _readback_after_write(
        self,
        method: str,
        path: str,
        data: Any,
        headers: Mapping[str, str],
        reconcile: Mapping[str, Any] | None,
        expected_readback: Mapping[str, Any],
    ) -> Any:
        if method in {"PUT", "PATCH"}:
            readback_path = path
        else:
            location = headers.get("Location") or headers.get("location")
            if location:
                readback_path = self._validate_url(urljoin(self.endpoint + "/", location))
            elif isinstance(data, dict) and data.get("slug"):
                readback_path = f"/api/recipes/{data['slug']}"
            else:
                existing = self._reconcile(reconcile)
                if existing is None:
                    raise ReadbackError("write response has no safe readback identity")
                return self._verify_readback(existing, expected_readback)
        return self._verify_readback(self.get_json(readback_path), expected_readback)

    def mutate(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        *,
        authority: str,
        confirm: str,
        expected_version: str | None = None,
        reconcile: Mapping[str, Any] | None = None,
    ) -> Any:
        preview = self.preview(
            method,
            path,
            payload,
            authority=authority,
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
        expected_readback = approved_payload
        lock = self._mutation_lock(path, approved_reconcile) if method == "POST" else nullcontext()
        with lock:
            if method == "POST":
                existing = self._reconcile(approved_reconcile)
                if existing is not None:
                    return self._verify_readback(existing, expected_readback)
            try:
                _status, data, headers = self._request(method, path, payload=approved_payload)
            except IndeterminateWriteError as original:
                try:
                    existing = self._reconcile(approved_reconcile)
                    if existing is not None:
                        return self._verify_readback(existing, expected_readback)
                except MealieError:
                    pass
                raise original
            try:
                return self._readback_after_write(
                    method, path, data, headers, approved_reconcile, expected_readback
                )
            except IndeterminateWriteError:
                raise
            except MealieError as exc:
                raise IndeterminateWriteError(
                    "write completed but mandatory readback failed; do not replay"
                ) from exc


def _load_object(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
    listing = sub.add_parser("list")
    listing.add_argument("path")
    listing.add_argument("--cap", type=int, default=DEFAULT_PAGE_CAP)
    for name in ("preview", "apply"):
        command = sub.add_parser(name)
        command.add_argument("method")
        command.add_argument("path")
        command.add_argument("--payload-file", required=True)
        command.add_argument("--authority", required=True)
        command.add_argument("--expected-version")
        command.add_argument("--reconcile-file")
        if name == "apply":
            command.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = MealieClient(timeout=args.timeout, max_retries=args.max_retries)
        if args.command == "preflight":
            output = client.preflight()
        elif args.command == "schema-check":
            client.check_schema(args.method, args.path, expected_version=args.expected_version)
            output = {"schema": "compatible", "method": args.method.upper(), "path": args.path}
        elif args.command == "get":
            client.check_schema("GET", args.path)
            output = client.get_json(args.path)
        elif args.command == "list":
            output = client.list_all(args.path, cap=args.cap)
        elif args.command == "preview":
            output = client.preview(
                args.method,
                args.path,
                _load_object(args.payload_file),
                authority=args.authority,
                expected_version=args.expected_version,
                reconcile=_load_object(args.reconcile_file) if args.reconcile_file else None,
            )
        else:
            output = client.mutate(
                args.method,
                args.path,
                _load_object(args.payload_file),
                authority=args.authority,
                confirm=args.confirm,
                expected_version=args.expected_version,
                reconcile=_load_object(args.reconcile_file) if args.reconcile_file else None,
            )
        json.dump(output, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except (MealieError, OSError, ValueError, json.JSONDecodeError) as exc:
        json.dump(
            {
                "error": exc.code if isinstance(exc, MealieError) else "input_error",
                "status": exc.status if isinstance(exc, MealieError) else None,
                "message": str(exc),
            },
            sys.stderr,
        )
        sys.stderr.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
