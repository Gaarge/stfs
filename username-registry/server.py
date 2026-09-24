#!/usr/bin/env python3
"""Small authenticated username-claim service backed by SQLite."""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_BATCH_SIZE = 200_000
MAX_CLAIM_NEXT_SIZE = 10_000


class RegistryError(Exception):
    def __init__(self, status: HTTPStatus, error: str, message: str) -> None:
        self.status, self.error, self.message = status, error, message
        super().__init__(message)


def normalize_username(value: Any) -> str:
    if not isinstance(value, str):
        raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_username", "username must be a string")
    username = value.strip()
    if username.startswith("@"):
        username = username[1:]
    if not username or len(username) > 128:
        raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_username", "username must contain between 1 and 128 characters")
    return username.casefold()


def normalize_optional_username(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_username", "username must be a string")
    if not value.strip() or value.strip() == "@":
        return None
    return normalize_username(value)


def validate_chat(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_chat", "chat must be a non-empty string")
    if len(value) > 20_000:
        raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_chat", "chat is too long")
    return value.strip()


def normalize_integer(value: Any, field: str, allow_negative: bool = False) -> str:
    if isinstance(value, bool):
        raise RegistryError(HTTPStatus.BAD_REQUEST, f"invalid_{field}", f"{field} must be an integer")
    raw = str(value).strip() if isinstance(value, (str, int)) else ""
    sign = raw[:1]
    digits = raw[1:] if sign in {"+", "-"} else raw
    if not digits.isdecimal() or (sign == "-" and not allow_negative):
        raise RegistryError(HTTPStatus.BAD_REQUEST, f"invalid_{field}", f"{field} must be an integer")
    if len(digits) > 20 or int(raw) == 0:
        raise RegistryError(HTTPStatus.BAD_REQUEST, f"invalid_{field}", f"{field} is out of range")
    return raw.lstrip("+")


def normalize_user_id(value: Any) -> str:
    return normalize_integer(value, "user_id")


def normalize_access_hash(value: Any) -> str:
    return normalize_integer(value, "access_hash", allow_negative=True)


def normalize_claim_next_size(value: Any) -> int:
    """Return a safe requested batch size; absent n means one record."""
    if value is None:
        return 1
    if isinstance(value, bool):
        raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_n", "n must be an integer")
    raw = str(value).strip() if isinstance(value, (str, int)) else ""
    if not raw.isdecimal():
        raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_n", "n must be an integer between 1 and 10000")
    size = int(raw)
    if not 1 <= size <= MAX_CLAIM_NEXT_SIZE:
        raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_n", "n must be an integer between 1 and 10000")
    return size


class Registry:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            table_info = list(connection.execute("PRAGMA table_info(usernames)"))
            columns = {row["name"] for row in table_info}
            if columns and {"user_id", "access_hash"} - columns:
                count = connection.execute("SELECT count(*) FROM usernames").fetchone()[0]
                if count:
                    raise RuntimeError(
                        "Legacy registry contains records without Telegram IDs. "
                        "Back it up and re-import it with username, user_id, access_hash and chat."
                    )
                connection.execute("DROP TABLE usernames")
                columns = set()
            username_is_primary_key = any(row["name"] == "username" and row["pk"] for row in table_info)
            if columns and username_is_primary_key:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("ALTER TABLE usernames RENAME TO usernames_before_optional_username")
                    self._create_table(connection)
                    connection.execute(
                        """
                        INSERT INTO usernames(user_id, username, access_hash, chat, used, used_at, queue_order)
                        SELECT user_id, username, access_hash, chat, used, used_at, rowid
                        FROM usernames_before_optional_username
                        """
                    )
                    connection.execute("DROP TABLE usernames_before_optional_username")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            self._create_table(connection)
            self._ensure_queue_order(connection)
            connection.execute("CREATE INDEX IF NOT EXISTS usernames_unused_order ON usernames(used, queue_order)")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS usernames_queue_order_unique ON usernames(queue_order)")

    @staticmethod
    def _create_table(connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS usernames (
                user_id TEXT PRIMARY KEY,
                username TEXT UNIQUE COLLATE NOCASE,
                access_hash TEXT NOT NULL,
                chat TEXT NOT NULL CHECK(length(trim(chat)) > 0),
                used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0, 1)),
                used_at TEXT,
                queue_order INTEGER
            )
        """)

    @staticmethod
    def _ensure_queue_order(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(usernames)")}
        if "queue_order" not in columns:
            connection.execute("ALTER TABLE usernames ADD COLUMN queue_order INTEGER")
        connection.execute("UPDATE usernames SET queue_order = rowid WHERE queue_order IS NULL")

    @staticmethod
    def _next_queue_order(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT COALESCE(MAX(queue_order), 0) + 1 FROM usernames").fetchone()[0])

    @staticmethod
    def _public_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "username": row["username"],
            "user_id": row["user_id"],
            "access_hash": row["access_hash"],
            "chat": row["chat"],
        }

    def claim(self, username: str) -> dict[str, Any]:
        """Claim a username once, safely even with simultaneous requests."""
        username = normalize_username(username)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT username, user_id, access_hash, chat, used FROM usernames WHERE username = ?", (username,)
            ).fetchone()
            if row is None:
                connection.commit()
                return {"available": False, "reason": "not_found"}
            if row["used"]:
                connection.commit()
                return {"available": False, "reason": "already_used"}
            changed = connection.execute(
                "UPDATE usernames SET used = 1, used_at = ? WHERE username = ? AND used = 0",
                (datetime.now(timezone.utc).isoformat(), username),
            ).rowcount
            connection.commit()
            if changed != 1:
                return {"available": False, "reason": "already_used"}
            return {"available": True, **self._public_record(row)}

    def claim_next(self, size: int = 1) -> dict[str, Any]:
        """Atomically claim the first `size` records that have not been used.

        BEGIN IMMEDIATE serializes concurrent clients: the next request starts
        only after this transaction marks its selected range as used.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = list(connection.execute(
                """
                SELECT username, user_id, access_hash, chat
                FROM usernames
                WHERE used = 0
                ORDER BY queue_order ASC
                LIMIT ?
                """,
                (size,),
            ))
            if not rows:
                connection.commit()
                return {"available": False, "requested": size, "claimed": 0, "items": [], "exhausted": True}
            user_ids = [row["user_id"] for row in rows]
            placeholders = ", ".join("?" for _ in user_ids)
            changed = connection.execute(
                f"UPDATE usernames SET used = 1, used_at = ? WHERE used = 0 AND user_id IN ({placeholders})",
                (datetime.now(timezone.utc).isoformat(), *user_ids),
            ).rowcount
            if changed != len(rows):
                connection.rollback()
                raise RuntimeError("queue claim changed unexpectedly")
            connection.commit()
            items = [self._public_record(row) for row in rows]
            result: dict[str, Any] = {
                "available": True,
                "requested": size,
                "claimed": len(items),
                "items": items,
                "exhausted": len(items) < size,
            }
            if size == 1:
                result["item"] = items[0]
            return result

    def add(self, username: str | None, user_id: str, access_hash: str, chat: str) -> None:
        username = normalize_optional_username(username)
        user_id = normalize_user_id(user_id)
        access_hash = normalize_access_hash(access_hash)
        chat = validate_chat(chat)
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                queue_order = self._next_queue_order(connection)
                connection.execute(
                    "INSERT INTO usernames(username, user_id, access_hash, chat, used, queue_order) VALUES (?, ?, ?, ?, 0, ?)",
                    (username, user_id, access_hash, chat, queue_order),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            if "user_id" in str(exc):
                raise RegistryError(HTTPStatus.CONFLICT, "duplicate_user_id", "This Telegram user already exists and cannot be added again.") from exc
            raise RegistryError(HTTPStatus.CONFLICT, "duplicate_username", "This username already exists and cannot be added again.") from exc

    def import_records(self, records: list[Any], ignore_existing: bool = False) -> tuple[int, int]:
        if not records:
            raise RegistryError(HTTPStatus.BAD_REQUEST, "empty_import", "records must not be empty")
        if len(records) > MAX_BATCH_SIZE:
            raise RegistryError(HTTPStatus.BAD_REQUEST, "batch_too_large", "too many records in one import")
        prepared: list[tuple[str | None, str, str, str]] = []
        seen_usernames: set[str] = set()
        seen_user_ids: set[str] = set()
        for number, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_record", f"record {number} must be an object")
            username = normalize_optional_username(record.get("username"))
            user_id = normalize_user_id(record.get("user_id"))
            access_hash = normalize_access_hash(record.get("access_hash"))
            chat = validate_chat(record.get("chat"))
            if username and username in seen_usernames:
                raise RegistryError(HTTPStatus.CONFLICT, "duplicate_username", f"username {username!r} appears more than once in this import")
            if user_id in seen_user_ids:
                raise RegistryError(HTTPStatus.CONFLICT, "duplicate_user_id", f"user_id {user_id!r} appears more than once in this import")
            if username:
                seen_usernames.add(username)
            seen_user_ids.add(user_id)
            prepared.append((username, user_id, access_hash, chat))
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                next_queue_order = self._next_queue_order(connection)
                prepared_with_order = [
                    (*record, next_queue_order + index)
                    for index, record in enumerate(prepared)
                ]
                statement = "INSERT INTO usernames(username, user_id, access_hash, chat, used, queue_order) VALUES (?, ?, ?, ?, 0, ?)"
                if ignore_existing:
                    before = connection.total_changes
                    connection.executemany("INSERT OR IGNORE" + statement.removeprefix("INSERT"), prepared_with_order)
                    imported = connection.total_changes - before
                else:
                    connection.executemany(statement, prepared_with_order)
                    imported = len(prepared)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            error = "duplicate_user_id" if "user_id" in str(exc) else "duplicate_username"
            raise RegistryError(HTTPStatus.CONFLICT, error, "A username or Telegram user already exists; import was not changed.") from exc
        return imported, len(prepared) - imported


class RequestHandler(BaseHTTPRequestHandler):
    registry: Registry
    client_key: str
    admin_key: str
    server_version = "UsernameRegistry/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.client_address[0]} - {format % args}", flush=True)

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def require_key(self, admin: bool = False) -> None:
        expected = self.admin_key if admin else self.client_key
        provided = self.headers.get("X-Admin-Key" if admin else "X-API-Key", "")
        if not hmac.compare_digest(provided, expected):
            raise RegistryError(HTTPStatus.UNAUTHORIZED, "unauthorized", "valid API key required")

    def read_json(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise RegistryError(HTTPStatus.LENGTH_REQUIRED, "invalid_body", "Content-Length is required") from exc
        if size < 1 or size > MAX_BODY_BYTES:
            raise RegistryError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid_body", "request body is too large")
        try:
            payload = json.loads(self.rfile.read(size))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_json", "request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_json", "JSON body must be an object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.send_json(HTTPStatus.OK, {"ok": True})
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": "endpoint not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/v1/claim":
                self.require_key()
                result = self.registry.claim(normalize_username(self.read_json().get("username")))
                self.send_json(HTTPStatus.OK, result)
                return
            if self.path == "/v1/claim-next":
                self.require_key()
                payload = self.read_json()
                result = self.registry.claim_next(normalize_claim_next_size(payload.get("n")))
                self.send_json(HTTPStatus.OK, result)
                return
            if self.path == "/v1/records":
                self.require_key(admin=True)
                payload = self.read_json()
                self.registry.add(payload.get("username"), payload.get("user_id"), payload.get("access_hash"), payload.get("chat"))
                self.send_json(HTTPStatus.CREATED, {"ok": True, "used": "no"})
                return
            if self.path == "/v1/import":
                self.require_key(admin=True)
                payload = self.read_json()
                records = payload.get("records")
                if not isinstance(records, list):
                    raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_records", "records must be an array")
                ignore_existing = payload.get("ignore_existing", False)
                if not isinstance(ignore_existing, bool):
                    raise RegistryError(HTTPStatus.BAD_REQUEST, "invalid_ignore_existing", "ignore_existing must be a boolean")
                imported, skipped_existing = self.registry.import_records(records, ignore_existing)
                self.send_json(
                    HTTPStatus.CREATED,
                    {"ok": True, "imported": imported, "skipped_existing": skipped_existing, "used": "no"},
                )
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": "endpoint not found"})
        except RegistryError as exc:
            self.send_json(exc.status, {"error": exc.error, "message": exc.message})
        except (sqlite3.Error, OSError):
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "database_unavailable", "message": "try again later"})


def main() -> None:
    database = os.environ.get("REGISTRY_DB", str(Path(__file__).with_name("registry.sqlite3")))
    client_key, admin_key = os.environ.get("REGISTRY_API_KEY"), os.environ.get("REGISTRY_ADMIN_KEY")
    if not client_key or not admin_key:
        raise SystemExit("REGISTRY_API_KEY and REGISTRY_ADMIN_KEY must be set")
    registry = Registry(database)
    registry.initialize()
    RequestHandler.registry, RequestHandler.client_key, RequestHandler.admin_key = registry, client_key, admin_key
    host, port = os.environ.get("REGISTRY_HOST", "127.0.0.1"), int(os.environ.get("REGISTRY_PORT", "8711"))
    print(f"Username registry listening on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), RequestHandler).serve_forever()


if __name__ == "__main__":
    main()
