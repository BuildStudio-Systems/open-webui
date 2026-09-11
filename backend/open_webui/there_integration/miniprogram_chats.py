"""Read-only, privacy-bounded access to Mini Program chats in THERE PostgreSQL."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Connection, Engine

POLICY_VERSION_ENV = "BUILDSTUDIO_MINIPROGRAM_POLICY_VERSION"
POSTGRES_SCHEMA = "there_miniprogram"
REQUIRED_SCHEMA_VERSION = 1
REQUIRED_SCHEMA_CHECKSUM = "deb322343dd480ee65889122a41267aa2e3f4dfdd1fcf7fc6f22f5ca2a498f33"
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
MAX_CURSOR_LENGTH = 256
BLOCKED_HOST_SUFFIXES = {
    "alt",
    "corp",
    "home",
    "home.arpa",
    "internal",
    "intranet",
    "invalid",
    "lan",
    "local",
    "localdomain",
    "localhost",
    "onion",
    "private",
    "test",
}
HOST_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
REQUIRED_COLUMNS = {
    "schema_version": {"version", "checksum", "applied_at"},
    "users": {"id"},
    "policy_acceptances": {"user_id", "policy_version", "accepted_at"},
    "chats": {
        "id",
        "user_id",
        "policy_version",
        "model",
        "thinking_mode",
        "review_provenance",
        "created_at",
        "updated_at",
    },
    "messages": {
        "id",
        "sequence",
        "chat_id",
        "role",
        "content",
        "sources",
        "review_provenance",
        "created_at",
    },
    "content_reports": {"id", "chat_id"},
}


class MiniProgramChatStoreError(RuntimeError):
    """A bounded error which never includes content or database details."""


def _canonical_uuid(value: str, *, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{field} is invalid") from None
    canonical = str(parsed)
    if canonical != str(value).lower():
        raise ValueError(f"{field} is invalid")
    return canonical


def _database_uuid(value: Any) -> str:
    try:
        return _canonical_uuid(str(value), field="database identifier")
    except ValueError:
        raise MiniProgramChatStoreError("Mini Program chat storage contains invalid data") from None


def _database_integer(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise MiniProgramChatStoreError("Mini Program chat storage contains invalid data") from None
    if result < 0:
        raise MiniProgramChatStoreError("Mini Program chat storage contains invalid data")
    return result


def _encode_cursor(values: list[Any]) -> str:
    payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, *, identifier: str) -> tuple[int, str | int] | None:
    if not cursor:
        return None
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError("cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("cursor is invalid") from None
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("cursor is invalid")
    timestamp = values[0]
    if not isinstance(timestamp, int) or timestamp < 0:
        raise ValueError("cursor is invalid")
    if identifier == "uuid":
        return timestamp, _canonical_uuid(values[1], field="cursor")
    if identifier == "integer" and isinstance(values[1], int) and values[1] > 0:
        return timestamp, values[1]
    raise ValueError("cursor is invalid")


def _page_size(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError("limit is invalid")
    return limit


def _account_reference(user_id: str) -> str:
    digest = hashlib.sha256(f"buildstudio-there-mini-account-v1:{user_id}".encode()).hexdigest()
    return f"wx-{digest[:12]}"


def _safe_text(value: Any, maximum: int) -> str:
    text_value = str(value or "")[:maximum]
    return "".join(character for character in text_value if character in "\n\r\t" or ord(character) >= 32)


def _public_hostname(value: str) -> str | None:
    """Return a normalized public DNS name; never return local names or IPs."""
    if not value or value.endswith("."):
        return None
    try:
        hostname = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if len(hostname) > 253:
        return None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return None
    labels = hostname.split(".")
    if len(labels) < 2 or any(not HOST_LABEL_PATTERN.fullmatch(label) for label in labels):
        return None
    if labels[-1].isdigit():
        return None
    if any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in BLOCKED_HOST_SUFFIXES):
        return None
    return hostname


def _safe_sources(value: Any) -> list[dict[str, str]]:
    if isinstance(value, str):
        try:
            source_rows = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
    else:
        source_rows = value
    if not isinstance(source_rows, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in source_rows[:32]:
        if not isinstance(row, dict):
            continue
        raw_url = str(row.get("url") or "")[:2048]
        try:
            parsed = urlsplit(raw_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                continue
            hostname = _public_hostname(parsed.hostname)
            if hostname is None or parsed.port not in {None, 443}:
                continue
            safe_url = urlunsplit(("https", hostname, parsed.path or "/", "", ""))
        except ValueError:
            continue
        if safe_url in seen:
            continue
        seen.add(safe_url)
        result.append(
            {
                "url": safe_url,
                "name": _safe_text(row.get("name") or row.get("title"), 256),
            }
        )
        if len(result) == 8:
            break
    return result


def _primary_engine() -> Engine:
    # Import lazily so pure store tests do not initialize the whole web backend.
    from open_webui.internal.db import engine

    return engine


class MiniProgramChatStore:
    """Run bounded SELECTs against the Mini Program schema on the main DB pool."""

    def __init__(
        self,
        *,
        policy_version: str | None = None,
        _engine: Engine | None = None,
    ):
        self._engine = _engine if _engine is not None else _primary_engine()
        self._injected_test_engine = _engine is not None
        self.policy_version = (
            policy_version if policy_version is not None else os.getenv(POLICY_VERSION_ENV, "")
        ).strip()

    def _policy_version(self) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.policy_version):
            raise MiniProgramChatStoreError("Mini Program chat storage policy is unavailable")
        return self.policy_version

    @property
    def _is_postgresql(self) -> bool:
        return self._engine.dialect.name == "postgresql"

    def _table(self, name: str) -> str:
        if name not in REQUIRED_COLUMNS:
            raise MiniProgramChatStoreError("Mini Program chat storage schema is invalid")
        return f'"{POSTGRES_SCHEMA}"."{name}"' if self._is_postgresql else f'"{name}"'

    @contextmanager
    def _connect(self) -> Iterator[Connection]:
        if not self._is_postgresql and not self._injected_test_engine:
            raise MiniProgramChatStoreError("Mini Program chat storage is unavailable")
        try:
            with self._engine.connect() as connection:
                transaction = connection.begin()
                try:
                    if self._is_postgresql:
                        # This must be the first statement in the transaction. It
                        # makes the admin reader fail at the database boundary if
                        # future code accidentally attempts a write.
                        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    self._validate_schema(connection)
                    yield connection
                finally:
                    transaction.rollback()
        except MiniProgramChatStoreError:
            raise
        except SQLAlchemyError:
            raise MiniProgramChatStoreError("Mini Program chat storage is unavailable") from None

    def _validate_schema(self, connection: Connection) -> None:
        if self._is_postgresql:
            rows = connection.execute(
                text("""
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                      AND table_name IN (
                          'schema_version', 'users', 'policy_acceptances',
                          'chats', 'messages', 'content_reports'
                      )
                    """),
                {"schema": POSTGRES_SCHEMA},
            ).mappings()
            available: dict[str, set[str]] = {}
            for row in rows:
                available.setdefault(str(row["table_name"]), set()).add(str(row["column_name"]))
        else:
            inspector = inspect(connection)
            available = {
                table: {str(column["name"]) for column in inspector.get_columns(table)}
                for table in REQUIRED_COLUMNS
                if inspector.has_table(table)
            }
        if any(not required.issubset(available.get(table, set())) for table, required in REQUIRED_COLUMNS.items()):
            raise MiniProgramChatStoreError("Mini Program chat storage schema is invalid")
        schema_row = (
            connection.execute(
                text(f"SELECT version, checksum FROM {self._table('schema_version')} " "ORDER BY version DESC LIMIT 1")
            )
            .mappings()
            .first()
        )
        if (
            not schema_row
            or schema_row["version"] != REQUIRED_SCHEMA_VERSION
            or schema_row["checksum"] != REQUIRED_SCHEMA_CHECKSUM
        ):
            raise MiniProgramChatStoreError("Mini Program chat storage schema is invalid")

    def _identifier_parameter(self, value: str) -> UUID | str:
        return UUID(value) if self._is_postgresql else value

    def list_chats(self, *, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        page_size = _page_size(limit)
        boundary = _decode_cursor(cursor, identifier="uuid")
        policy_version = self._policy_version()
        chats = self._table("chats")
        messages = self._table("messages")
        reports = self._table("content_reports")
        acceptances = self._table("policy_acceptances")
        boundary_sql = ""
        parameters: dict[str, Any] = {"policy_version": policy_version, "row_limit": page_size + 1}
        if boundary:
            boundary_sql = " AND (c.updated_at < :cursor_time OR (c.updated_at = :cursor_time AND c.id < :cursor_id))"
            parameters.update(cursor_time=boundary[0], cursor_id=self._identifier_parameter(boundary[1]))
        query = text(f"""
            SELECT c.id, c.user_id, c.model, c.thinking_mode,
                   c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM {messages} m
                    WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                      AND m.role IN ('user', 'assistant')) AS message_count,
                   (SELECT COUNT(*) FROM {messages} m
                    WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                      AND m.role = 'user') AS turn_count,
                   (SELECT m.role FROM {messages} m
                    WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                      AND m.role IN ('user', 'assistant')
                    ORDER BY m.created_at DESC, m.sequence DESC LIMIT 1) AS last_role,
                   (SELECT COUNT(*) FROM {reports} r WHERE r.chat_id = c.id) AS report_count
            FROM {chats} c
            JOIN {acceptances} p
              ON p.user_id = c.user_id AND p.policy_version = :policy_version
             AND p.accepted_at > 0
            WHERE c.review_provenance = 'wechat' AND c.policy_version = :policy_version{boundary_sql}
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT :row_limit
            """)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).mappings().all()

        has_more = len(rows) > page_size
        visible = rows[:page_size]
        items = []
        for row in visible:
            items.append(
                {
                    "id": _database_uuid(row["id"]),
                    "account_ref": _account_reference(str(row["user_id"])),
                    "model": _safe_text(row["model"], 128),
                    "thinking_mode": _safe_text(row["thinking_mode"], 16),
                    "created_at": _database_integer(row["created_at"]),
                    "updated_at": _database_integer(row["updated_at"]),
                    "message_count": _database_integer(row["message_count"] or 0),
                    "turn_count": _database_integer(row["turn_count"] or 0),
                    "report_count": _database_integer(row["report_count"] or 0),
                    "state": "completed" if row["last_role"] == "assistant" else "incomplete",
                }
            )
        next_cursor = None
        if has_more and visible:
            last = items[-1]
            next_cursor = _encode_cursor([last["updated_at"], last["id"]])
        return {"items": items, "next_cursor": next_cursor}

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        canonical_id = _canonical_uuid(chat_id, field="chat_id")
        policy_version = self._policy_version()
        chats = self._table("chats")
        messages = self._table("messages")
        reports = self._table("content_reports")
        acceptances = self._table("policy_acceptances")
        query = text(f"""
            SELECT c.id, c.user_id, c.model, c.thinking_mode,
                   c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM {messages} m
                    WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                      AND m.role IN ('user', 'assistant')) AS message_count,
                   (SELECT COUNT(*) FROM {messages} m
                    WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                      AND m.role = 'user') AS turn_count,
                   (SELECT m.role FROM {messages} m
                    WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                      AND m.role IN ('user', 'assistant')
                    ORDER BY m.created_at DESC, m.sequence DESC LIMIT 1) AS last_role,
                   (SELECT COUNT(*) FROM {reports} r WHERE r.chat_id = c.id) AS report_count
            FROM {chats} c
            JOIN {acceptances} p
              ON p.user_id = c.user_id AND p.policy_version = :policy_version
             AND p.accepted_at > 0
            WHERE c.id = :chat_id AND c.review_provenance = 'wechat'
              AND c.policy_version = :policy_version
            """)
        with self._connect() as connection:
            row = (
                connection.execute(
                    query,
                    {"policy_version": policy_version, "chat_id": self._identifier_parameter(canonical_id)},
                )
                .mappings()
                .first()
            )
        if not row:
            return None
        return {
            "id": _database_uuid(row["id"]),
            "account_ref": _account_reference(str(row["user_id"])),
            "model": _safe_text(row["model"], 128),
            "thinking_mode": _safe_text(row["thinking_mode"], 16),
            "created_at": _database_integer(row["created_at"]),
            "updated_at": _database_integer(row["updated_at"]),
            "message_count": _database_integer(row["message_count"] or 0),
            "turn_count": _database_integer(row["turn_count"] or 0),
            "report_count": _database_integer(row["report_count"] or 0),
            "state": "completed" if row["last_role"] == "assistant" else "incomplete",
        }

    def list_messages(
        self,
        chat_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any] | None:
        canonical_id = _canonical_uuid(chat_id, field="chat_id")
        page_size = _page_size(limit)
        boundary = _decode_cursor(cursor, identifier="integer")
        policy_version = self._policy_version()
        chats = self._table("chats")
        messages = self._table("messages")
        acceptances = self._table("policy_acceptances")
        boundary_sql = ""
        parameters: dict[str, Any] = {
            "chat_id": self._identifier_parameter(canonical_id),
            "row_limit": page_size + 1,
        }
        if boundary:
            boundary_sql = (
                " AND (m.created_at > :cursor_time OR (m.created_at = :cursor_time AND m.sequence > :cursor_sequence))"
            )
            parameters.update(cursor_time=boundary[0], cursor_sequence=boundary[1])
        exists_query = text(f"""
            SELECT 1
            FROM {chats} c
            JOIN {acceptances} p
              ON p.user_id = c.user_id AND p.policy_version = :policy_version
             AND p.accepted_at > 0
            WHERE c.id = :chat_id AND c.review_provenance = 'wechat'
              AND c.policy_version = :policy_version
            """)
        messages_query = text(f"""
            SELECT m.sequence AS message_order, m.id, m.role, m.content,
                   m.sources, m.created_at
            FROM {messages} m
            WHERE m.chat_id = :chat_id AND m.review_provenance = 'wechat'
              AND m.role IN ('user', 'assistant'){boundary_sql}
            ORDER BY m.created_at ASC, m.sequence ASC
            LIMIT :row_limit
            """)
        with self._connect() as connection:
            exists = connection.execute(
                exists_query,
                {"policy_version": policy_version, "chat_id": self._identifier_parameter(canonical_id)},
            ).first()
            if not exists:
                return None
            rows = connection.execute(messages_query, parameters).mappings().all()

        has_more = len(rows) > page_size
        visible = rows[:page_size]
        items = [
            {
                "id": _database_uuid(row["id"]),
                "role": str(row["role"]),
                "content": _safe_text(row["content"], 20_000),
                "sources": _safe_sources(row["sources"]),
                "created_at": _database_integer(row["created_at"]),
            }
            for row in visible
        ]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                [
                    _database_integer(last["created_at"]),
                    _database_integer(last["message_order"]),
                ]
            )
        return {"items": items, "next_cursor": next_cursor}
