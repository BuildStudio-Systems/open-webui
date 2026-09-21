"""Exercise the real identity client with an offline transport and no database."""
import asyncio
import importlib.util
import io
import json
import logging
from pathlib import Path
import re
import socket
import ssl
import urllib.error

import pytest
from fastapi import HTTPException


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('STUDIO_IDENTITY_KEY', 'fixture-private-service-key')
    path = Path(__file__).parents[1] / 'backend/open_webui/studio_identity.py'
    spec = importlib.util.spec_from_file_location('identity_diagnostics_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('reason,kind', [
    (TimeoutError('fixture-private-error'), 'timeout'),
    (socket.gaierror(-2, 'fixture-private-error'), 'dns'),
    (ssl.SSLCertVerificationError('fixture-private-error'), 'tls'),
    (ConnectionResetError('fixture-private-error'), 'connection_reset'),
    (ConnectionRefusedError('fixture-private-error'), 'connection_refused'),
    ('fixture-private-error', 'transport_or_response'),
])
def test_transport_diagnostics_are_safe_and_correlated(client, monkeypatch, caplog, reason, kind):
    captured = []
    def fail(request, timeout):
        captured.append((request, timeout))
        raise urllib.error.URLError(reason)
    monkeypatch.setattr(client._opener, 'open', fail)
    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as caught:
        client._request('login', {'login': 'fixture-private-user', 'password': 'fixture-private-password'}, 'fixture-private-source')
    error = caught.value
    assert error.status_code == 503
    request_id = error.headers['X-Studio-Request-ID']
    assert re.fullmatch('[a-f0-9]{32}', request_id)
    assert request_id in caplog.text and 'kind='+kind in caplog.text
    assert 'elapsed_ms=' in caplog.text and 'fixture-private' not in caplog.text
    assert 'fixture-private' not in error.detail
    assert len(captured) == 1 and captured[0][1] == 3  # no implicit retries or timeout changes
    assert captured[0][0].get_header('X-studio-request-id') == request_id
    assert captured[0][0].full_url.startswith('https://buildstudio-systems.com/')


@pytest.mark.parametrize('status', [401, 403, 409, 422, 429, 500, 502, 503])
def test_business_statuses_stay_intact_and_upstream_bodies_never_log(client, monkeypatch, caplog, status):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError('https://private.invalid/', status, 'fixture-private-message', {}, io.BytesIO(b'fixture-private-body'))
    monkeypatch.setattr(client._opener, 'open', fail)
    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as caught:
        client._request('check', {'token': 'fixture-private-token'})
    expected = status if status < 500 else 503
    assert caught.value.status_code == expected
    assert 'fixture-private' not in caplog.text
    if expected == 503:
        assert 'kind=upstream_http' in caplog.text and f'upstream_status={status}' in caplog.text
    else:
        assert not caplog.records and caught.value.headers is None


def test_invalid_json_and_unknown_action_are_sanitized(client, monkeypatch, caplog):
    monkeypatch.setattr(client._opener, 'open', lambda *a, **k: io.BytesIO(b'fixture-private-malformed'))
    with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as caught:
        client._request('fixture-private-action', {})
    assert caught.value.status_code == 503
    assert 'kind=invalid_response' in caplog.text and 'action=unknown' in caplog.text
    assert 'fixture-private' not in caplog.text


def test_success_is_not_logged_and_response_is_unchanged(client, monkeypatch, caplog):
    result = {'token': 'fixture-private-session', 'person': {'name': 'fixture-private-name'}}
    monkeypatch.setattr(client._opener, 'open', lambda *a, **k: io.BytesIO(json.dumps(result).encode()))
    with caplog.at_level(logging.WARNING):
        assert client._request('login', {}) == result
    assert not caplog.records


def test_expired_cache_does_not_mask_failed_revalidation(client, monkeypatch):
    token = 'bs1_fixture-private-token'
    key = client.hashlib.sha256(token.encode()).hexdigest()
    client._cache[key] = (0, {'stale': True})
    def fail(*args, **kwargs):
        raise TimeoutError('fixture-private-error')
    monkeypatch.setattr(client._opener, 'open', fail)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(client.check(token))
    assert caught.value.status_code == 503 and key not in client._cache
