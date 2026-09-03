from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest

REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def events_module(tmp_path_factory):
    # Open WebUI validates its runtime environment while importing. Keep this
    # test isolated from developer and production data.
    data_dir = tmp_path_factory.mktemp("open-webui-provider-redaction")
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["WEBUI_SECRET_KEY"] = "local-open-webui-provider-redaction-test-secret"

    from open_webui import events
    from open_webui.utils import middleware

    return events, middleware


def test_provider_failure_logs_and_events_exclude_raw_error_material(events_module, monkeypatch, caplog):
    events, _ = events_module
    secret_prompt = "PRIVATE CUSTOMER PROMPT 91d6282a"
    signed_url = "https://provider.example/error?access_token=signed-secret-642f"
    bearer_token = "bearer-secret-08f7"
    captured = {}

    async def capture_event(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(events, "publish_event", capture_event)
    caplog.set_level(logging.WARNING, logger=events.__name__)

    asyncio.run(
        events.publish_model_provider_request_failed(
            object(),
            actor=object(),
            provider="openai-compatible",
            base_url=signed_url,
            api_key=bearer_token,
            status=404,
            requested_model=f"model-{secret_prompt}",
            upstream_error={
                "error": {
                    "code": "model_not_found",
                    "message": f"{secret_prompt} {signed_url} {bearer_token}",
                }
            },
        )
    )

    public_material = caplog.text + repr(captured)
    assert secret_prompt not in public_material
    assert signed_url not in public_material
    assert bearer_token not in public_material
    assert "access_token" not in public_material
    assert "upstream_message" not in public_material

    assert "status=404" in caplog.text
    assert "error_type=model_not_found" in caplog.text
    assert "code=model_not_found" in caplog.text
    assert captured["kwargs"]["data"] == {
        "error_type": "model_not_found",
        "status": 404,
        "provider": "openai-compatible",
        "upstream_error_code": "model_not_found",
    }


def test_untrusted_provider_and_error_code_are_reduced_to_fixed_values(events_module, monkeypatch, caplog):
    events, _ = events_module
    secret = "secret-provider-code-592e"
    captured = {}

    async def capture_event(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(events, "publish_event", capture_event)
    caplog.set_level(logging.WARNING, logger=events.__name__)

    asyncio.run(
        events.publish_model_provider_request_failed(
            object(),
            actor=None,
            provider=secret,
            base_url=f"https://{secret}.example",
            api_key=secret,
            status=429,
            requested_model=secret,
            upstream_error={"error": {"code": secret, "message": secret}},
        )
    )

    public_material = caplog.text + repr(captured)
    assert secret not in public_material
    assert "provider=unknown" in caplog.text
    assert "status=429" in caplog.text
    assert "error_type=rate_limited" in caplog.text
    assert "code=-" in caplog.text
    assert captured["data"] == {
        "error_type": "rate_limited",
        "status": 429,
        "provider": "unknown",
    }


def test_non_streaming_provider_error_event_uses_fixed_message(events_module, monkeypatch, caplog):
    _, middleware = events_module
    from fastapi.responses import JSONResponse

    secret_prompt = "PRIVATE CUSTOMER PROMPT 376edc"
    signed_url = "https://provider.example/error?access_token=signed-secret-f11a"
    bearer_token = "bearer-secret-a53c"
    emitted = []

    async def capture_event(event):
        emitted.append(event)

    response = JSONResponse(
        status_code=429,
        content={
            "error": {
                "detail": f"{secret_prompt} {signed_url} {bearer_token}",
            }
        },
    )
    ctx = {
        "request": object(),
        "user": object(),
        "metadata": {"chat_id": "local", "message_id": "message"},
        "events": [],
        "event_emitter": capture_event,
    }
    caplog.set_level(logging.WARNING, logger=middleware.__name__)

    result = asyncio.run(middleware.non_streaming_chat_response_handler(response, ctx))

    public_material = caplog.text + repr(emitted)
    assert secret_prompt not in public_material
    assert signed_url not in public_material
    assert bearer_token not in public_material
    assert "access_token" not in public_material
    assert "status=429" in caplog.text
    assert "error_type=upstream_error" in caplog.text
    assert emitted == [
        {
            "type": "chat:message:error",
            "data": {
                "error": {
                    "content": "Open WebUI: Server Connection Error",
                }
            },
        }
    ]
    assert result.status_code == 429
    assert json.loads(result.body) == {"error": {"detail": "Open WebUI: Server Connection Error"}}


def test_streaming_provider_error_metadata_excludes_raw_payload(events_module, caplog):
    _, middleware = events_module
    secret_prompt = "PRIVATE CUSTOMER PROMPT 988a28"
    signed_url = "https://provider.example/failure?access_token=signed-secret-c441"
    bearer_token = "bearer-secret-6f37"

    caplog.set_level(logging.WARNING, logger=middleware.__name__)
    client_error = middleware._safe_streaming_provider_error(
        {
            "status": 429,
            "error": {
                "code": "rate_limit_exceeded",
                "message": f"{secret_prompt} {signed_url} {bearer_token}",
            },
        }
    )

    assert client_error == "Open WebUI: Server Connection Error"
    assert "status=429" in caplog.text
    assert "error_type=rate_limited" in caplog.text
    assert "code=rate_limit_exceeded" in caplog.text
    for secret in (secret_prompt, signed_url, bearer_token, "access_token"):
        assert secret not in caplog.text


def test_middleware_logs_never_serialize_exception_or_code_output() -> None:
    source_path = os.path.join(
        REPOSITORY_ROOT,
        "backend",
        "open_webui",
        "utils",
        "middleware.py",
    )
    with open(source_path, encoding="utf-8") as source_file:
        source = source_file.read()

    assert "log.exception(" not in source
    assert "Code interpreter output: %s" not in source
    assert "Terminal unavailable: {e}" not in source
    assert "Provider returned error (streaming): %s" not in source
    assert "'data': {'error': response_metadata['error']}" not in source
    assert "'error': {'content': error}" not in source


def test_provider_entry_points_do_not_emit_tracebacks_or_raw_exception_details() -> None:
    guarded_fragments = {
        "backend/open_webui/main.py": (
            "exc_info=True",
            "detail=f'Failed to unload model on {len(errors)} node(s): {errors}'",
            "errors.append({'url_idx': idx, 'error': str(e)})",
        ),
        "backend/open_webui/routers/openai.py": ("detail=str(e)",),
    }
    for relative_path, forbidden in guarded_fragments.items():
        source_path = os.path.join(REPOSITORY_ROOT, *relative_path.split("/"))
        with open(source_path, encoding="utf-8") as source_file:
            source = source_file.read()
        for fragment in forbidden:
            assert fragment not in source
