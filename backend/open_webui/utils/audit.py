import hashlib
import hmac
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    MutableMapping,
    Optional,
    cast,
)

from asgiref.typing import (
    ASGI3Application,
    ASGIReceiveCallable,
    ASGIReceiveEvent,
    ASGISendCallable,
    ASGISendEvent,
)
from asgiref.typing import (
    Scope as ASGIScope,
)
from loguru import logger
from open_webui.env import (
    AUDIT_INCLUDED_PATHS,
    AUDIT_LOG_LEVEL,
    ENABLE_AUDIT_GET_REQUESTS,
    ENABLE_AUDIT_LOGS_FILE,
    MAX_BODY_LOG_SIZE,
)
from open_webui.models.users import UserModel
from open_webui.utils.auth import get_current_user, get_http_authorization_cred
from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from loguru import Logger


@dataclass(frozen=True)
class AuditLogEntry:
    # `Metadata` audit level properties
    id: str
    user: Optional[dict[str, Any]]
    audit_level: str
    verb: str
    request_uri: str
    user_agent: Optional[str] = None
    source_ip: Optional[str] = None
    # `Request` audit level properties
    request_object: Any = None
    # `Request Response` level
    response_object: Any = None
    response_status_code: Optional[int] = None


class AuditLevel(str, Enum):
    NONE = 'NONE'
    METADATA = 'METADATA'
    REQUEST = 'REQUEST'
    REQUEST_RESPONSE = 'REQUEST_RESPONSE'


def wechat_admin_audit_is_ready() -> bool:
    """Fail-closed readiness check for sensitive WeChat administrator reads."""
    try:
        audit_level = AuditLevel(AUDIT_LOG_LEVEL)
    except ValueError:
        return False

    if audit_level == AuditLevel.NONE or not ENABLE_AUDIT_LOGS_FILE:
        return False

    # Import lazily to keep the existing audit/logger dependency direction and
    # avoid coupling normal middleware construction to logger initialization.
    from open_webui.utils.logger import audit_file_sink_is_ready

    return audit_file_sink_is_ready()


class AuditLogger:
    """
    A helper class that encapsulates audit logging functionality. It uses Loguru’s logger with an auditable binding to ensure that audit log entries are filtered correctly.

    Parameters:
    logger (Logger): An instance of Loguru’s logger.
    """

    def __init__(self, logger: 'Logger'):
        self.logger = logger.bind(auditable=True)

    def write(
        self,
        audit_entry: AuditLogEntry,
        *,
        log_level: str = 'INFO',
        extra: Optional[dict] = None,
    ):
        entry = asdict(audit_entry)

        if extra:
            entry['extra'] = extra

        self.logger.log(
            log_level,
            '',
            **entry,
        )


class AuditContext:
    """
    Captures and aggregates the HTTP request and response bodies during the processing of a request. It ensures that only a configurable maximum amount of data is stored to prevent excessive memory usage.

    Attributes:
    request_body (bytearray): Accumulated request payload.
    response_body (bytearray): Accumulated response payload.
    max_body_size (int): Maximum number of bytes to capture.
    metadata (Dict[str, Any]): A dictionary to store additional audit metadata (user, http verb, user agent, etc.).
    """

    def __init__(self, max_body_size: int = MAX_BODY_LOG_SIZE):
        self.request_body = bytearray()
        self.response_body = bytearray()
        self.max_body_size = max_body_size
        self.metadata: Dict[str, Any] = {}

    def add_request_chunk(self, chunk: bytes):
        if len(self.request_body) < self.max_body_size:
            self.request_body.extend(chunk[: self.max_body_size - len(self.request_body)])

    def add_response_chunk(self, chunk: bytes):
        if len(self.response_body) < self.max_body_size:
            self.response_body.extend(chunk[: self.max_body_size - len(self.response_body)])


class AuditLoggingMiddleware:
    """
    ASGI middleware that intercepts HTTP requests and responses to perform audit logging. It captures request/response bodies (depending on audit level), headers, HTTP methods, and user information, then logs a structured audit entry at the end of the request cycle.
    """

    DEFAULT_AUDITED_METHODS = {'PUT', 'PATCH', 'DELETE', 'POST'}
    # Chat contents, source URLs, and administrator filters are deliberately
    # excluded from the generic request/response audit stream.  The WeChat
    # administrator router emits its own metadata-only access events; this
    # middleware still records request metadata, but must never duplicate the
    # sensitive payload into application logs even when global body auditing is
    # enabled.
    METADATA_ONLY_PATH_PREFIXES = ('/api/v1/there/admin/wechat',)
    NO_STORE_REQUEST_HEADER = 'x-buildstudio-no-store'
    NO_STORE_CHAT_PATH = '/api/v1/chat/completions'
    NO_STORE_TOKEN_HASH_ENV = 'BUILDSTUDIO_MINIPROGRAM_OPENWEBUI_TOKEN_SHA256'

    def __init__(
        self,
        app: ASGI3Application,
        *,
        excluded_paths: Optional[list[str]] = None,
        included_paths: Optional[list[str]] = None,
        max_body_size: int = MAX_BODY_LOG_SIZE,
        audit_level: AuditLevel = AuditLevel.NONE,
        audit_get_requests: bool = False,
    ) -> None:
        self.app = app
        self.audit_logger = AuditLogger(logger)

        def normalize_paths(paths: Optional[list[str]]) -> list[str]:
            return [path for path in (path.strip().lstrip('/') for path in paths or []) if path]

        self.excluded_paths = normalize_paths(excluded_paths)
        self.included_paths = normalize_paths(included_paths)
        self.max_body_size = max_body_size
        self.audited_methods = set(self.DEFAULT_AUDITED_METHODS)
        if audit_get_requests:
            self.audited_methods.add('GET')
        self.audit_level = audit_level
        configured_token_hash = os.getenv(self.NO_STORE_TOKEN_HASH_ENV, '').strip().lower()
        self._no_store_token_hash = (
            configured_token_hash
            if re.fullmatch(r'[0-9a-f]{64}', configured_token_hash)
            else None
        )

        # Paths are fixed for the process lifetime; compile once instead of
        # per request. None means the corresponding mode has nothing to match.
        self._included_pattern = (
            re.compile(r'^/api(?:/v1)?/(' + '|'.join(self.included_paths) + r')\b') if self.included_paths else None
        )
        self._excluded_pattern = (
            re.compile(r'^/api(?:/v1)?/(' + '|'.join(self.excluded_paths) + r')\b') if self.excluded_paths else None
        )

        if self.included_paths and self.excluded_paths:
            logger.warning(
                'Both AUDIT_INCLUDED_PATHS and AUDIT_EXCLUDED_PATHS are set. '
                'AUDIT_INCLUDED_PATHS (whitelist) takes precedence.'
            )

    async def __call__(
        self,
        scope: ASGIScope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
    ) -> None:
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        request = Request(scope=cast(MutableMapping, scope))

        if self._is_no_store_chat_request(request) and not self._is_trusted_no_store_request(request):
            response = JSONResponse(
                {'detail': 'Trusted no-store request configuration is unavailable'},
                status_code=503,
                headers={
                    'Cache-Control': 'private, no-store',
                    'Pragma': 'no-cache',
                },
            )
            await response(scope, receive, send)
            return

        if self._should_skip_auditing(request):
            return await self.app(scope, receive, send)

        force_metadata_only = self._requires_metadata_only_audit(request)
        effective_audit_level = AuditLevel.METADATA if force_metadata_only else self.audit_level

        async with self._audit_context(
            request,
            audit_level=effective_audit_level,
            redact_query=force_metadata_only,
        ) as context:

            async def send_wrapper(message: ASGISendEvent) -> None:
                if force_metadata_only or (effective_audit_level == AuditLevel.REQUEST_RESPONSE):
                    await self._capture_response(
                        message,
                        context,
                        capture_body=(effective_audit_level == AuditLevel.REQUEST_RESPONSE),
                    )

                await send(message)

            original_receive = receive

            async def receive_wrapper() -> ASGIReceiveEvent:
                nonlocal original_receive
                message = await original_receive()

                if effective_audit_level in (
                    AuditLevel.REQUEST,
                    AuditLevel.REQUEST_RESPONSE,
                ):
                    await self._capture_request(message, context)

                return message

            await self.app(scope, receive_wrapper, send_wrapper)

    @asynccontextmanager
    async def _audit_context(
        self,
        request: Request,
        *,
        audit_level: AuditLevel,
        redact_query: bool,
    ) -> AsyncGenerator[AuditContext, None]:
        """
        async context manager that ensures that an audit log entry is recorded after the request is processed.
        """
        context = AuditContext()
        try:
            yield context
        finally:
            await self._log_audit_entry(
                request,
                context,
                audit_level=audit_level,
                redact_query=redact_query,
            )

    async def _get_authenticated_user(self, request: Request) -> Optional[UserModel]:
        # get_current_user stashes the resolved user on the scope-backed state;
        # reuse it instead of running the full auth pipeline (JWT decode, Redis
        # revocation checks, DB fetch, last-active write) a second time.
        user = getattr(request.state, 'user', None)
        if isinstance(user, UserModel):
            return user

        auth_header = request.headers.get('Authorization')

        try:
            user = await get_current_user(request, None, None, get_http_authorization_cred(auth_header))
            return user
        except Exception as e:
            logger.debug('Failed to get authenticated user: {}', e)

        return None

    ALWAYS_LOG_ENDPOINTS = (
        '/api/v1/auths/signin',
        '/api/v1/auths/signout',
        '/api/v1/auths/signup',
    )

    @classmethod
    def _is_no_store_chat_request(cls, request: Request) -> bool:
        path = request.url.path.casefold().rstrip('/')
        return (
            path == cls.NO_STORE_CHAT_PATH
            and request.headers.get(cls.NO_STORE_REQUEST_HEADER, '').strip().casefold()
            == 'true'
        )

    def _is_trusted_no_store_request(self, request: Request) -> bool:
        if not self._no_store_token_hash:
            return False
        authorization = getattr(request.state, 'token', None)
        if authorization is None:
            authorization = get_http_authorization_cred(
                request.headers.get('Authorization')
            )
        if (
            authorization is None
            or str(getattr(authorization, 'scheme', '')).casefold() != 'bearer'
        ):
            return False
        token = getattr(authorization, 'credentials', None)
        if not isinstance(token, str) or not token:
            return False
        actual = hashlib.sha256(token.encode('utf-8')).hexdigest()
        return hmac.compare_digest(actual, self._no_store_token_hash)

    def _requires_metadata_only_audit(self, request: Request) -> bool:
        if self._is_no_store_chat_request(request):
            return self._is_trusted_no_store_request(request)
        path = request.url.path.casefold().rstrip('/')
        return any(
            path == prefix or path.startswith(f'{prefix}/')
            for prefix in self.METADATA_ONLY_PATH_PREFIXES
        )

    def _should_skip_auditing(self, request: Request) -> bool:
        if AUDIT_LOG_LEVEL == 'NONE':
            return True

        # Sensitive administrator reads and internal no-store AI requests must
        # remain metadata-audited even when ordinary method auditing or global
        # include/exclude filters would otherwise skip them. Authentication is
        # still required before we create an audit record.
        if self._requires_metadata_only_audit(request):
            return not (
                request.headers.get('authorization')
                or request.cookies.get('token')
                or getattr(request.state, 'token', None)
            )

        if request.method not in self.audited_methods:
            return True

        path = request.url.path.lower()
        for endpoint in self.ALWAYS_LOG_ENDPOINTS:
            if path.startswith(endpoint):
                return False  # Do NOT skip logging for auth endpoints

        # Skip logging if the request is not authenticated
        # Check both Authorization header (API keys) and token cookie (browser sessions)
        if not request.headers.get('authorization') and not request.cookies.get('token'):
            return True

        # Whitelist mode: only log paths that match included_paths
        if self._included_pattern:
            return not self._included_pattern.match(request.url.path)

        # Blacklist mode: skip paths that match excluded_paths
        if self._excluded_pattern and self._excluded_pattern.match(request.url.path):
            return True

        return False

    async def _capture_request(self, message: ASGIReceiveEvent, context: AuditContext):
        if message['type'] == 'http.request':
            body = message.get('body', b'')
            context.add_request_chunk(body)

    async def _capture_response(
        self,
        message: ASGISendEvent,
        context: AuditContext,
        *,
        capture_body: bool,
    ):
        if message['type'] == 'http.response.start':
            context.metadata['response_status_code'] = message['status']

        elif capture_body and message['type'] == 'http.response.body':
            body = message.get('body', b'')
            context.add_response_chunk(body)

    async def _log_audit_entry(
        self,
        request: Request,
        context: AuditContext,
        *,
        audit_level: AuditLevel,
        redact_query: bool,
    ):
        try:
            user = await self._get_authenticated_user(request)

            user = user.model_dump(include={'id', 'name', 'email', 'role'}) if user else {}

            request_body = None
            response_body = None
            if audit_level in (AuditLevel.REQUEST, AuditLevel.REQUEST_RESPONSE):
                request_body = context.request_body.decode('utf-8', errors='replace')
            if audit_level == AuditLevel.REQUEST_RESPONSE:
                response_body = context.response_body.decode('utf-8', errors='replace')

            # Redact sensitive information
            if request_body and 'password' in request_body:
                request_body = re.sub(
                    r'"password":\s*"(.*?)"',
                    '"password": "********"',
                    request_body,
                )

            entry = AuditLogEntry(
                id=str(uuid.uuid4()),
                user=user,
                audit_level=audit_level.value,
                verb=request.method,
                request_uri=request.url.path if redact_query else str(request.url),
                response_status_code=context.metadata.get('response_status_code', None),
                source_ip=request.client.host if request.client else None,
                user_agent=request.headers.get('user-agent'),
                request_object=request_body,
                response_object=response_body,
            )

            self.audit_logger.write(entry)
        except Exception as e:
            logger.error(f'Failed to log audit entry: {str(e)}')
