from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

FILE_OWNER_HEADER = 'X-BuildStudio-User-Id'
_OWNER_RE = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
_LOOPBACK_HOSTS = frozenset({'127.0.0.1', '::1', 'localhost'})


def is_local_hermes_url(url: Any) -> bool:
    try:
        parsed = urlsplit(str(url or ''))
        return (
            parsed.scheme in {'http', 'https'}
            and parsed.hostname in _LOOPBACK_HOSTS
            and parsed.port == 8642
            and parsed.username is None
            and parsed.password is None
            and parsed.path.rstrip('/') == '/v1'
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def file_owner_headers(user: Any) -> dict[str, str]:
    """Return the trusted owner header for an administrator only.

    The shared Hermes cache is not a tenant-scoped storage boundary. Until
    generated files are written below per-user roots, customer-facing media
    delivery must use the dedicated authenticated video API instead.
    """
    if getattr(user, 'role', None) != 'admin':
        return {}
    owner_id = str(getattr(user, 'id', '') or '').strip()
    if not _OWNER_RE.fullmatch(owner_id):
        return {}
    return {FILE_OWNER_HEADER: owner_id}
