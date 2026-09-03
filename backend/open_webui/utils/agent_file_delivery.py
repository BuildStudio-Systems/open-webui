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
        return parsed.hostname in _LOOPBACK_HOSTS and parsed.port == 8642
    except ValueError:
        return False


def file_owner_headers(user: Any) -> dict[str, str]:
    owner_id = str(getattr(user, 'id', '') or '').strip()
    if not _OWNER_RE.fullmatch(owner_id):
        return {}
    return {FILE_OWNER_HEADER: owner_id}
