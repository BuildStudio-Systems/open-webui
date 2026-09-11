"""Isolated tests for the read-only Mini Program administrator chat view."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

CHAT_OLDER = "11111111-1111-4111-8111-111111111111"
CHAT_NEWER = "22222222-2222-4222-8222-222222222222"
CHAT_LEGACY = "33333333-3333-4333-8333-333333333333"
USER_ID = "44444444-4444-4444-8444-444444444444"
CHAT_UNCONSENTED = "55555555-5555-4555-8555-555555555555"
USER_UNCONSENTED = "66666666-6666-4666-8666-666666666666"
CHAT_OLD_POLICY = "77777777-7777-4777-8777-777777777777"
POLICY_VERSION = "2026-09-09.1"
OLD_POLICY_VERSION = "2026-09-03"


def create_store(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE user (
            id TEXT PRIMARY KEY, openid TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE policy_acceptance (
            user_id TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
            policy_version TEXT NOT NULL, accepted_at INTEGER NOT NULL,
            PRIMARY KEY(user_id, policy_version)
        );
        CREATE TABLE chat (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
            policy_version TEXT NOT NULL, title TEXT NOT NULL, model TEXT NOT NULL, thinking_mode TEXT NOT NULL,
            review_provenance TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chat(id) ON DELETE CASCADE,
            role TEXT NOT NULL, content TEXT NOT NULL, sources_json TEXT NOT NULL,
            review_provenance TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE content_report (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
            chat_id TEXT NOT NULL REFERENCES chat(id) ON DELETE CASCADE,
            message_id TEXT NOT NULL REFERENCES message(id) ON DELETE CASCADE,
            reason TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        """)
    connection.execute(
        "INSERT INTO user VALUES (?, ?, ?, ?, ?)",
        (USER_ID, "secret-openid-must-never-leave", "There User", 10, 10),
    )
    connection.execute(
        "INSERT INTO policy_acceptance VALUES (?, ?, ?)",
        (USER_ID, POLICY_VERSION, 10),
    )
    connection.execute(
        "INSERT INTO policy_acceptance VALUES (?, ?, ?)",
        (USER_ID, OLD_POLICY_VERSION, 9),
    )
    connection.execute(
        "INSERT INTO user VALUES (?, ?, ?, ?, ?)",
        (
            USER_UNCONSENTED,
            "unconsented-openid-must-never-leave",
            "There User",
            10,
            10,
        ),
    )
    for row in (
        (CHAT_OLDER, POLICY_VERSION, "private first question", "there-3.8", "off", "wechat", 10, 20),
        (CHAT_NEWER, POLICY_VERSION, "newer private question", "there-3.8", "high", "wechat", 30, 40),
        (CHAT_LEGACY, POLICY_VERSION, "quarantined legacy text", "there-3.8", "off", "legacy", 50, 60),
        (CHAT_OLD_POLICY, OLD_POLICY_VERSION, "old-policy private question", "there-3.8", "off", "wechat", 55, 65),
    ):
        connection.execute(
            "INSERT INTO chat VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row[0], USER_ID, *row[1:]),
        )
    connection.execute(
        "INSERT INTO chat VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            CHAT_UNCONSENTED,
            USER_UNCONSENTED,
            POLICY_VERSION,
            "unconsented private question",
            "there-3.8",
            "off",
            "wechat",
            60,
            70,
        ),
    )
    messages = (
        ("a0000000-0000-4000-8000-000000000001", CHAT_OLDER, "user", "Question one", "[]", "wechat", 11),
        (
            "a0000000-0000-4000-8000-000000000002",
            CHAT_OLDER,
            "assistant",
            "Answer one <script>alert(1)</script>",
            json.dumps(
                [
                    {"url": "https://example.com/paper?token=secret#section", "name": "Paper"},
                    {"url": "javascript:alert(1)", "name": "Bad"},
                    {"url": "https://user:password@example.com/private", "name": "Credential"},
                    {"url": "https://localhost/admin", "name": "Localhost"},
                    {"url": "https://localhost./admin", "name": "Localhost FQDN"},
                    {"url": "https://service.internal/admin", "name": "Internal"},
                    {"url": "https://service.local/admin", "name": "mDNS"},
                    {"url": "https://printer/admin", "name": "Single label"},
                    {"url": "https://127.0.0.1/admin", "name": "Loopback"},
                    {"url": "https://10.0.0.8/admin", "name": "Private"},
                    {"url": "https://169.254.169.254/latest", "name": "Link local"},
                    {"url": "https://[::1]/admin", "name": "IPv6 loopback"},
                    {"url": "https://[fe80::1]/admin", "name": "IPv6 link local"},
                    {"url": "https://8.8.8.8/admin", "name": "Public IP literal"},
                    {"url": "https://example.net:8443/admin", "name": "Nonstandard port"},
                ]
            ),
            "wechat",
            12,
        ),
        ("a0000000-0000-4000-8000-000000000003", CHAT_NEWER, "user", "Pending question", "[]", "wechat", 31),
        ("a0000000-0000-4000-8000-000000000004", CHAT_NEWER, "assistant", "hidden dev", "[]", "development", 32),
        ("a0000000-0000-4000-8000-000000000005", CHAT_LEGACY, "user", "hidden legacy", "[]", "legacy", 51),
    )
    connection.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?)", messages)
    connection.execute(
        "INSERT INTO content_report VALUES (?, ?, ?, ?, ?, ?)",
        (
            "b0000000-0000-4000-8000-000000000001",
            USER_ID,
            CHAT_OLDER,
            "a0000000-0000-4000-8000-000000000002",
            "Incorrect or misleading",
            13,
        ),
    )
    connection.commit()
    connection.close()
    if os.name != "nt":
        path.chmod(0o600)


def sensitive_text(value) -> str:
    return json.dumps(value, ensure_ascii=False).lower()


def test_store_filters_identity_provenance_and_list_content(tmp_path):
    from open_webui.there_integration.miniprogram_chats import MiniProgramChatStore

    database = tmp_path / "mini.sqlite"
    create_store(database)
    store = MiniProgramChatStore(database, policy_version=POLICY_VERSION)
    first = store.list_chats(limit=1)
    assert len(first["items"]) == 1
    assert first["items"][0]["id"] == CHAT_NEWER
    assert first["items"][0]["state"] == "incomplete"
    assert first["next_cursor"]
    second = store.list_chats(limit=1, cursor=first["next_cursor"])
    assert [item["id"] for item in second["items"]] == [CHAT_OLDER]
    assert second["items"][0]["state"] == "completed"
    assert second["items"][0]["report_count"] == 1
    all_summaries = first["items"] + second["items"]
    serialized = sensitive_text(all_summaries)
    for forbidden in (
        "openid",
        "secret-openid",
        "private first question",
        "newer private question",
        "legacy",
        "unconsented private question",
        "old-policy private question",
        "user_id",
        "token",
    ):
        assert forbidden not in serialized
    assert all(item["account_ref"].startswith("wx-") for item in all_summaries)


def test_detail_is_bounded_to_wechat_messages_and_safe_sources(tmp_path):
    from open_webui.there_integration.miniprogram_chats import MiniProgramChatStore

    database = tmp_path / "mini.sqlite"
    create_store(database)
    store = MiniProgramChatStore(database, policy_version=POLICY_VERSION)
    metadata = store.get_chat(CHAT_OLDER)
    assert metadata and metadata["message_count"] == 2
    page = store.list_messages(CHAT_OLDER, limit=1)
    assert page and page["items"][0]["content"] == "Question one"
    assert page["next_cursor"]
    remainder = store.list_messages(CHAT_OLDER, cursor=page["next_cursor"], limit=10)
    assert remainder and len(remainder["items"]) == 1
    answer = remainder["items"][0]
    assert answer["content"] == "Answer one <script>alert(1)</script>"
    assert answer["sources"] == [{"url": "https://example.com/paper", "name": "Paper"}]
    assert store.get_chat(CHAT_LEGACY) is None
    assert store.list_messages(CHAT_LEGACY) is None
    assert store.get_chat(CHAT_OLD_POLICY) is None
    assert store.list_messages(CHAT_OLD_POLICY) is None


def test_deleted_chat_immediately_disappears_from_admin_view(tmp_path):
    from open_webui.there_integration.miniprogram_chats import MiniProgramChatStore

    database = tmp_path / "mini.sqlite"
    create_store(database)
    store = MiniProgramChatStore(database, policy_version=POLICY_VERSION)
    assert store.get_chat(CHAT_OLDER)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("DELETE FROM chat WHERE id = ?", (CHAT_OLDER,))
    connection.commit()
    connection.close()
    assert store.get_chat(CHAT_OLDER) is None
    assert store.list_messages(CHAT_OLDER) is None


def test_store_is_scoped_to_the_configured_policy_version(tmp_path):
    from open_webui.there_integration.miniprogram_chats import (
        MiniProgramChatStore,
        MiniProgramChatStoreError,
    )

    database = tmp_path / "mini.sqlite"
    create_store(database)
    stale = MiniProgramChatStore(database, policy_version="2026-09-03")
    assert [item["id"] for item in stale.list_chats()["items"]] == [CHAT_OLD_POLICY]
    assert stale.get_chat(CHAT_OLDER) is None
    assert stale.list_messages(CHAT_OLDER) is None

    with pytest.raises(MiniProgramChatStoreError, match="policy is unavailable"):
        MiniProgramChatStore(database, policy_version="").list_chats()


def test_invalid_cursor_and_schema_fail_closed(tmp_path):
    from open_webui.there_integration.miniprogram_chats import (
        MiniProgramChatStore,
        MiniProgramChatStoreError,
    )

    database = tmp_path / "mini.sqlite"
    create_store(database)
    with pytest.raises(ValueError, match="cursor is invalid"):
        MiniProgramChatStore(database, policy_version=POLICY_VERSION).list_chats(cursor="not-base64!")
    broken = tmp_path / "broken.sqlite"
    sqlite3.connect(broken).close()
    if os.name != "nt":
        broken.chmod(0o600)
    with pytest.raises(MiniProgramChatStoreError, match="schema is invalid"):
        MiniProgramChatStore(broken, policy_version=POLICY_VERSION).list_chats()

    corrupt = tmp_path / "corrupt.sqlite"
    create_store(corrupt)
    connection = sqlite3.connect(corrupt)
    connection.execute(
        "UPDATE chat SET id = 'not-a-uuid' WHERE id = ?",
        (CHAT_NEWER,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(MiniProgramChatStoreError, match="invalid data"):
        MiniProgramChatStore(corrupt, policy_version=POLICY_VERSION).list_chats()


@pytest.fixture(scope="module")
def router_module(tmp_path_factory):
    environment = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("mini-admin-router")
    for key, value in {
        "DATA_DIR": str(root / "data"),
        "STATIC_DIR": str(root / "static"),
        "FRONTEND_BUILD_DIR": str(root / "no-build"),
        "DATABASE_URL": f"sqlite:///{root / 'openwebui.sqlite'}",
        "ENABLE_DB_MIGRATIONS": "false",
        "OFFLINE_MODE": "true",
        "WEBUI_SECRET_KEY": "mini-admin-isolated-test-secret",
    }.items():
        environment.setenv(key, value)
    from open_webui.routers import there_miniprogram_admin

    yield there_miniprogram_admin
    environment.undo()


def test_router_requires_admin_feature_and_never_audits_content(router_module, monkeypatch, tmp_path):
    from fastapi import FastAPI, HTTPException, Response

    database = tmp_path / "mini.sqlite"
    create_store(database)
    monkeypatch.setenv("BUILDSTUDIO_MINIPROGRAM_DATABASE_PATH", str(database))
    monkeypatch.setenv("BUILDSTUDIO_MINIPROGRAM_POLICY_VERSION", POLICY_VERSION)
    audits = []
    monkeypatch.setattr(router_module, "_write_audit", lambda *args, **kwargs: audits.append(kwargs))
    monkeypatch.setattr(router_module, "wechat_admin_audit_is_ready", lambda: True)

    async def admin():
        return SimpleNamespace(id="admin-id", role="admin")

    async def ordinary_user():
        raise HTTPException(401, "Access prohibited")

    async def scenario():
        app = FastAPI()
        app.include_router(router_module.router, prefix="/api/v1/there/admin/wechat")
        app.add_middleware(router_module.AdminWeChatNoStoreMiddleware)

        @app.get("/api/v1/there/admin/other")
        async def unrelated_route():
            return Response("ok", headers={"Cache-Control": "public, max-age=60"})

        app.dependency_overrides[router_module.get_admin_user] = admin
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://there.test") as client:
            monkeypatch.setenv("ENABLE_ADMIN_CHAT_ACCESS", "true")
            monkeypatch.setattr(router_module, "ENABLE_ADMIN_CHAT_ACCESS", True, raising=False)
            monkeypatch.delenv("ENABLE_ADMIN_WECHAT_CHAT_ACCESS", raising=False)
            response = await client.get("/api/v1/there/admin/wechat/chats")
            assert response.status_code == 403

            monkeypatch.setenv("ENABLE_ADMIN_WECHAT_CHAT_ACCESS", "enabled")
            response = await client.get("/api/v1/there/admin/wechat/chats")
            assert response.status_code == 403

            monkeypatch.setenv("ENABLE_ADMIN_WECHAT_CHAT_ACCESS", "false")
            response = await client.get("/api/v1/there/admin/wechat/chats")
            assert response.status_code == 403
            assert response.headers["cache-control"] == "private, no-store"

            monkeypatch.setenv("ENABLE_ADMIN_CHAT_ACCESS", "false")
            monkeypatch.setattr(router_module, "ENABLE_ADMIN_CHAT_ACCESS", False, raising=False)
            monkeypatch.setenv("ENABLE_ADMIN_WECHAT_CHAT_ACCESS", "true")
            response = await client.get("/api/v1/there/admin/wechat/chats", params={"cursor": "not-base64!"})
            assert response.status_code == 422
            assert response.headers["cache-control"] == "private, no-store"

            response = await client.get("/api/v1/there/admin/wechat/chats")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
            assert len(response.json()["items"]) == 2

            response = await client.get("/api/v1/there/admin/wechat/chats", params={"limit": 0})
            assert response.status_code == 422
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers["pragma"] == "no-cache"

            response = await client.get("/api/v1/there/admin/wechat/chats/not-a-uuid")
            assert response.status_code == 422
            assert response.headers["cache-control"] == "private, no-store"

            response = await client.get(f"/api/v1/there/admin/wechat/chats/{CHAT_OLDER}/messages")
            assert response.status_code == 200
            assert "Question one" in response.text
            assert all("content" not in sensitive_text(item) for item in audits)
            assert all("openid" not in sensitive_text(item) for item in audits)

            response = await client.get("/api/v1/there/admin/wechat/chats/99999999-9999-4999-8999-999999999999")
            assert response.status_code == 404
            assert response.headers["cache-control"] == "private, no-store"

            app.dependency_overrides[router_module.get_admin_user] = ordinary_user
            response = await client.get("/api/v1/there/admin/wechat/chats")
            assert response.status_code == 401
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers["pragma"] == "no-cache"

            response = await client.get("/api/v1/there/admin/other")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "public, max-age=60"
            assert "pragma" not in response.headers

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/there/admin/wechat/chats",
        f"/api/v1/there/admin/wechat/chats/{CHAT_OLDER}",
        f"/api/v1/there/admin/wechat/chats/{CHAT_OLDER}/messages",
    ),
)
def test_router_fails_closed_before_database_access_when_audit_is_unavailable(
    router_module,
    monkeypatch,
    path,
):
    from fastapi import FastAPI

    monkeypatch.setenv("ENABLE_ADMIN_WECHAT_CHAT_ACCESS", "true")
    monkeypatch.setattr(router_module, "wechat_admin_audit_is_ready", lambda: False)

    def forbidden_store():
        raise AssertionError("database must not be opened without a durable audit sink")

    monkeypatch.setattr(router_module, "MiniProgramChatStore", forbidden_store)

    async def admin():
        return SimpleNamespace(id="admin-id", role="admin")

    async def scenario():
        app = FastAPI()
        app.include_router(router_module.router, prefix="/api/v1/there/admin/wechat")
        app.dependency_overrides[router_module.get_admin_user] = admin
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://there.test") as client:
            response = await client.get(path)

        assert response.status_code == 503
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json() == {"detail": "Administrator audit trail is temporarily unavailable"}

    asyncio.run(scenario())


def test_router_never_returns_chat_data_when_the_audit_write_fails(
    router_module,
    monkeypatch,
):
    from fastapi import FastAPI

    sensitive_value = "private chat content must not cross the response boundary"
    store_reads = []

    class StoreWhoseReadMustBeAudited:
        def list_chats(self, *, cursor, limit):
            store_reads.append((cursor, limit))
            return {"items": [{"content": sensitive_value}], "next_cursor": None}

    def failed_audit_write(*_args, **_kwargs):
        raise OSError("synthetic full audit filesystem")

    monkeypatch.setenv("ENABLE_ADMIN_WECHAT_CHAT_ACCESS", "true")
    monkeypatch.setattr(router_module, "wechat_admin_audit_is_ready", lambda: True)
    monkeypatch.setattr(router_module, "MiniProgramChatStore", StoreWhoseReadMustBeAudited)
    monkeypatch.setattr(router_module.audit_logger, "write", failed_audit_write)
    monkeypatch.setattr(router_module.logger, "exception", lambda *_args, **_kwargs: None)

    async def admin():
        return SimpleNamespace(id="admin-id", role="admin")

    async def scenario():
        app = FastAPI()
        app.include_router(router_module.router, prefix="/api/v1/there/admin/wechat")
        app.add_middleware(router_module.AdminWeChatNoStoreMiddleware)
        app.dependency_overrides[router_module.get_admin_user] = admin
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://there.test") as client:
            response = await client.get("/api/v1/there/admin/wechat/chats")

        assert store_reads == [(None, 50)]
        assert response.status_code == 503
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json() == {"detail": "Administrator audit trail is temporarily unavailable"}
        assert sensitive_value not in response.text

    asyncio.run(scenario())
