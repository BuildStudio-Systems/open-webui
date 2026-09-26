from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

FILE_OWNER_HEADER = 'X-BuildStudio-User-Id'
AGENT_CHAT_HEADER = 'X-BuildStudio-Chat-Id'
_RESERVED_AGENT_HEADERS = frozenset({
    FILE_OWNER_HEADER.lower(), AGENT_CHAT_HEADER.lower(),
    'x-hermes-session-id', 'x-hermes-session-key',
})
_OWNER_RE = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
_LOOPBACK_HOSTS = frozenset({'127.0.0.1', '::1', 'localhost'})

AGENT_ATTACHMENT_GUIDANCE = """[THERE administrator attachment delivery v1]
For administrator-requested generated attachments, use the actual file format.
Workspace paths cannot be downloaded through this strict gateway. Create a unique
new output directory under the active HERMES_HOME/cache/documents for documents,
code and archives, or HERMES_HOME/cache/images for images. Resolve HERMES_HOME from
the runtime environment, not from guessed home paths. Generate final files there,
or copy only the specific newly generated files authorized for this request there.
Never bulk-copy workspaces, credentials, configurations, logs or unrelated customer
data. Reject symlinks and never overwrite existing outputs. Keep intermediate scripts
outside delivery caches. Verify file existence, format and size before publishing.
Return one plain MEDIA:/absolute/path/to/final-file directive per line; the gateway
creates authenticated links or image previews. Do not invent HTTP links or claim
that the browser saved a file. Do not disable security or install software to evade
blocked tools. Downloads are temporary (up to 12 hours, subject to source retention).
If delivery is impossible, report that honestly instead of claiming completion.
This instruction does not grant ordinary users Agent access.
For new XLSX spreadsheets, do not hand-write OOXML or probe/install libraries.
Use the maintained command /opt/buildstudio-there/artifact-runtime/current/bin/there-xlsx
with a JSON object on stdin: {"filename":"report.xlsx","sheets":[{"name":"QA",
"cells":{"A1":"Item","B1":"Value","A2":"Alpha","B2":10,"A3":"Beta","B3":20,
"A4":"Total","B4":{"formula":"=SUM(B2:B3)","cached":30}}}],"zip":true,
"readme":"Requested report only."}. Set zip false when no archive is requested.
Adapt names and cells to the user's request; this is an example, not fixed test data.
Scalars are literal cells; formulas are explicit objects. Cached formula results
are caller-supplied, not calculation proof. The command validates and publishes
only its new workbook and optional ZIP/README, and returns plain MEDIA directives.
If the command fails or the requested feature is unsupported, report the limitation;
do not replace it with invented file links or hand-built XML. Keep the final response
brief and in the user's language; return actual download directives, not a long
self-verification narrative. This command does not process existing user files.
"""


def with_agent_attachment_guidance(payload: dict, url: str, user: Any) -> dict:
    """Apply only to verified administrator requests to the local Agent.

    Use a stable first system message on both streaming and non-streaming requests.
    Caller-owned history is never modified; ordinary models/providers are untouched.
    """
    if not is_local_hermes_url(url) or not file_owner_headers(user):
        return payload
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return payload
    if messages and messages[0] == {'role': 'system', 'content': AGENT_ATTACHMENT_GUIDANCE}:
        return payload
    return {**payload, 'messages': [
        {'role': 'system', 'content': AGENT_ATTACHMENT_GUIDANCE}, *messages
    ]}


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


class AgentChatBindingError(PermissionError):
    """The requested saved Agent chat is not owned by the current administrator."""


async def require_agent_chat_owner(
    chat_id: str,
    owner_id: str,
    is_chat_owner: Callable[[str, str], Awaitable[bool]],
) -> None:
    """Check an exact saved chat against PostgreSQL for requests and downloads."""
    if not isinstance(chat_id, str):
        raise AgentChatBindingError('Agent chat is unavailable')
    try:
        canonical = str(UUID(chat_id))
    except ValueError:
        raise AgentChatBindingError('Agent chat is unavailable') from None
    if canonical != chat_id or not await is_chat_owner(chat_id, owner_id):
        raise AgentChatBindingError('Agent chat is unavailable')


async def bind_agent_request_headers(
    headers: dict[str, str],
    url: str,
    user: Any,
    metadata: dict | None,
    is_chat_owner: Callable[[str, str], Awaitable[bool]],
) -> dict[str, str]:
    """Bind a saved chat from the authoritative Web database, never caller headers.

    This is an execution namespace, not permission to load Hermes history. The
    existing Web message history remains authoritative. Temporary/channel/API
    requests have no saved-chat binding; other providers are left unchanged.
    """
    if not is_local_hermes_url(url):
        return headers

    bound = {key: value for key, value in headers.items()
             if key.lower() not in _RESERVED_AGENT_HEADERS}
    owner_headers = file_owner_headers(user)
    bound.update(owner_headers)
    if not owner_headers or metadata is None:
        return bound
    if not isinstance(metadata, dict):
        raise AgentChatBindingError('Agent chat is unavailable')
    chat_id = metadata.get('chat_id')
    if chat_id is None or chat_id == '':
        return bound
    if not isinstance(chat_id, str):
        raise AgentChatBindingError('Agent chat is unavailable')
    if chat_id.startswith(('temporary:', 'local:', 'channel:')):
        return bound
    owner_id = owner_headers[FILE_OWNER_HEADER]
    await require_agent_chat_owner(chat_id, owner_id, is_chat_owner)
    bound[AGENT_CHAT_HEADER] = chat_id
    return bound
