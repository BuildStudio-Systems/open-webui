"""Read-only, privacy-bounded access to the canonical Mini Program chat store."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

DATABASE_PATH_ENV = "BUILDSTUDIO_MINIPROGRAM_DATABASE_PATH"
POLICY_VERSION_ENV = "BUILDSTUDIO_MINIPROGRAM_POLICY_VERSION"
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
    "user": {"id"},
    "policy_acceptance": {"user_id", "policy_version", "accepted_at"},
    "chat": {
        "id",
        "user_id",
        "policy_version",
        "model",
        "thinking_mode",
        "review_provenance",
        "created_at",
        "updated_at",
    },
    "message": {
        "id",
        "chat_id",
        "role",
        "content",
        "sources_json",
        "review_provenance",
        "created_at",
    },
    "content_report": {"id", "chat_id"},
}


class MiniProgramChatStoreError(RuntimeError):
    """A bounded error which never includes paths, content, or SQLite details."""


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


def _decode_cursor(cursor: str | None, *, kind: str) -> tuple[int, str | int] | None:
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
    if kind == "chat":
        return timestamp, _canonical_uuid(values[1], field="cursor")
    rowid = values[1]
    if not isinstance(rowid, int) or rowid < 1:
        raise ValueError("cursor is invalid")
    return timestamp, rowid


def _page_size(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError("limit is invalid")
    return limit


def _account_reference(user_id: str) -> str:
    digest = hashlib.sha256(f"buildstudio-there-mini-account-v1:{user_id}".encode("utf-8")).hexdigest()
    return f"wx-{digest[:12]}"


def _safe_text(value: Any, maximum: int) -> str:
    text = str(value or "")[:maximum]
    return "".join(character for character in text if character in "\n\r\t" or ord(character) >= 32)


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
    try:
        source_rows = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
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


class MiniProgramChatStore:
    """Open the live SQLite database in read-only mode for each bounded query."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        policy_version: str | None = None,
    ):
        configured = path if path is not None else os.getenv(DATABASE_PATH_ENV, "")
        self.path = Path(configured).expanduser() if configured else None
        self.policy_version = (
            policy_version if policy_version is not None else os.getenv(POLICY_VERSION_ENV, "")
        ).strip()

    def _policy_version(self) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.policy_version):
            raise MiniProgramChatStoreError("Mini Program chat storage policy is unavailable")
        return self.policy_version

    def _path(self) -> Path:
        path = self.path
        if path is None or not path.is_absolute():
            raise MiniProgramChatStoreError("Mini Program chat storage is unavailable")
        try:
            if path.is_symlink() or not path.is_file():
                raise MiniProgramChatStoreError("Mini Program chat storage is unavailable")
            if os.name != "nt" and path.stat().st_mode & 0o077:
                raise MiniProgramChatStoreError("Mini Program chat storage permissions are unsafe")
            return path.resolve(strict=True)
        except OSError:
            raise MiniProgramChatStoreError("Mini Program chat storage is unavailable") from None

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self._path().as_uri() + "?mode=ro",
                uri=True,
                timeout=3,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 3000")
            self._validate_schema(connection)
            return connection
        except MiniProgramChatStoreError:
            raise
        except sqlite3.Error:
            raise MiniProgramChatStoreError("Mini Program chat storage is unavailable") from None

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        for table, required in REQUIRED_COLUMNS.items():
            try:
                columns = {str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()}
            except sqlite3.Error:
                raise MiniProgramChatStoreError("Mini Program chat storage schema is invalid") from None
            if not required.issubset(columns):
                raise MiniProgramChatStoreError("Mini Program chat storage schema is invalid")

    def list_chats(self, *, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        page_size = _page_size(limit)
        boundary = _decode_cursor(cursor, kind="chat")
        policy_version = self._policy_version()
        where = "c.review_provenance = 'wechat' AND c.policy_version = ?"
        parameters: list[Any] = [policy_version, policy_version]
        if boundary:
            where += " AND (c.updated_at < ? OR (c.updated_at = ? AND c.id < ?))"
            parameters.extend((boundary[0], boundary[0], boundary[1]))
        parameters.append(page_size + 1)
        query = f"""
            SELECT c.id, c.user_id, c.model, c.thinking_mode,
                   c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM message m
                    WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                      AND m.role IN ('user', 'assistant')) AS message_count,
                   (SELECT COUNT(*) FROM message m
                    WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                      AND m.role = 'user') AS turn_count,
                   (SELECT m.role FROM message m
                    WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                      AND m.role IN ('user', 'assistant')
                    ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_role,
                   (SELECT COUNT(*) FROM content_report r WHERE r.chat_id = c.id) AS report_count
            FROM chat c
            JOIN policy_acceptance p
              ON p.user_id = c.user_id AND p.policy_version = ?
             AND p.accepted_at > 0
            WHERE {where}
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT ?
        """
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(query, parameters).fetchall()
        except sqlite3.Error:
            raise MiniProgramChatStoreError("Mini Program chat storage is unavailable") from None

        has_more = len(rows) > page_size
        visible = rows[:page_size]
        items = []
        for row in visible:
            message_count = _database_integer(row["message_count"] or 0)
            items.append(
                {
                    "id": _database_uuid(row["id"]),
                    "account_ref": _account_reference(str(row["user_id"])),
                    "model": _safe_text(row["model"], 128),
                    "thinking_mode": _safe_text(row["thinking_mode"], 16),
                    "created_at": _database_integer(row["created_at"]),
                    "updated_at": _database_integer(row["updated_at"]),
                    "message_count": message_count,
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
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT c.id, c.user_id, c.model, c.thinking_mode,
                           c.created_at, c.updated_at,
                           (SELECT COUNT(*) FROM message m
                            WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                              AND m.role IN ('user', 'assistant')) AS message_count,
                           (SELECT COUNT(*) FROM message m
                            WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                              AND m.role = 'user') AS turn_count,
                           (SELECT m.role FROM message m
                            WHERE m.chat_id = c.id AND m.review_provenance = 'wechat'
                              AND m.role IN ('user', 'assistant')
                            ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_role,
                           (SELECT COUNT(*) FROM content_report r WHERE r.chat_id = c.id) AS report_count
                    FROM chat c
                    JOIN policy_acceptance p
                      ON p.user_id = c.user_id AND p.policy_version = ?
                     AND p.accepted_at > 0
                    WHERE c.id = ? AND c.review_provenance = 'wechat'
                      AND c.policy_version = ?
                    """,
                    (self._policy_version(), canonical_id, self._policy_version()),
                ).fetchone()
        except sqlite3.Error:
            raise MiniProgramChatStoreError("Mini Program chat storage is unavailable") from None
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
        boundary = _decode_cursor(cursor, kind="message")
        parameters: list[Any] = [canonical_id]
        boundary_sql = ""
        if boundary:
            boundary_sql = " AND (m.created_at > ? OR (m.created_at = ? AND m.rowid > ?))"
            parameters.extend((boundary[0], boundary[0], boundary[1]))
        parameters.append(page_size + 1)
        try:
            with closing(self._connect()) as connection:
                exists = connection.execute(
                    """
                    SELECT 1
                    FROM chat c
                    JOIN policy_acceptance p
                      ON p.user_id = c.user_id AND p.policy_version = ?
                     AND p.accepted_at > 0
                    WHERE c.id = ? AND c.review_provenance = 'wechat'
                      AND c.policy_version = ?
                    """,
                    (self._policy_version(), canonical_id, self._policy_version()),
                ).fetchone()
                if not exists:
                    return None
                rows = connection.execute(
                    f"""
                    SELECT m.rowid AS message_order, m.id, m.role, m.content,
                           m.sources_json, m.created_at
                    FROM message m
                    WHERE m.chat_id = ? AND m.review_provenance = 'wechat'
                      AND m.role IN ('user', 'assistant'){boundary_sql}
                    ORDER BY m.created_at ASC, m.rowid ASC
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
        except sqlite3.Error:
            raise MiniProgramChatStoreError("Mini Program chat storage is unavailable") from None

        has_more = len(rows) > page_size
        visible = rows[:page_size]
        items = [
            {
                "id": _database_uuid(row["id"]),
                "role": str(row["role"]),
                "content": _safe_text(row["content"], 20_000),
                "sources": _safe_sources(row["sources_json"]),
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
