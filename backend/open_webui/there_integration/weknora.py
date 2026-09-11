"""Bounded THERE adapter for pinned WeKnora v0.7.2.

The caller must authorize the THERE workspace and its mapped knowledge-base ID.
No browser headers, arbitrary URLs, filesystem upload paths, or provider secrets
are accepted. Document operations additionally verify upstream KB ownership.
Methods preserve upstream response envelopes; sensitive configuration and local
paths are removed. A returned configuration is not a runtime readiness claim.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8894"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
REQUEST_DEADLINE_SECONDS = 60.0
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_PRIVATE_FIELDS = {
    "api_key",
    "apikey",
    "api_keys",
    "password",
    "secret",
    "secrets",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "credentials",
    "credential",
    "secret_key",
    "access_key",
    "private_key",
    "file_path",
    "local_path",
    "base_url",
    "storage_config",
    "cos_config",
    "storage_provider_config",
    "storage_backend",
    "error",
    "details",
    "traceback",
    "stack_trace",
}
_BASE_FIELDS = {
    "id",
    "name",
    "type",
    "description",
    "embedding_model_id",
    "summary_model_id",
    "chunking_config",
    "indexing_strategy",
    "created_at",
    "updated_at",
    "knowledge_count",
    "chunk_count",
    "is_processing",
    "processing_count",
    "faq_config",
}
_DOCUMENT_FIELDS = {
    "id",
    "knowledge_base_id",
    "type",
    "title",
    "description",
    "source",
    "channel",
    "parse_status",
    "summary_status",
    "enable_status",
    "pending_subtasks_count",
    "embedding_model_id",
    "file_name",
    "folder_path",
    "file_type",
    "file_size",
    "file_hash",
    "storage_size",
    "custom_metadata",
    "tags",
    "created_at",
    "updated_at",
    "processed_at",
    "error_message",
    "knowledge_base_name",
}


class WeKnoraError(RuntimeError):
    """Stable, safe-to-display error, deliberately without raw upstream context."""

    def __init__(self, code: str, status_code: int = 502, message: str = "Knowledge service request failed."):
        self.code = code
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _invalid() -> WeKnoraError:
    return WeKnoraError("WEKNORA_ARGUMENT_INVALID", 422, "Invalid knowledge request.")


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise _invalid()
    return value


def default_embedding_model_id() -> str:
    """Use MODEL_ID, then the legacy MODEL alias, then the built-in default.

    Empty values are unset. Different nonempty aliases are a configuration
    error so an existing index is never silently switched to another model.
    """
    current = os.getenv("THERE_WEKNORA_EMBEDDING_MODEL_ID", "")
    legacy = os.getenv("THERE_WEKNORA_EMBEDDING_MODEL", "")
    if current and legacy and current != legacy:
        raise WeKnoraError(
            "WEKNORA_MODEL_CONFIG_CONFLICT", 503,
            "THERE_WEKNORA_EMBEDDING_MODEL_ID conflicts with the legacy "
            "THERE_WEKNORA_EMBEDDING_MODEL setting; remove the legacy setting or make both match.",
        )
    value = current or legacy or "buildstudio-weknora-bge-m3"
    if not _ID.fullmatch(value):
        raise WeKnoraError(
            "WEKNORA_MODEL_CONFIG_INVALID", 503,
            "Knowledge service embedding model ID is not configured correctly.",
        )
    return value


def _text(value: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (not empty and not value.strip()):
        raise _invalid()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise _invalid() from None
    if size > maximum:
        raise WeKnoraError("WEKNORA_PAYLOAD_TOO_LARGE", 413, "Knowledge request is too large.")
    return value


def _integer(value: int, minimum: int = 1, maximum: int = 100) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _threshold(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise _invalid()
    return float(value)


def _pagination(page: int, page_size: int) -> dict[str, int]:
    return {"page": _integer(page, maximum=100000), "page_size": _integer(page_size)}


def _sanitize(value: Any, secret: str = "", depth: int = 0) -> Any:
    if depth > 32:
        raise WeKnoraError("WEKNORA_RESPONSE_INVALID", 502, "Knowledge service returned an invalid response.")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in _PRIVATE_FIELDS or any(
                normalized.endswith(suffix) for suffix in ("_password", "_secret", "_token", "_api_key")
            ):
                continue
            if normalized == "error_message":
                result[key] = "Document processing failed." if item else ""
            else:
                result[key] = _sanitize(item, secret, depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize(item, secret, depth + 1) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[redacted]")
    return value


def _project(envelope: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    """Keep pagination wrappers while projecting all returned entity records."""

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            if "id" in value:
                return {key: item for key, item in value.items() if key in fields}
            return {
                key: visit(item)
                for key, item in value.items()
                if key in {"data", "list", "items", "total", "page", "page_size", "success", "has_more"}
            }
        return value

    return visit(envelope)


class WeKnoraClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key_file: str | Path | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        try:
            parsed = urlsplit(base_url)
            valid = (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "::1"}
                and parsed.port == 8894
                and parsed.path in {"", "/", "/api/v1", "/api/v1/"}
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise WeKnoraError(
                "WEKNORA_ENDPOINT_INVALID", 503, "Knowledge service endpoint is not configured correctly."
            )
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        self.base_url = f"http://{host}:8894/api/v1"
        self._key_path = api_key_file
        self._transport = transport

    async def __aenter__(self) -> WeKnoraClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def _read_key(self) -> str:
        value = self._key_path or os.getenv("THERE_WEKNORA_API_KEY_FILE", "")
        if not value:
            raise WeKnoraError("WEKNORA_NOT_CONFIGURED", 503, "Knowledge service credentials are not configured.")
        try:
            path = Path(value)
            before = path.lstat()
            if (
                not path.is_absolute()
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or not 1 <= before.st_size <= 4096
            ):
                raise ValueError()
            if os.name != "nt" and (before.st_mode & 0o077 or before.st_uid not in {0, os.geteuid()}):
                raise ValueError()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                current = os.fstat(descriptor)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                ):
                    raise ValueError()
                raw = os.read(descriptor, 4097)
            finally:
                os.close(descriptor)
            key = raw.decode("ascii").rstrip("\r\n")
            if not 8 <= len(key) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in key):
                raise ValueError()
            return key
        except (OSError, ValueError, TypeError, UnicodeError):
            raise WeKnoraError(
                "WEKNORA_NOT_CONFIGURED", 503, "Knowledge service credentials are not configured correctly."
            ) from None

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Only explicit methods below construct endpoints. Never accept a client URL.
        key = self._read_key()
        headers = {"X-API-Key": key, "Accept": "application/json", "Accept-Encoding": "identity"}
        content = None
        if payload is not None:
            try:
                content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise _invalid() from None
            if len(content) > MAX_TEXT_BYTES + 65536:
                raise WeKnoraError("WEKNORA_PAYLOAD_TOO_LARGE", 413, "Knowledge request is too large.")
            headers["Content-Type"] = "application/json"
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(45.0, connect=5.0),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client:
                    async with client.stream(
                        method,
                        self.base_url + endpoint,
                        headers=headers,
                        content=content,
                        params=params,
                        files=files,
                        data=form,
                    ) as response:
                        if 300 <= response.status_code < 400:
                            raise WeKnoraError(
                                "WEKNORA_REDIRECT_REJECTED", 502, "Knowledge service returned an unexpected redirect."
                            )
                        if not 200 <= response.status_code < 300:
                            status = response.status_code
                            if status in (401, 403):
                                raise WeKnoraError(
                                    "WEKNORA_CAPABILITY_DENIED",
                                    503,
                                    "Knowledge service permission is not configured for this operation.",
                                )
                            if status == 404:
                                raise WeKnoraError("WEKNORA_NOT_FOUND", 404, "Knowledge resource was not found.")
                            if status == 409:
                                raise WeKnoraError(
                                    "WEKNORA_CONFLICT", 409, "Knowledge resource changed; refresh before retrying."
                                )
                            if status == 429:
                                raise WeKnoraError(
                                    "WEKNORA_RATE_LIMITED", 429, "Knowledge service is busy; retry later."
                                )
                            raise WeKnoraError("WEKNORA_UPSTREAM_REJECTED", 422 if status in (400, 413, 422) else 502)
                        if response.status_code == 204:
                            return {"success": True, "data": None}
                        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                            raise WeKnoraError("WEKNORA_RESPONSE_INVALID")
                        length = response.headers.get("content-length")
                        if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                            raise WeKnoraError(
                                "WEKNORA_RESPONSE_TOO_LARGE", 502, "Knowledge service response is too large."
                            )
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            body.extend(chunk)
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise WeKnoraError(
                                    "WEKNORA_RESPONSE_TOO_LARGE", 502, "Knowledge service response is too large."
                                )
            result = json.loads(body)
            if not isinstance(result, dict) or result.get("success") is False or "error" in result:
                raise WeKnoraError("WEKNORA_RESPONSE_INVALID", 502, "Knowledge service returned an invalid response.")
            return _sanitize(result, key)
        except (httpx.TimeoutException, TimeoutError):
            raise WeKnoraError(
                "WEKNORA_TIMEOUT",
                504,
                "Knowledge service request timed out; a submitted operation may still be processing.",
            ) from None
        except httpx.HTTPError:
            raise WeKnoraError(
                "WEKNORA_UNAVAILABLE",
                503,
                "Knowledge service is unavailable; a submitted operation may still be processing.",
            ) from None
        except (ValueError, UnicodeError, RecursionError):
            raise WeKnoraError(
                "WEKNORA_RESPONSE_INVALID", 502, "Knowledge service returned an invalid response."
            ) from None

    async def list_bases(self) -> dict[str, Any]:
        return _project(await self._request("GET", "/knowledge-bases"), _BASE_FIELDS)

    async def create_base(
        self,
        *,
        name: str,
        description: str = "",
        base_type: str = "document",
        embedding_model_id: str = "",
        summary_model_id: str = "",
        wiki_enabled: bool = False,
        graph_enabled: bool = False,
    ) -> dict[str, Any]:
        if (
            base_type not in {"document", "faq", "wiki"}
            or type(wiki_enabled) is not bool
            or type(graph_enabled) is not bool
        ):
            raise _invalid()
        wiki_enabled = wiki_enabled or base_type == "wiki"
        summary_model_id = summary_model_id or os.getenv("THERE_WEKNORA_SUMMARY_MODEL_ID", "")
        if (wiki_enabled or graph_enabled) and not summary_model_id:
            raise WeKnoraError(
                "WEKNORA_MODEL_REQUIRED", 409, "Wiki and graph generation require a configured language model."
            )
        embedding_model_id = embedding_model_id or default_embedding_model_id()
        payload = {
            "name": _text(name, 256),
            "description": _text(description, 4096, empty=True),
            "type": base_type,
            "embedding_model_id": _identifier(embedding_model_id),
            "chunking_config": {"chunk_size": 1000, "chunk_overlap": 200},
            "indexing_strategy": {
                "vector_enabled": True,
                "keyword_enabled": True,
                "wiki_enabled": wiki_enabled,
                "graph_enabled": graph_enabled,
            },
        }
        if summary_model_id:
            payload["summary_model_id"] = _identifier(summary_model_id)
        if graph_enabled:
            payload["extract_config"] = {"enabled": True}
        return _project(await self._request("POST", "/knowledge-bases", payload=payload), _BASE_FIELDS)

    async def get_base(self, kb_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/knowledge-bases/{_identifier(kb_id)}")
        base = result.get("data")
        if not isinstance(base, dict) or base.get("id") != kb_id:
            raise WeKnoraError("WEKNORA_RESPONSE_INVALID")
        return _project(result, _BASE_FIELDS)

    async def update_base(
        self, kb_id: str, *, name: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        changes = {}
        if name is not None:
            changes["name"] = _text(name, 256)
        if description is not None:
            changes["description"] = _text(description, 4096, empty=True)
        if not changes:
            raise _invalid()
        # Pinned PUT requires name and replaces description. Preserve omitted
        # fields, and never echo engine credentials/configuration back upstream.
        if name is None or description is None:
            current = (await self.get_base(kb_id)).get("data")
            if not isinstance(current, dict):
                raise WeKnoraError("WEKNORA_RESPONSE_INVALID")
            changes.setdefault("name", _text(current.get("name"), 256))
            changes.setdefault("description", _text(current.get("description", ""), 4096, empty=True))
        return _project(
            await self._request("PUT", f"/knowledge-bases/{_identifier(kb_id)}", payload=changes), _BASE_FIELDS
        )

    async def delete_base(self, kb_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/knowledge-bases/{_identifier(kb_id)}")

    async def list_documents(self, kb_id: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return _project(
            await self._request(
                "GET", f"/knowledge-bases/{_identifier(kb_id)}/knowledge", params=_pagination(page, page_size)
            ),
            _DOCUMENT_FIELDS,
        )

    async def get_document(self, kb_id: str, document_id: str) -> dict[str, Any]:
        _identifier(kb_id)
        result = await self._request("GET", f"/knowledge/{_identifier(document_id)}")
        document = result.get("data")
        if (
            not isinstance(document, dict)
            or document.get("id") != document_id
            or document.get("knowledge_base_id") != kb_id
        ):
            raise WeKnoraError("WEKNORA_NOT_FOUND", 404, "Knowledge resource was not found.")
        return _project(result, _DOCUMENT_FIELDS)

    async def create_manual_document(
        self, kb_id: str, *, title: str, content: str, status: str = "publish"
    ) -> dict[str, Any]:
        if status not in {"publish", "draft"}:
            raise _invalid()
        payload = {
            "title": _text(title, 512),
            "content": _text(content, MAX_TEXT_BYTES),
            "status": status,
            "channel": "there",
        }
        return _project(
            await self._request("POST", f"/knowledge-bases/{_identifier(kb_id)}/knowledge/manual", payload=payload),
            _DOCUMENT_FIELDS,
        )

    async def upload_document(
        self, kb_id: str, *, filename: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> dict[str, Any]:
        filename = _text(filename, 255)
        if (
            filename in {".", ".."}
            or any(char in filename for char in ("/", "\\", "\r", "\n"))
            or any(ord(char) < 32 for char in filename)
        ):
            raise _invalid()
        if not isinstance(content, bytes) or not content or len(content) > MAX_UPLOAD_BYTES:
            raise WeKnoraError("WEKNORA_PAYLOAD_TOO_LARGE", 413, "Upload must contain between 1 byte and 20 MiB.")
        if not isinstance(content_type, str) or not re.fullmatch(
            r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", content_type
        ):
            raise _invalid()
        return _project(
            await self._request(
                "POST",
                f"/knowledge-bases/{_identifier(kb_id)}/knowledge/file",
                files={"file": (filename, content, content_type)},
                form={"enable_multimodel": "false", "channel": "there"},
            ),
            _DOCUMENT_FIELDS,
        )

    async def delete_document(self, kb_id: str, document_id: str) -> dict[str, Any]:
        await self.get_document(kb_id, document_id)
        return await self._request("DELETE", f"/knowledge/{document_id}")

    async def reparse_document(self, kb_id: str, document_id: str) -> dict[str, Any]:
        await self.get_document(kb_id, document_id)
        return await self._request("POST", f"/knowledge/{document_id}/reparse", payload={})

    async def search(
        self, kb_id: str, query: str, *, limit: int = 10, vector_threshold: float = 0.5, keyword_threshold: float = 0.3
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/knowledge-bases/{_identifier(kb_id)}/hybrid-search",
            payload={
                "query_text": _text(query, 8192),
                "match_count": _integer(limit, maximum=50),
                "vector_threshold": _threshold(vector_threshold),
                "keyword_threshold": _threshold(keyword_threshold),
            },
        )

    async def list_chunks(self, kb_id: str, document_id: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        params = _pagination(page, page_size)
        await self.get_document(kb_id, document_id)
        return await self._request("GET", f"/chunks/{document_id}", params=params)

    async def _verify_chunk(self, kb_id: str, document_id: str, chunk_id: str) -> None:
        _identifier(chunk_id)
        await self.get_document(kb_id, document_id)
        result = await self._request("GET", f"/chunks/by-id/{chunk_id}")
        chunk = result.get("data")
        if not isinstance(chunk, dict) or chunk.get("id") != chunk_id or chunk.get("knowledge_id") != document_id:
            raise WeKnoraError("WEKNORA_NOT_FOUND", 404, "Knowledge resource was not found.")

    async def update_chunk(
        self, kb_id: str, document_id: str, chunk_id: str, *, content: str, expected_revision: int
    ) -> dict[str, Any]:
        payload = {
            "content": _text(content, MAX_TEXT_BYTES),
            "expected_revision": _integer(expected_revision, minimum=0, maximum=2147483647),
        }
        await self._verify_chunk(kb_id, document_id, chunk_id)
        return await self._request("PUT", f"/chunks/{document_id}/{chunk_id}", payload=payload)

    async def list_chunk_revisions(self, kb_id: str, document_id: str, chunk_id: str) -> dict[str, Any]:
        await self._verify_chunk(kb_id, document_id, chunk_id)
        return await self._request("GET", f"/chunks/{document_id}/{chunk_id}/revisions")

    async def revert_chunk(
        self, kb_id: str, document_id: str, chunk_id: str, *, revision: int, expected_revision: int
    ) -> dict[str, Any]:
        payload = {
            "revision": _integer(revision, minimum=0, maximum=2147483647),
            "expected_revision": _integer(expected_revision, minimum=0, maximum=2147483647),
        }
        await self._verify_chunk(kb_id, document_id, chunk_id)
        return await self._request("POST", f"/chunks/{document_id}/{chunk_id}/revert", payload=payload)

    async def delete_chunk(self, kb_id: str, document_id: str, chunk_id: str) -> dict[str, Any]:
        await self._verify_chunk(kb_id, document_id, chunk_id)
        return await self._request("DELETE", f"/chunks/{document_id}/{chunk_id}")

    async def list_faq_entries(self, kb_id: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return await self._request(
            "GET", f"/knowledge-bases/{_identifier(kb_id)}/faq/entries", params=_pagination(page, page_size)
        )

    async def create_faq_entry(
        self, kb_id: str, *, question: str, answers: list[str], similar_questions: list[str] | None = None
    ) -> dict[str, Any]:
        similar_questions = similar_questions or []
        if (
            not isinstance(answers, list)
            or not 1 <= len(answers) <= 20
            or not isinstance(similar_questions, list)
            or len(similar_questions) > 20
        ):
            raise _invalid()
        payload = {
            "standard_question": _text(question, 8192),
            "answers": [_text(answer, 32768) for answer in answers],
            "similar_questions": [_text(item, 8192) for item in similar_questions],
        }
        return await self._request("POST", f"/knowledge-bases/{_identifier(kb_id)}/faq/entry", payload=payload)

    async def search_faq(self, kb_id: str, query: str, *, limit: int = 10) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/knowledge-bases/{_identifier(kb_id)}/faq/search",
            payload={"query_text": _text(query, 8192), "match_count": _integer(limit, maximum=50)},
        )

    async def get_faq_entry(self, kb_id: str, entry_id: int) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/knowledge-bases/{_identifier(kb_id)}/faq/entries/{_integer(entry_id, maximum=9223372036854775807)}",
        )

    async def update_faq_entry(
        self,
        kb_id: str,
        entry_id: int,
        *,
        question: str,
        answers: list[str],
        similar_questions: list[str] | None = None,
    ) -> dict[str, Any]:
        similar_questions = similar_questions or []
        if (
            not isinstance(answers, list)
            or not 1 <= len(answers) <= 20
            or not isinstance(similar_questions, list)
            or len(similar_questions) > 20
        ):
            raise _invalid()
        payload = {
            "standard_question": _text(question, 8192),
            "answers": [_text(answer, 32768) for answer in answers],
            "similar_questions": [_text(item, 8192) for item in similar_questions],
        }
        return await self._request(
            "PUT",
            f"/knowledge-bases/{_identifier(kb_id)}/faq/entries/{_integer(entry_id, maximum=9223372036854775807)}",
            payload=payload,
        )

    async def delete_faq_entries(self, kb_id: str, entry_ids: list[int]) -> dict[str, Any]:
        if not isinstance(entry_ids, list) or not 1 <= len(entry_ids) <= 100:
            raise _invalid()
        ids = [_integer(item, maximum=9223372036854775807) for item in entry_ids]
        if len(set(ids)) != len(ids):
            raise _invalid()
        return await self._request("DELETE", f"/knowledge-bases/{_identifier(kb_id)}/faq/entries", payload={"ids": ids})

    async def _wiki_path(self, kb_id: str) -> str:
        base = (await self.get_base(kb_id)).get("data")
        indexing = base.get("indexing_strategy") if isinstance(base, dict) else None
        if not isinstance(indexing, dict) or indexing.get("wiki_enabled") is not True:
            raise WeKnoraError("WEKNORA_WIKI_DISABLED", 409, "Wiki is not enabled for this knowledge space.")
        return f"/knowledgebase/{kb_id}/wiki"

    @staticmethod
    def _slug(slug: str) -> str:
        _text(slug, 512)
        if (
            any(part in {"", ".", ".."} for part in slug.split("/"))
            or any(char in slug for char in ("\\", "%", "?", "#"))
            or any(ord(char) < 32 for char in slug)
        ):
            raise _invalid()
        return quote(slug, safe="/")

    async def list_wiki_pages(self, kb_id: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        params = _pagination(page, page_size)
        return await self._request("GET", await self._wiki_path(kb_id) + "/pages", params=params)

    async def get_wiki_page(self, kb_id: str, slug: str) -> dict[str, Any]:
        path = self._slug(slug)
        return await self._request("GET", await self._wiki_path(kb_id) + "/pages/" + path)

    async def create_wiki_page(
        self, kb_id: str, *, slug: str, title: str, content: str, page_type: str = "concept", status: str = "draft"
    ) -> dict[str, Any]:
        self._slug(slug)
        if page_type not in {"entity", "concept", "summary", "index", "synthesis", "comparison"} or status not in {
            "draft",
            "published",
            "archived",
        }:
            raise _invalid()
        payload = {
            "slug": slug,
            "title": _text(title, 512),
            "content": _text(content, MAX_TEXT_BYTES),
            "page_type": page_type,
            "status": status,
        }
        return await self._request("POST", await self._wiki_path(kb_id) + "/pages", payload=payload)

    async def delete_wiki_page(self, kb_id: str, slug: str) -> dict[str, Any]:
        path = self._slug(slug)
        return await self._request("DELETE", await self._wiki_path(kb_id) + "/pages/" + path)

    async def search_wiki(self, kb_id: str, query: str, *, limit: int = 10) -> dict[str, Any]:
        params = {"q": _text(query, 8192), "limit": _integer(limit, maximum=50)}
        return await self._request("GET", await self._wiki_path(kb_id) + "/search", params=params)

    async def get_wiki_index(self, kb_id: str) -> dict[str, Any]:
        return await self._request("GET", await self._wiki_path(kb_id) + "/index")

    async def get_wiki_graph(self, kb_id: str) -> dict[str, Any]:
        """Wiki page-link graph, not a claim that GraphRAG extraction is enabled."""
        return await self._request("GET", await self._wiki_path(kb_id) + "/graph")

    async def update_wiki_page(self, kb_id: str, slug: str, *, content: str, expected_version: int) -> dict[str, Any]:
        path = self._slug(slug)
        # Upstream calls this field `version`; zero disables concurrency checks.
        payload = {"content": _text(content, MAX_TEXT_BYTES), "version": _integer(expected_version, maximum=2147483647)}
        return await self._request("PUT", await self._wiki_path(kb_id) + "/pages/" + path, payload=payload)

    async def list_wiki_revisions(self, kb_id: str, slug: str) -> dict[str, Any]:
        path = self._slug(slug)
        return await self._request("GET", await self._wiki_path(kb_id) + "/revisions/" + path)

    async def revert_wiki_page(self, kb_id: str, slug: str, *, version: int) -> dict[str, Any]:
        self._slug(slug)
        payload = {"slug": slug, "version": _integer(version, maximum=2147483647)}
        return await self._request("POST", await self._wiki_path(kb_id) + "/revert", payload=payload)
