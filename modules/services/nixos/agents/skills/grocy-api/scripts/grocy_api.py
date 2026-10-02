"""Fail-closed interactive client for a profile-private Grocy API binding."""

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
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_ENDPOINT_FILE = Path("/run/hermes-credentials/services/grocy/endpoint")
DEFAULT_CREDENTIAL_FILE = Path("/run/hermes-credentials/services/grocy/credential")
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_PAGE_CAP = 1000
DEFAULT_PAGE_SIZE = 100
DEFAULT_LOCK_ROOT = (
    Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "locks" / "grocy-api"
)


class GrocyError(RuntimeError):
    code = "grocy_error"
    retryable = False
    indeterminate = False

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class RuntimeContractError(GrocyError):
    code = "runtime_contract"


class ConnectivityError(GrocyError):
    code = "connectivity"
    retryable = True


class AuthenticationError(GrocyError):
    code = "authentication"


class AuthorizationError(GrocyError):
    code = "authorization"


class NotFoundError(GrocyError):
    code = "not_found"


class ValidationError(GrocyError):
    code = "validation"


class ConflictError(GrocyError):
    code = "conflict"


class RateLimitError(GrocyError):
    code = "rate_limit"
    retryable = True


class ServerError(GrocyError):
    code = "server"
    retryable = True


class ResponseFormatError(GrocyError):
    code = "response_format"


class SchemaDriftError(GrocyError):
    code = "schema_drift"


class VersionDriftError(GrocyError):
    code = "version_drift"


class PaginationError(GrocyError):
    code = "pagination"


class OriginViolationError(GrocyError):
    code = "origin_violation"


class ConfirmationRequiredError(GrocyError):
    code = "confirmation_required"


class UnsupportedOperationError(GrocyError):
    code = "unsupported_operation"


class AuthorityError(GrocyError):
    code = "authority"


class ReadbackError(GrocyError):
    code = "readback"


class IndeterminateWriteError(GrocyError):
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
    def __init__(self, client: GrocyClient):
        self.client = client
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        self.client._validate_url(target)
        if req.get_method() not in {"GET", "HEAD"}:
            raise OriginViolationError("write redirects are forbidden")
        return super().redirect_request(req, fp, code, msg, headers, target)


class GrocyClient:
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
            or parts.path.rstrip("/") != "/api"
        ):
            raise RuntimeContractError("endpoint must be the canonical Grocy /api root")
        if not parts.hostname or (
            parts.scheme != "https" and not (parts.scheme == "http" and _loopback(parts.hostname))
        ):
            raise RuntimeContractError("endpoint must use HTTPS (HTTP is test-only on loopback)")
        self._origin = _origin(parts)
        self._opener = build_opener(_SafeRedirectHandler(self))

    @staticmethod
    def _stable_reconcile_identity(
        path: str, reconcile: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        if reconcile is None:
            raise UnsupportedOperationError("POST mutation locking requires reconciliation")
        if path.startswith("/objects/"):
            match = reconcile.get("match")
            if not isinstance(match, Mapping) or not match:
                raise UnsupportedOperationError("object mutation locking requires an exact match")
            if reconcile.get("path") == "/objects/products":
                name = match.get("name")
                if not isinstance(name, str) or not name:
                    raise UnsupportedOperationError(
                        "product mutation locking requires a stable non-empty name"
                    )
                match = {"name": name}
            return {"collection": reconcile.get("path"), "match": dict(match)}
        after = reconcile.get("after")
        if not isinstance(after, Mapping) or not after:
            raise UnsupportedOperationError("action mutation locking requires exact after state")
        if re.fullmatch(r"/stock/products/([^/]+)/add", path):
            return {"collection": "/stock", "product_id": path.split("/")[3]}
        if path == "/stock/shoppinglist/add-product":
            return {
                "collection": "/objects/shopping_list",
                "product_id": after.get("product_id"),
                "shopping_list_id": after.get("shopping_list_id"),
            }
        raise UnsupportedOperationError("POST path has no stable mutation lock identity")

    @contextmanager
    def _mutation_lock(
        self,
        method: str,
        path: str,
        reconcile: Mapping[str, Any] | None,
    ) -> Iterator[None]:
        identity = (
            self._stable_reconcile_identity(path, reconcile)
            if method == "POST"
            else {"resource": path}
        )
        lock_name = (
            hashlib.sha256(
                json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()
            + ".lock"
        )
        self.lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_stat = self.lock_root.lstat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or stat.S_IMODE(root_stat.st_mode) & 0o077
        ):
            raise RuntimeContractError("Grocy lock root must be an owner-private directory")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_root / lock_name, flags, 0o600)
        try:
            lock_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.getuid()
                or lock_stat.st_nlink != 1
                or stat.S_IMODE(lock_stat.st_mode) & 0o177
            ):
                raise RuntimeContractError(
                    "Grocy mutation lock is not an owner-private regular file"
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
        if parts.path != "/api" and not parts.path.startswith("/api/"):
            raise OriginViolationError("URL escaped the Grocy /api root")
        return url

    def _url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        if urlsplit(path).scheme:
            url = self._validate_url(path)
        else:
            if not path.startswith("/") or path.startswith("//"):
                raise OriginViolationError("API paths must be absolute within /api")
            url = self._validate_url(self.endpoint + path)
        if query:
            parts = urlsplit(url)
            if parts.query:
                raise OriginViolationError("query must be supplied separately")
            url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), "")
            )
        return url

    def _error_for(self, status: int) -> type[GrocyError]:
        if status in ERROR_BY_STATUS:
            return ERROR_BY_STATUS[status]
        return ServerError if 500 <= status <= 599 else GrocyError

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
        headers = {"Accept": "application/json", "User-Agent": "hermes-managed-grocy-api/1"}
        if authenticate:
            headers["GROCY-API-KEY"] = self._credential
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
                raise error_type(f"Grocy returned HTTP {exc.code}", status=exc.code) from exc
            except (TimeoutError, URLError, OSError) as exc:
                if retryable_method and attempt < self.max_retries:
                    time.sleep(self.backoff * (2**attempt))
                    continue
                if not retryable_method:
                    raise IndeterminateWriteError(
                        "write transport failed after dispatch; reconcile before retrying"
                    ) from exc
                raise ConnectivityError("Grocy request failed at the transport boundary") from exc
        else:
            raise ConnectivityError("Grocy request exhausted retries")
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
                raise ResponseFormatError("Grocy returned a non-JSON response") from exc
        return status, data, response_headers

    def get_json(self, path: str, *, query: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", path, query=query)[1]

    def preflight(self) -> dict[str, Any]:
        status, _body, _headers = self._request("GET", "/user")
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
        schema = self._request("GET", "/openapi/specification", authenticate=False)[1]
        if not isinstance(schema, dict) or not str(schema.get("openapi", "")).startswith("3."):
            raise SchemaDriftError("live Grocy schema is not OpenAPI 3.x")
        version = (
            schema.get("info", {}).get("version") if isinstance(schema.get("info"), dict) else None
        )
        if not isinstance(version, str) or not version:
            raise SchemaDriftError("live Grocy schema has no application version")
        if expected_version is not None and version != expected_version:
            raise VersionDriftError(
                f"live Grocy version differs from required version {expected_version!r}"
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
    def _request_object_contract(
        schema: Mapping[str, Any], request_schema: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], frozenset[str]]:
        node = request_schema
        if "$ref" in node:
            if set(node) != {"$ref"}:
                raise SchemaDriftError("write schema reference siblings are unsupported")
            reference = node["$ref"]
            prefix = "#/components/schemas/"
            if not isinstance(reference, str) or not reference.startswith(prefix):
                raise SchemaDriftError("write schema contains an unsupported reference")
            name = reference.removeprefix(prefix).replace("~1", "/").replace("~0", "~")
            schemas = schema.get("components", {}).get("schemas", {})
            node = schemas.get(name) if isinstance(schemas, Mapping) else None
            if not isinstance(node, Mapping) or "$ref" in node:
                raise SchemaDriftError("write schema reference is missing or recursive")
        annotation_keywords = {
            "default",
            "deprecated",
            "description",
            "example",
            "examples",
            "readOnly",
            "title",
            "writeOnly",
        }
        allowed_root_keywords = {"type", "properties", "required", *annotation_keywords}
        if any(keyword in node for keyword in ("allOf", "anyOf", "oneOf")):
            raise SchemaDriftError("write schema composition is outside the bounded adapter")
        if set(node) - allowed_root_keywords:
            raise SchemaDriftError("write schema root exceeds the bounded object contract")
        if node.get("type") != "object":
            raise SchemaDriftError("write schema must be an explicit object")
        properties = node.get("properties")
        required = node.get("required", [])
        if not isinstance(properties, Mapping) or not properties:
            raise SchemaDriftError("write schema must declare supported payload fields")
        if (
            not isinstance(required, list)
            or any(not isinstance(field, str) for field in required)
            or len(set(required)) != len(required)
            or any(field not in properties for field in required)
            or any(not isinstance(value, Mapping) for value in properties.values())
        ):
            raise SchemaDriftError("write schema field contract is malformed")
        supported_types = {"array", "boolean", "integer", "number", "object", "string"}
        scalar_item_types = {"boolean", "integer", "number", "string"}
        for field, field_schema in properties.items():
            items = field_schema.get("items")
            field_type = field_schema.get("type")
            allowed_keywords = {"type", *annotation_keywords}
            if field_type == "array":
                allowed_keywords.add("items")
            if (
                "$ref" in field_schema
                or any(keyword in field_schema for keyword in ("allOf", "anyOf", "oneOf"))
                or field_type not in supported_types
                or bool(set(field_schema) - allowed_keywords)
                or (field_type != "array" and "items" in field_schema)
                or (
                    field_type == "array"
                    and "items" in field_schema
                    and (
                        not isinstance(items, Mapping)
                        or set(items) != {"type"}
                        or items.get("type") not in scalar_item_types
                    )
                )
            ):
                raise SchemaDriftError(
                    f"write field {field!r} exceeds the bounded top-level contract"
                )
        return properties, frozenset(required)

    @staticmethod
    def _field_matches_contract(field: str, node: Mapping[str, Any], value: Any) -> bool:
        if "$ref" in node or any(keyword in node for keyword in ("allOf", "anyOf", "oneOf")):
            raise SchemaDriftError(
                f"write field {field!r} uses schema features outside the bounded adapter"
            )
        expected = node.get("type")
        checks = {
            "array": lambda: isinstance(value, list),
            "boolean": lambda: type(value) is bool,
            "integer": lambda: type(value) is int,
            "number": lambda: type(value) in {int, float},
            "object": lambda: isinstance(value, Mapping),
            "string": lambda: isinstance(value, str),
        }
        if expected not in checks:
            raise SchemaDriftError(f"write field {field!r} has no supported exact JSON type")
        if not checks[expected]():
            return False
        if expected != "array" or "items" not in node:
            return True
        item_type = node["items"]["type"]
        item_checks = {
            "boolean": lambda item: type(item) is bool,
            "integer": lambda item: type(item) is int,
            "number": lambda item: type(item) in {int, float},
            "string": lambda item: isinstance(item, str),
        }
        return all(item_checks[item_type](item) for item in value)

    def _validate_payload_schema(
        self,
        schema: Mapping[str, Any],
        method: str,
        path: str,
        payload: Mapping[str, Any],
    ) -> None:
        operation = self._operation(schema, path, method)
        content = operation.get("requestBody", {}).get("content", {}) if operation else {}
        media = content.get("application/json") if isinstance(content, Mapping) else None
        request_schema = media.get("schema") if isinstance(media, Mapping) else None
        if not isinstance(request_schema, Mapping):
            raise SchemaDriftError("live write operation has no JSON request schema")
        properties, required = self._request_object_contract(schema, request_schema)
        if any(field not in payload for field in required):
            raise ValidationError("write payload is missing fields required by the live operation")
        undeclared = set(payload) - set(properties)
        if undeclared:
            raise ValidationError(
                "write payload contains fields not declared by the live operation"
            )
        if any(
            not self._field_matches_contract(field, properties[field], value)
            for field, value in payload.items()
        ):
            raise ValidationError("write payload field type differs from the live operation")

    def list_all(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        cap: int = DEFAULT_PAGE_CAP,
    ) -> list[Any]:
        if cap < 1 or page_size < 1:
            raise ValueError("page_size and cap must be positive")
        schema = self.check_schema("GET", path)
        operation = self._operation(schema, path, "GET") or {}
        parameter_names = {
            item.get("name") for item in operation.get("parameters", []) if isinstance(item, dict)
        }
        paginated = {"limit", "offset"}.issubset(parameter_names)
        if not paginated:
            value = self.get_json(path, query=query)
            if not isinstance(value, list):
                raise PaginationError("Grocy list response is not an array")
            if len(value) > cap:
                raise PaginationError(f"record cap {cap} would be exceeded")
            return value
        records: list[Any] = []
        offset = 0
        while True:
            page_query = dict(query or {})
            page_query.update({"limit": page_size, "offset": offset})
            page = self.get_json(path, query=page_query)
            if not isinstance(page, list):
                raise PaginationError("Grocy paginated response is not an array")
            if len(records) + len(page) > cap:
                raise PaginationError(f"pagination cap {cap} would be exceeded")
            records.extend(page)
            if len(page) < page_size:
                return records
            if not page:
                return records
            offset += len(page)
            if offset >= cap:
                probe = self.get_json(
                    path, query={**dict(query or {}), "limit": 1, "offset": offset}
                )
                if probe:
                    raise PaginationError(f"pagination cap {cap} would be exceeded")
                return records

    @staticmethod
    def _write_domain(method: str, path: str) -> str:
        pantry_entities = {
            "products",
            "locations",
            "quantity_units",
            "quantity_unit_conversions",
            "product_barcodes",
        }
        object_match = re.fullmatch(r"/objects/([^/]+)(?:/[^/]+)?", path)
        if object_match and method in {"POST", "PUT", "PATCH"}:
            entity = object_match.group(1)
            if entity in pantry_entities:
                return "pantry"
            if entity.startswith("shopping_"):
                return "shopping"
        if method == "POST" and re.fullmatch(r"/stock/products/[^/]+/add", path):
            return "pantry"
        if method == "POST" and path == "/stock/shoppinglist/add-product":
            return "shopping"
        raise UnsupportedOperationError(
            "write path is outside the managed pantry and shopping contract"
        )

    @staticmethod
    def _snapshot_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
        try:
            snapshot = json.loads(
                json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
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
        expected_authority = f"grocy:{domain}"
        if authority != expected_authority:
            raise AuthorityError(
                f"write requires explicit current authority {expected_authority!r}"
            )

        schema = self.check_schema(method, path, expected_version=expected_version)
        self._validate_payload_schema(schema, method, path, payload_snapshot)
        if method == "POST":
            self._validate_reconcile(path, payload_snapshot, reconcile_snapshot)
        elif reconcile_snapshot is not None:
            raise UnsupportedOperationError("reconciliation is supported only for POST")
        version = schema["info"]["version"]
        return {
            "service": "grocy",
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
    def _validate_reconcile(
        path: str, payload: Mapping[str, Any], contract: Mapping[str, Any] | None
    ) -> None:
        if contract is None:
            raise UnsupportedOperationError(
                "POST requires an exact bounded reconciliation contract"
            )
        contract_path = contract.get("path")
        if path.startswith("/objects/"):
            if (
                set(contract) != {"path", "query", "match"}
                or not isinstance(contract.get("match"), Mapping)
                or not contract["match"]
            ):
                raise UnsupportedOperationError(
                    "generic object reconciliation requires exactly path, query, and non-empty match"
                )
            if contract_path != path:
                raise UnsupportedOperationError(f"reconciliation path must be {path}")
            if any(contract["match"].get(key) != value for key, value in payload.items()):
                raise UnsupportedOperationError(
                    "generic object reconciliation match must include every requested payload field"
                )
            return

        if (
            set(contract) != {"path", "query", "before", "after"}
            or not isinstance(contract.get("query"), Mapping)
            or not contract["query"]
        ):
            raise UnsupportedOperationError(
                "stock and shopping reconciliation requires exactly path, non-empty query, before, and after"
            )
        before = contract.get("before")
        after = contract.get("after")
        if before is not None and (not isinstance(before, Mapping) or not before):
            raise UnsupportedOperationError(
                "action before state must be null or a non-empty exact object"
            )
        if not isinstance(after, Mapping) or not after:
            raise UnsupportedOperationError("action after state must be a non-empty exact object")
        if re.fullmatch(r"/stock/products/([^/]+)/add", path):
            expected_path = "/stock"
            product_id = path.split("/")[3]
            if set(payload) != {"amount"} or not isinstance(before, Mapping):
                raise UnsupportedOperationError(
                    "stock add supports only amount and requires an exact before state"
                )
            if str(after.get("product_id")) != product_id or (
                isinstance(before, Mapping) and str(before.get("product_id")) != product_id
            ):
                raise UnsupportedOperationError(
                    "stock before/after state must match the product_id from the write path"
                )
            try:
                requested_amount = Decimal(str(payload["amount"]))
                if requested_amount <= 0:
                    raise UnsupportedOperationError("stock add amount must be positive")
                if (
                    Decimal(str(after.get("amount")))
                    != Decimal(str(before.get("amount"))) + requested_amount
                ):
                    raise UnsupportedOperationError(
                        "stock after amount must equal before amount plus the requested amount"
                    )
            except InvalidOperation as exc:
                raise UnsupportedOperationError(
                    "stock before, requested, and after amounts must be numeric"
                ) from exc
        elif path == "/stock/shoppinglist/add-product":
            expected_path = "/objects/shopping_list"
            if set(payload) not in (
                {"product_id", "amount"},
                {"product_id", "amount", "shopping_list_id"},
            ):
                raise UnsupportedOperationError(
                    "shopping add supports only product_id, amount, and optional shopping_list_id"
                )
            if str(after.get("product_id")) != str(payload.get("product_id")):
                raise UnsupportedOperationError(
                    "shopping after state must match the requested product_id"
                )
            if (
                "shopping_list_id" in payload
                and after.get("shopping_list_id") != payload["shopping_list_id"]
            ):
                raise UnsupportedOperationError(
                    "shopping after state must match the requested shopping_list_id"
                )
            if isinstance(before, Mapping) and any(
                before.get(key) != after.get(key)
                for key in ("product_id", "shopping_list_id")
                if key in after
            ):
                raise UnsupportedOperationError(
                    "shopping before state must describe the same list item identity"
                )
            try:
                before_amount = Decimal(0) if before is None else Decimal(str(before.get("amount")))
                requested_amount = Decimal(str(payload["amount"]))
                if requested_amount <= 0:
                    raise UnsupportedOperationError("shopping add amount must be positive")
                if Decimal(str(after.get("amount"))) != before_amount + requested_amount:
                    raise UnsupportedOperationError(
                        "shopping after amount must equal before amount plus the requested amount"
                    )
            except InvalidOperation as exc:
                raise UnsupportedOperationError(
                    "shopping before, requested, and after amounts must be numeric"
                ) from exc
        else:
            raise UnsupportedOperationError("POST path has no supported reconciliation contract")
        if contract_path != expected_path:
            raise UnsupportedOperationError(f"reconciliation path must be {expected_path}")

    @staticmethod
    def _is_action(path: str) -> bool:
        return (
            bool(re.fullmatch(r"/stock/products/[^/]+/add", path))
            or path == "/stock/shoppinglist/add-product"
        )

    @staticmethod
    def _reconcile_query(contract: Mapping[str, Any]) -> Mapping[str, Any]:
        match = contract.get("match")
        if not isinstance(match, Mapping):
            query = contract.get("query")
            if not isinstance(query, Mapping):
                raise UnsupportedOperationError("reconciliation query must be a JSON object")
            return query
        stable = (
            {"name": match["name"]}
            if contract.get("path") == "/objects/products" and isinstance(match.get("name"), str)
            else dict(match)
        )
        return {"query[]": [f"{key}={value}" for key, value in sorted(stable.items())]}

    def _candidates(self, contract: Mapping[str, Any]) -> list[Any]:
        try:
            return self.list_all(
                str(contract["path"]),
                query=self._reconcile_query(contract),
                page_size=2,
                cap=2,
            )
        except PaginationError as exc:
            raise IndeterminateWriteError("reconciliation exceeded the bounded match set") from exc

    @staticmethod
    def _exact(candidates: list[Any], expected: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            item
            for item in candidates
            if isinstance(item, dict)
            and all(k in item and item[k] == v for k, v in expected.items())
        ]

    def _reconcile(self, contract: Mapping[str, Any] | None) -> Any | None:
        if contract is None:
            return None
        candidates = self._candidates(contract)
        exact = self._exact(candidates, contract["match"])
        if len(exact) > 1:
            raise IndeterminateWriteError("reconciliation found multiple matching resources")
        return exact[0] if exact else None

    def _prepare_action(self, contract: Mapping[str, Any]) -> Any | None:
        candidates = self._candidates(contract)
        after = self._exact(candidates, contract["after"])
        if len(after) == 1:
            return after[0]
        if len(after) > 1:
            raise IndeterminateWriteError(
                "action reconciliation found multiple after-state matches"
            )
        before = contract["before"]
        if before is None:
            if candidates:
                raise ReadbackError(
                    "current action state is neither the approved absence nor after state"
                )
            return None
        before_matches = self._exact(candidates, before)
        if len(before_matches) != 1:
            raise ReadbackError("current action state does not match the approved before state")
        return None

    def _verify_action_after(self, contract: Mapping[str, Any]) -> Any:
        candidates = self._candidates(contract)
        after = self._exact(candidates, contract["after"])
        if len(after) != 1:
            raise ReadbackError("action result does not match exactly one approved after state")
        return after[0]

    @staticmethod
    def _verify_readback(record: Any, expected: Mapping[str, Any]) -> Any:
        if not isinstance(record, dict) or not expected:
            raise ReadbackError("readback is not a verifiable JSON object")
        if any(key not in record or record[key] != value for key, value in expected.items()):
            raise ReadbackError("readback does not match the requested fields")
        return record

    def _readback_after_write(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        data: Any,
        reconcile: Mapping[str, Any] | None,
        action: bool,
    ) -> Any:
        if action:
            if reconcile is None:
                raise ReadbackError("action reconciliation contract is absent")
            return self._verify_action_after(reconcile)
        if method in {"PUT", "PATCH"}:
            readback_path = path
        elif (
            path.startswith("/objects/")
            and isinstance(data, dict)
            and data.get("created_object_id") is not None
        ):
            readback_path = path.rstrip("/") + f"/{data['created_object_id']}"
        else:
            existing = self._reconcile(reconcile)
            if existing is None:
                raise ReadbackError(
                    "write response has no safe readback identity; supply a supported exact reconciliation path"
                )
            return self._verify_readback(existing, payload)
        return self._verify_readback(self.get_json(readback_path), payload)

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
        action = method == "POST" and self._is_action(path)
        if method == "POST" and approved_reconcile is None:
            raise UnsupportedOperationError(
                "POST requires an exact bounded reconciliation contract"
            )
        with self._mutation_lock(method, path, approved_reconcile):
            if method == "POST":
                assert approved_reconcile is not None
                existing = (
                    self._prepare_action(approved_reconcile)
                    if action
                    else self._reconcile(approved_reconcile)
                )
                if existing is not None:
                    expected = approved_reconcile["after"] if action else approved_payload
                    return self._verify_readback(existing, expected)
            try:
                _status, data, _headers = self._request(method, path, payload=approved_payload)
            except IndeterminateWriteError:
                try:
                    if action:
                        assert approved_reconcile is not None
                        return self._verify_action_after(approved_reconcile)
                    existing = self._reconcile(approved_reconcile)
                    if existing is not None:
                        return self._verify_readback(existing, approved_payload)
                except (GrocyError, KeyError, TypeError):
                    pass
                raise
            try:
                return self._readback_after_write(
                    method,
                    path,
                    approved_payload,
                    data,
                    approved_reconcile,
                    action,
                )
            except IndeterminateWriteError:
                raise
            except GrocyError as exc:
                raise IndeterminateWriteError(
                    "write completed but mandatory readback failed; do not replay"
                ) from exc


def _load_object(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError("JSON input must be an object")
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
    listing.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
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
        client = GrocyClient(timeout=args.timeout, max_retries=args.max_retries)
        if args.command == "preflight":
            output = client.preflight()
        elif args.command == "schema-check":
            client.check_schema(args.method, args.path, expected_version=args.expected_version)
            output = {"schema": "compatible", "method": args.method.upper(), "path": args.path}
        elif args.command == "get":
            client.check_schema("GET", args.path)
            output = client.get_json(args.path)
        elif args.command == "list":
            output = client.list_all(args.path, cap=args.cap, page_size=args.page_size)
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
    except (GrocyError, OSError, ValueError, json.JSONDecodeError) as exc:
        json.dump(
            {
                "error": exc.code if isinstance(exc, GrocyError) else "input_error",
                "status": exc.status if isinstance(exc, GrocyError) else None,
                "message": str(exc),
            },
            sys.stderr,
        )
        sys.stderr.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
