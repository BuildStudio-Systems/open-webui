"""Pinned API contract and trust-boundary tests; no live service is contacted."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from open_webui.there_integration import weknora
from open_webui.there_integration.weknora import WeKnoraClient, WeKnoraError


class WeKnoraClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.key_file = Path(self.temporary.name) / "api-key"
        self.secret = "wk_test_key_that_must_never_escape"
        self.key_file.write_text(self.secret + "\n", encoding="ascii")
        self.key_file.chmod(0o600)
        self.requests = []

    def tearDown(self):
        self.temporary.cleanup()

    def client(self, responses=None, *, handler=None):
        queued = list(responses or [])

        async def dispatch(request):
            self.requests.append(request)
            self.assertEqual(request.url.host, "127.0.0.1")
            self.assertEqual(request.url.port, 8894)
            self.assertEqual(request.headers["X-API-Key"], self.secret)
            if handler is not None:
                return await handler(request)
            return queued.pop(0)

        return WeKnoraClient(api_key_file=self.key_file, transport=httpx.MockTransport(dispatch))

    def response(self, data, status=200):
        return httpx.Response(status, json={"success": True, "data": data})

    def document(self, kb="kb-1", identifier="doc-1"):
        return {"id": identifier, "knowledge_base_id": kb, "title": "Document", "parse_status": "completed"}

    def base(self, wiki=False):
        return {
            "id": "kb-1",
            "name": "Library",
            "description": "Prior description",
            "indexing_strategy": {"wiki_enabled": wiki, "graph_enabled": False},
        }

    async def test_endpoint_accepts_only_literal_loopback_at_pinned_port(self):
        for url in (
            "https://127.0.0.1:8894",
            "http://localhost:8894",
            "http://127.0.0.2:8894",
            "http://127.0.0.1:8895",
            "http://127.0.0.1:8894/x",
            "http://127.0.0.1:8894?x=1",
            "http://name:secret@127.0.0.1:8894",
            "http://127.0.0.1:8894#fragment",
            "http://external.invalid:8894",
            "http://127.0.0.1:99999",
        ):
            with self.subTest(url=url), self.assertRaises(WeKnoraError) as raised:
                WeKnoraClient(base_url=url, api_key_file=self.key_file)
            self.assertEqual(raised.exception.code, "WEKNORA_ENDPOINT_INVALID")
        self.assertEqual(WeKnoraClient("http://[::1]:8894/api/v1/").base_url, "http://[::1]:8894/api/v1")

    async def test_missing_or_unsafe_credentials_fail_before_network(self):
        with patch.dict(os.environ, {"THERE_WEKNORA_API_KEY_FILE": ""}):
            with self.assertRaises(WeKnoraError) as raised:
                await WeKnoraClient().list_bases()
            self.assertEqual(raised.exception.status_code, 503)
        for value in ("wk_bad\r\nInjected: value", "short", "", "wk_含非ASCII"):
            self.key_file.write_text(value, encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(WeKnoraError):
                await self.client().list_bases()
        self.assertEqual(self.requests, [])

    async def test_environment_credentials_and_rotation_are_read_per_request(self):
        async def handler(request):
            return self.response([])

        client = self.client(handler=handler)
        client._key_path = None
        with patch.dict(os.environ, {"THERE_WEKNORA_API_KEY_FILE": str(self.key_file)}):
            await client.list_bases()
            self.secret = "wk_rotated_key_with_sufficient_length"
            self.key_file.write_text(self.secret, encoding="ascii")
            await client.list_bases()
        self.assertNotEqual(self.requests[0].headers["X-API-Key"], self.requests[1].headers["X-API-Key"])

    async def test_requests_reuse_one_verifying_tls_context_but_not_clients(self):
        async def handler(request):
            return self.response([])

        created = []
        real_client = httpx.AsyncClient

        def recording_client(**kwargs):
            created.append(kwargs.get("verify"))
            return real_client(**kwargs)

        client = self.client(handler=handler)
        with patch.object(weknora.httpx, "AsyncClient", side_effect=recording_client):
            await client.list_bases()
            await client.list_bases()
        # A new client per request (unchanged lifecycle), never a rebuilt CA bundle.
        self.assertEqual(len(created), 2)
        self.assertTrue(all(context is weknora._TLS_CONTEXT for context in created))
        import ssl

        self.assertEqual(weknora._TLS_CONTEXT.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(weknora._TLS_CONTEXT.check_hostname)

    @unittest.skipIf(os.name == "nt", "POSIX permission boundary")
    async def test_world_readable_key_rejected(self):
        self.key_file.chmod(0o644)
        with self.assertRaises(WeKnoraError):
            await self.client().list_bases()
        self.assertEqual(self.requests, [])

    async def test_symlink_key_rejected(self):
        linked = self.key_file.parent / "linked"
        try:
            linked.symlink_to(self.key_file)
        except OSError:
            self.skipTest("Host does not permit creating symlinks")
        client = self.client()
        client._key_path = linked
        with self.assertRaises(WeKnoraError):
            await client.list_bases()
        self.assertEqual(self.requests, [])

    async def test_redirect_rejected_without_followup(self):
        client = self.client([httpx.Response(307, headers={"location": "http://external.invalid/steal"})])
        with self.assertRaises(WeKnoraError) as raised:
            await client.list_bases()
        self.assertEqual(raised.exception.code, "WEKNORA_REDIRECT_REJECTED")
        self.assertEqual(len(self.requests), 1)

    async def test_errors_redact_response_details_and_credentials(self):
        for status, expected in ((400, 422), (401, 503), (403, 503), (404, 404), (409, 409), (429, 429), (500, 502)):
            client = self.client([httpx.Response(status, text=f"password={self.secret}; /server/private/path")])
            with self.subTest(status=status), self.assertRaises(WeKnoraError) as raised:
                await client.list_bases()
            self.assertEqual(raised.exception.status_code, expected)
            self.assertNotIn(self.secret, repr(raised.exception))
            self.assertNotIn("/server", str(raised.exception))

    async def test_invalid_json_and_false_success_fail_closed(self):
        for response in (
            httpx.Response(200, text="not JSON"),
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"success": False, "message": self.secret}),
            httpx.Response(200, json={"error": self.secret}),
        ):
            with self.assertRaises(WeKnoraError) as raised:
                await self.client([response]).list_bases()
            self.assertEqual(raised.exception.code, "WEKNORA_RESPONSE_INVALID")
            self.assertNotIn(self.secret, str(raised.exception))

    async def test_response_size_and_content_encoding_boundaries(self):
        with patch.object(weknora, "MAX_RESPONSE_BYTES", 64):
            for response in (
                httpx.Response(200, content=b"x" * 65),
                httpx.Response(200, content=b"{}", headers={"content-length": "100"}),
            ):
                with self.assertRaises(WeKnoraError) as raised:
                    await self.client([response]).list_bases()
                self.assertEqual(raised.exception.code, "WEKNORA_RESPONSE_TOO_LARGE")
        response = httpx.Response(200, content=b"{}")
        response.headers["content-encoding"] = "gzip"
        with self.assertRaises(WeKnoraError):
            await self.client([response]).list_bases()

    async def test_chunked_response_limit_applies_without_content_length(self):
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"a" * 40
                yield b"b" * 40

        with patch.object(weknora, "MAX_RESPONSE_BYTES", 64):
            with self.assertRaises(WeKnoraError) as raised:
                await self.client([httpx.Response(200, stream=Stream())]).list_bases()
            self.assertEqual(raised.exception.code, "WEKNORA_RESPONSE_TOO_LARGE")

    async def test_wall_clock_deadline_and_network_error_are_safe(self):
        async def slow(_request):
            await asyncio.sleep(1)
            return self.response([])

        with patch.object(weknora, "REQUEST_DEADLINE_SECONDS", 0.01):
            with self.assertRaises(WeKnoraError) as raised:
                await self.client(handler=slow).list_bases()
            self.assertEqual(raised.exception.code, "WEKNORA_TIMEOUT")

        async def fail(request):
            raise httpx.ConnectError(self.secret, request=request)

        with self.assertRaises(WeKnoraError) as raised:
            await self.client(handler=fail).list_bases()
        self.assertEqual(raised.exception.code, "WEKNORA_UNAVAILABLE")
        self.assertNotIn(self.secret, str(raised.exception))

    async def test_base_projection_removes_provider_secrets_and_local_paths(self):
        base = {
            **self.base(),
            "storage_config": {"secret_key": "hidden"},
            "vlm_config": {"api_key": "hidden"},
            "tenant_id": 10,
            "file_path": "/etc/private",
        }
        result = await self.client([self.response([base])]).list_bases()
        self.assertEqual(result["data"], [self.base()])

    async def test_wrong_base_response_is_rejected(self):
        with self.assertRaises(WeKnoraError) as raised:
            await self.client([self.response({**self.base(), "id": "other-kb"})]).get_base("kb-1")
        self.assertEqual(raised.exception.code, "WEKNORA_RESPONSE_INVALID")

    async def test_create_base_uses_document_defaults_and_rejects_unconfigured_models(self):
        client = self.client([self.response(self.base(), 201)])
        with patch.dict(os.environ, {
            "THERE_WEKNORA_EMBEDDING_MODEL_ID": "", "THERE_WEKNORA_EMBEDDING_MODEL": "",
        }):
            await client.create_base(name="Library")
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(payload["embedding_model_id"], "buildstudio-weknora-bge-m3")
        self.assertEqual(
            payload["indexing_strategy"],
            {"vector_enabled": True, "keyword_enabled": True, "wiki_enabled": False, "graph_enabled": False},
        )
        with patch.dict(os.environ, {"THERE_WEKNORA_SUMMARY_MODEL_ID": ""}):
            for kwargs in ({"base_type": "wiki"}, {"graph_enabled": True}):
                with self.assertRaises(WeKnoraError) as raised:
                    await client.create_base(name="New", **kwargs)
                self.assertEqual(raised.exception.code, "WEKNORA_MODEL_REQUIRED")

    async def test_create_base_resolves_model_aliases_and_explicit_override(self):
        for current, legacy, explicit, expected in (
            ("custom-model", "", "", "custom-model"),
            ("", "legacy-model", "", "legacy-model"),
            ("same-model", "same-model", "", "same-model"),
            ("default-model", "", "explicit-model", "explicit-model"),
            ("conflicting-current", "conflicting-legacy", "explicit-model", "explicit-model"),
        ):
            with self.subTest(current=current, legacy=legacy, explicit=explicit), patch.dict(os.environ, {
                "THERE_WEKNORA_EMBEDDING_MODEL_ID": current,
                "THERE_WEKNORA_EMBEDDING_MODEL": legacy,
            }):
                await self.client([self.response(self.base(), 201)]).create_base(
                    name="Library", embedding_model_id=explicit,
                )
                self.assertEqual(json.loads(self.requests[-1].content)["embedding_model_id"], expected)

    async def test_model_configuration_errors_never_call_upstream(self):
        for current, legacy, expected in (
            ("current-model", "legacy-model", "WEKNORA_MODEL_CONFIG_CONFLICT"),
            ("bad/model", "", "WEKNORA_MODEL_CONFIG_INVALID"),
            ("", "bad model", "WEKNORA_MODEL_CONFIG_INVALID"),
        ):
            with self.subTest(current=current, legacy=legacy), patch.dict(os.environ, {
                "THERE_WEKNORA_EMBEDDING_MODEL_ID": current,
                "THERE_WEKNORA_EMBEDDING_MODEL": legacy,
            }):
                with self.assertRaises(WeKnoraError) as raised:
                    await self.client().create_base(name="Library")
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.requests, [])

    async def test_update_base_preserves_omitted_required_fields(self):
        client = self.client([self.response(self.base()), self.response(self.base())])
        await client.update_base("kb-1", description="Changed")
        self.assertEqual(json.loads(self.requests[-1].content), {"name": "Library", "description": "Changed"})
        self.assertEqual(self.requests[-1].method, "PUT")

    async def test_manual_ingest_uses_pinned_contract_and_redacts_processing_error(self):
        document = {
            **self.document(),
            "file_path": "/var/private",
            "error_message": f"connection failed {self.secret}",
            "custom_metadata": {"api_key": self.secret, "note": "keep"},
        }
        result = await self.client([self.response(document, 201)]).create_manual_document(
            "kb-1", title="Paper", content="# Data", status="draft"
        )
        self.assertEqual(self.requests[0].url.path, "/api/v1/knowledge-bases/kb-1/knowledge/manual")
        self.assertEqual(
            json.loads(self.requests[0].content),
            {"title": "Paper", "content": "# Data", "status": "draft", "channel": "there"},
        )
        self.assertNotIn("file_path", result["data"])
        self.assertEqual(result["data"]["error_message"], "Document processing failed.")
        self.assertEqual(result["data"]["custom_metadata"], {"note": "keep"})

    async def test_upload_uses_bytes_not_server_filesystem_paths(self):
        await self.client([self.response(self.document())]).upload_document(
            "kb-1", filename="paper.txt", content=b"Paper text", content_type="text/plain"
        )
        request = self.requests[0]
        self.assertTrue(request.headers["content-type"].startswith("multipart/form-data;"))
        body = await request.aread()
        self.assertIn(b'filename="paper.txt"', body)
        self.assertIn(b"Paper text", body)
        self.assertIn(b"enable_multimodel", body)
        for filename in ("../secret", "..\\secret", "x\r\nInjected", "."):
            with self.assertRaises(WeKnoraError):
                await self.client().upload_document("kb-1", filename=filename, content=b"x")

    async def test_cross_kb_document_never_read_deleted_or_reparsed(self):
        for operation in ("get_document", "delete_document", "reparse_document", "list_chunks"):
            client = self.client([self.response(self.document(kb="other-kb"))])
            prior_count = len(self.requests)
            with self.subTest(operation=operation), self.assertRaises(WeKnoraError) as raised:
                await getattr(client, operation)("kb-1", "doc-1")
            self.assertEqual(raised.exception.status_code, 404)
            self.assertEqual(len(self.requests), prior_count + 1)
            self.assertEqual(self.requests[-1].method, "GET")

    async def test_delete_document_checks_exact_owner_then_mutates(self):
        result = await self.client([self.response(self.document()), httpx.Response(204)]).delete_document(
            "kb-1", "doc-1"
        )
        self.assertEqual([r.method for r in self.requests], ["GET", "DELETE"])
        self.assertEqual(result, {"success": True, "data": None})

    async def test_search_uses_query_text_and_validates_finite_thresholds(self):
        await self.client([self.response([])]).search("kb-1", "test-time scaling", limit=5)
        self.assertEqual(self.requests[0].url.path, "/api/v1/knowledge-bases/kb-1/hybrid-search")
        self.assertEqual(
            json.loads(self.requests[0].content),
            {"query_text": "test-time scaling", "match_count": 5, "vector_threshold": 0.5, "keyword_threshold": 0.3},
        )
        for threshold in (float("nan"), float("inf"), -1, 2, True):
            with self.assertRaises(WeKnoraError):
                await self.client().search("kb-1", "test", vector_threshold=threshold)

    async def test_ids_pagination_and_payload_bounds_reject_before_request(self):
        for identifier in ("../keys", "kb?x=1", "kb%2fother", "kb/other", "", "kb\\other"):
            with self.assertRaises(WeKnoraError):
                await self.client().get_base(identifier)
        for page in (0, True, 1.5):
            with self.assertRaises(WeKnoraError):
                await self.client().list_documents("kb-1", page=page)
        with patch.object(weknora, "MAX_TEXT_BYTES", 8):
            with self.assertRaises(WeKnoraError):
                await self.client().create_manual_document("kb-1", title="Title", content="x" * 9)
        self.assertEqual(self.requests, [])

    async def test_chunk_update_verifies_both_document_and_chunk_ownership(self):
        client = self.client(
            [self.response(self.document()), self.response({"id": "chunk-1", "knowledge_id": "other-doc"})]
        )
        with self.assertRaises(WeKnoraError) as raised:
            await client.update_chunk("kb-1", "doc-1", "chunk-1", content="replacement", expected_revision=2)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual([r.method for r in self.requests], ["GET", "GET"])

    async def test_chunk_update_sends_revision_guard(self):
        client = self.client(
            [
                self.response(self.document()),
                self.response({"id": "chunk-1", "knowledge_id": "doc-1"}),
                self.response({"id": "chunk-1"}),
            ]
        )
        await client.update_chunk("kb-1", "doc-1", "chunk-1", content="replacement", expected_revision=2)
        self.assertEqual(self.requests[-1].url.path, "/api/v1/chunks/doc-1/chunk-1")
        self.assertEqual(json.loads(self.requests[-1].content), {"content": "replacement", "expected_revision": 2})

    async def test_faq_contract(self):
        client = self.client([self.response({"id": 100000001}), self.response([])])
        await client.create_faq_entry("kb-1", question="Why?", answers=["Because."], similar_questions=["How come?"])
        await client.search_faq("kb-1", "Why?")
        self.assertEqual(self.requests[0].url.path, "/api/v1/knowledge-bases/kb-1/faq/entry")
        self.assertEqual(
            json.loads(self.requests[0].content),
            {"standard_question": "Why?", "answers": ["Because."], "similar_questions": ["How come?"]},
        )
        self.assertEqual(json.loads(self.requests[1].content)["query_text"], "Why?")

    async def test_faq_update_and_delete_keep_numeric_ids_in_scoped_routes(self):
        client = self.client([self.response({"id": 100000001}), httpx.Response(200, json={"success": True})])
        await client.update_faq_entry("kb-1", 100000001, question="Why?", answers=["Because."])
        await client.delete_faq_entries("kb-1", [100000001, 100000002])
        self.assertEqual(self.requests[0].url.path, "/api/v1/knowledge-bases/kb-1/faq/entries/100000001")
        self.assertEqual(json.loads(self.requests[1].content), {"ids": [100000001, 100000002]})
        self.assertEqual(self.requests[1].method, "DELETE")
        with self.assertRaises(WeKnoraError):
            await client.delete_faq_entries("kb-1", [True])

    async def test_wiki_create_uses_published_status_and_no_caller_tenant(self):
        client = self.client([self.response(self.base(True)), httpx.Response(201, json={"slug": "concept/rag"})])
        await client.create_wiki_page("kb-1", slug="concept/rag", title="RAG", content="Content", status="published")
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(payload["status"], "published")
        self.assertEqual(self.requests[-1].url.path, "/api/v1/knowledgebase/kb-1/wiki/pages")
        self.assertNotIn("tenant_id", payload)
        with self.assertRaises(WeKnoraError):
            await client.create_wiki_page("kb-1", slug="concept/rag", title="RAG", content="Content", status="publish")

    async def test_wiki_reports_disabled_and_uses_singular_path_with_version_guard(self):
        with self.assertRaises(WeKnoraError) as raised:
            await self.client([self.response(self.base(False))]).list_wiki_pages("kb-1")
        self.assertEqual(raised.exception.code, "WEKNORA_WIKI_DISABLED")
        client = self.client(
            [self.response(self.base(True)), httpx.Response(200, json={"slug": "concept/rag", "content": "new"})]
        )
        await client.update_wiki_page("kb-1", "concept/rag", content="new", expected_version=2)
        self.assertEqual(self.requests[-1].url.path, "/api/v1/knowledgebase/kb-1/wiki/pages/concept/rag")
        self.assertEqual(json.loads(self.requests[-1].content), {"content": "new", "version": 2})
        for slug in ("../secret", "concept/../secret", "concept/%2e%2e/secret", "/leading", "trailing/"):
            with self.assertRaises(WeKnoraError):
                await self.client().get_wiki_page("kb-1", slug)


if __name__ == "__main__":
    unittest.main()
