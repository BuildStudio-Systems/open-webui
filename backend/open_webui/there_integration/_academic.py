# BuildStudio first-party provider implementation, copied into the THERE runtime.
# Provenance: deploy/native/hermes-skills/academic-search/scripts/search_academic.py
# The provider adapters, normalization and deadline worker isolation are unchanged.
#!/usr/bin/env python3
"""Search public scholarly APIs and merge their paper metadata."""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import hashlib
import html
from http.client import HTTPException
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ElementTree
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = "BuildStudio-There-Academic-Search/1.0"
MAX_RESPONSE_BYTES = 5_000_000
RESPONSE_READ_CHUNK_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 20
WORKER_TERMINATE_GRACE_SECONDS = 0.25
WORKER_POLL_INTERVAL_SECONDS = 0.01
MAX_WORKER_REQUEST_BYTES = 128 * 1024
MAX_WORKER_OUTPUT_BYTES = 32 * 1024 * 1024
WORKER_PROCESS_ENVIRONMENT_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
PROVIDER_ENVIRONMENT_KEYS = {
    "crossref": ("BUILDSTUDIO_ACADEMIC_CONTACT",),
    "openalex": ("OPENALEX_API_KEY",),
    "semantic-scholar": ("SEMANTIC_SCHOLAR_API_KEY",),
}

DEFAULT_SOURCES = (
    "arxiv",
    "openreview",
    "crossref",
    "openalex",
    "europe-pmc",
    "datacite",
    "doaj",
    "openaire",
)
OPTIONAL_SOURCES = ("semantic-scholar", "dblp")
ALL_SOURCES = DEFAULT_SOURCES + OPTIONAL_SOURCES

_SYSTEM_GETADDRINFO = socket.getaddrinfo

SOURCE_DETAILS: dict[str, dict[str, Any]] = {
    "arxiv": {
        "default": True,
        "status": "stable",
        "description": "arXiv preprints through the official Atom API",
    },
    "openreview": {
        "default": True,
        "status": "stable",
        "description": "OpenReview public submission records through API v2",
    },
    "crossref": {
        "default": True,
        "status": "stable",
        "description": "Crossref DOI and publication metadata",
    },
    "openalex": {
        "default": True,
        "status": "stable",
        "description": "OpenAlex scholarly graph; optional API key is supported",
    },
    "europe-pmc": {
        "default": True,
        "status": "stable",
        "description": "Europe PMC life-sciences records, including PubMed IDs",
    },
    "datacite": {
        "default": True,
        "status": "stable",
        "description": "DataCite public DOI metadata",
    },
    "doaj": {
        "default": True,
        "status": "stable",
        "description": "Directory of Open Access Journals article metadata",
    },
    "openaire": {
        "default": True,
        "status": "stable",
        "description": "OpenAIRE Graph publication metadata",
    },
    "semantic-scholar": {
        "default": False,
        "status": "optional/degraded",
        "description": "Semantic Scholar Academic Graph; anonymous pool may throttle",
    },
    "dblp": {
        "default": False,
        "status": "optional/degraded",
        "description": "DBLP computer-science bibliography search",
    },
}

SOURCE_QUALITY = {
    "crossref": 100,
    "europe-pmc": 98,
    "doaj": 96,
    "datacite": 94,
    "dblp": 92,
    "openreview": 88,
    "arxiv": 86,
    "openalex": 82,
    "openaire": 78,
    "semantic-scholar": 74,
}

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")

STRONG_IDENTIFIER_NAMESPACES = frozenset(
    {
        "doi",
        "arxiv",
        "pmid",
        "pmcid",
        "openreview",
        "semantic-scholar",
        "dblp",
        "openalex",
        "openaire",
        "datacite",
        "doaj",
    }
)
RATE_GATE_INTERVALS = {"arxiv": 3.0, "crossref": 1.0}
_RATE_GATE_LOCKS = {name: threading.Lock() for name in RATE_GATE_INTERVALS}
_RATE_GATE_LAST_START = {name: 0.0 for name in RATE_GATE_INTERVALS}


class ProviderError(RuntimeError):
    """A provider failed in a way that is safe to expose to a caller."""

    def __init__(self, status: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.safe_message = message
        self.retryable = retryable


@dataclass(frozen=True)
class SearchContext:
    query: str
    per_source: int
    abstract_chars: int
    timeout: int
    opener: Callable[..., Any]
    environment: Mapping[str, str]
    deadline: float | None = None


def _clean_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    )
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _strip_markup(value: Any) -> str:
    return _clean_text(TAG_RE.sub(" ", str(value or "")))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _string_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            item = (
                item.get("name")
                or item.get("fullName")
                or item.get("display_name")
                or item.get("text")
                or item.get("value")
            )
        cleaned = _clean_text(item, 500)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _first_text(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("value")
            or value.get("title")
            or value.get("name")
            or value.get("display_name")
            or value.get("label")
        )
    if isinstance(value, list):
        for item in value:
            candidate = _first_text(item)
            if candidate:
                return candidate
        return ""
    return _clean_text(value)


def _safe_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _normalize_date(value: Any) -> str | None:
    text = _clean_text(value)
    match = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", text)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return f"{year:04d}"


def _date_from_parts(value: Any) -> str | None:
    parts = value
    if isinstance(value, dict):
        parts = value.get("date-parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], list):
        parts = parts[0]
    if not isinstance(parts, list) or not parts:
        return None
    numbers = [_safe_int(item) for item in parts[:3]]
    if numbers[0] is None:
        return None
    return _normalize_date(
        "-".join(str(number) for number in numbers if number is not None)
    )


def _date_from_millis(value: Any) -> str | None:
    milliseconds = _safe_int(value)
    if milliseconds is None or milliseconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _year_from_date(value: str | None) -> int | None:
    if not value or not re.match(r"^\d{4}", value):
        return None
    return _safe_int(value[:4])


def _clean_url(value: Any, *, base: str | None = None) -> str | None:
    text = _clean_text(value, 4_000)
    if not text:
        return None
    if base and text.startswith("/"):
        text = urljoin(base, text)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return text


def _iter_strings(value: Any, depth: int = 0) -> Iterable[str]:
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in list(value.values())[:100]:
            yield from _iter_strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value[:100]:
            yield from _iter_strings(item, depth + 1)


def _normalize_doi(value: Any) -> str | None:
    for text in _iter_strings(value):
        decoded = unquote(html.unescape(text)).strip()
        match = DOI_RE.search(decoded)
        if not match:
            continue
        doi = match.group(0).rstrip(".,;:").lower()
        while doi.endswith(")") and doi.count(")") > doi.count("("):
            doi = doi[:-1]
        return doi or None
    return None


def _normalize_arxiv_id(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    text = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", text, flags=re.I)
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.I)
    text = text.removesuffix(".pdf")
    text = re.sub(r"v\d+$", "", text, flags=re.I)
    return text.strip().lower() or None


def _identifier_value(namespace: str, value: Any) -> str | None:
    text = _clean_text(value, 1_000)
    if not text:
        return None
    namespace = namespace.lower().replace("_", "-")
    if namespace == "doi":
        return _normalize_doi(text)
    if namespace == "arxiv":
        return _normalize_arxiv_id(text)
    if namespace == "pmid":
        match = re.search(r"\d+", text)
        return match.group(0) if match else None
    if namespace == "pmcid":
        match = re.search(r"PMC\d+", text, re.I)
        return match.group(0).upper() if match else None
    if namespace == "openalex":
        return text.rstrip("/").rsplit("/", 1)[-1].upper()
    if namespace == "semantic-scholar":
        return text.lower()
    return text


def _truncate_abstract(value: Any, maximum: int) -> str:
    text = _strip_markup(value)
    if maximum == 0:
        return ""
    if len(text) > maximum:
        return text[:maximum].rstrip() + "..."
    return text


def _response_timeout_error() -> ProviderError:
    return ProviderError("timeout", "Provider request timed out.", retryable=True)


def _remaining_response_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _response_timeout_error()
    return remaining


def _tighten_response_socket_timeout(response: Any, timeout: float) -> None:
    """Best-effort socket discovery for urllib responses; injected fakes may omit it."""
    pending: list[tuple[Any, int]] = [(response, 0)]
    seen: set[int] = set()
    while pending:
        candidate, depth = pending.pop(0)
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            setter(timeout)
            return
        if depth >= 4:
            continue
        for attribute in ("fp", "raw", "_sock", "sock"):
            try:
                child = getattr(candidate, attribute)
            except (AttributeError, OSError, ValueError):
                continue
            pending.append((child, depth + 1))


def _read_limited(response: Any, *, deadline: float) -> bytes:
    body = bytearray()
    reader = getattr(response, "read1", None)
    if not callable(reader):
        reader = response.read
    while True:
        remaining = _remaining_response_time(deadline)
        amount = min(
            RESPONSE_READ_CHUNK_BYTES,
            MAX_RESPONSE_BYTES + 1 - len(body),
        )
        try:
            _tighten_response_socket_timeout(response, remaining)
            chunk = reader(amount)
        except TimeoutError as error:
            raise _response_timeout_error() from error
        except (OSError, HTTPException) as error:
            if time.monotonic() >= deadline:
                raise _response_timeout_error() from error
            raise ProviderError(
                "network_error",
                "Provider response body could not be read.",
                retryable=True,
            ) from error
        _remaining_response_time(deadline)
        if not isinstance(chunk, (bytes, bytearray)):
            raise ProviderError("invalid_response", "Provider returned a non-byte response.")
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProviderError(
                "response_too_large", "Provider response exceeded the size limit."
            )


def _rate_gate_timeout() -> ProviderError:
    return ProviderError(
        "timeout", "Timed out waiting for the local provider rate gate.", retryable=True
    )


def _bounded_gate_wait(seconds: float, deadline: float) -> None:
    if seconds <= 0:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0 or seconds > remaining:
        raise _rate_gate_timeout()
    time.sleep(seconds)


def _open_linux_rate_gate_file(source: str) -> int:
    """Open a private, non-symlink lock file owned by the current uid."""
    uid = os.getuid()
    directory = os.path.join(
        tempfile.gettempdir(), f"buildstudio-academic-search-{uid}"
    )
    directory_created = False
    try:
        os.mkdir(directory, 0o700)
        directory_created = True
    except FileExistsError:
        pass
    except OSError as error:
        raise ProviderError(
            "rate_gate_error", "Could not initialize the local provider rate gate."
        ) from error
    if directory_created:
        try:
            os.chmod(directory, 0o700)
        except OSError as error:
            raise ProviderError(
                "rate_gate_error", "Could not secure the local provider rate gate."
            ) from error

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_fd = os.open(directory, directory_flags)
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != uid
        ):
            raise ProviderError(
                "rate_gate_error", "Local provider rate gate directory is unsafe."
            )
        if stat.S_IMODE(directory_stat.st_mode) != 0o700:
            raise ProviderError(
                "rate_gate_error", "Local provider rate gate directory is unsafe."
            )

        no_follow = getattr(os, "O_NOFOLLOW", 0)
        file_created = False
        try:
            file_fd = os.open(
                f"{source}.lock",
                os.O_CREAT | os.O_EXCL | os.O_RDWR | no_follow,
                0o600,
                dir_fd=directory_fd,
            )
            file_created = True
        except FileExistsError:
            file_fd = os.open(
                f"{source}.lock",
                os.O_RDWR | no_follow,
                dir_fd=directory_fd,
            )
        if file_created:
            os.fchmod(file_fd, 0o600)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != uid:
            raise ProviderError(
                "rate_gate_error", "Local provider rate gate file is unsafe."
            )
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise ProviderError(
                "rate_gate_error", "Local provider rate gate file is unsafe."
            )
        result_fd = file_fd
        file_fd = None
        return result_fd
    except ProviderError:
        raise
    except OSError as error:
        raise ProviderError(
            "rate_gate_error", "Could not open the local provider rate gate."
        ) from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


@contextmanager
def _linux_rate_gate(source: str, interval: float, deadline: float) -> Iterable[None]:
    import fcntl

    file_fd = _open_linux_rate_gate_file(source)
    locked = False
    try:
        while not locked:
            try:
                fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise ProviderError(
                        "rate_gate_error", "Local provider rate gate lock failed."
                    ) from error
                _bounded_gate_wait(0.05, deadline)

        os.lseek(file_fd, 0, os.SEEK_SET)
        raw_last_start = os.read(file_fd, 128)
        try:
            last_start = float(raw_last_start.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            last_start = 0.0
        _bounded_gate_wait(interval - (time.time() - last_start), deadline)

        current_start = f"{time.time():.9f}".encode("ascii")
        os.lseek(file_fd, 0, os.SEEK_SET)
        os.ftruncate(file_fd, 0)
        os.write(file_fd, current_start)
        os.fsync(file_fd)
        yield
    finally:
        if locked:
            fcntl.flock(file_fd, fcntl.LOCK_UN)
        os.close(file_fd)


@contextmanager
def _in_process_rate_gate(
    source: str, interval: float, deadline: float
) -> Iterable[None]:
    lock = _RATE_GATE_LOCKS[source]
    remaining = max(0.0, deadline - time.monotonic())
    if not lock.acquire(timeout=remaining):
        raise _rate_gate_timeout()
    try:
        _bounded_gate_wait(
            interval - (time.monotonic() - _RATE_GATE_LAST_START[source]),
            deadline,
        )
        _RATE_GATE_LAST_START[source] = time.monotonic()
        yield
    finally:
        lock.release()


@contextmanager
def _provider_rate_gate(source: str, context: SearchContext) -> Iterable[None]:
    """Rate-limit live arXiv/Crossref calls; injected test openers bypass waits."""
    interval = RATE_GATE_INTERVALS.get(source)
    if interval is None or context.opener is not urlopen:
        yield
        return
    deadline = (
        context.deadline
        if context.deadline is not None
        else time.monotonic() + context.timeout
    )
    gate = (
        _linux_rate_gate(source, interval, deadline)
        if sys.platform.startswith("linux")
        else _in_process_rate_gate(source, interval, deadline)
    )
    with gate:
        yield


def _fetch_bytes(
    url: str,
    *,
    timeout: int,
    opener: Callable[..., Any],
    headers: Mapping[str, str] | None = None,
    sensitive_headers: Mapping[str, str] | None = None,
    deadline: float | None = None,
) -> bytes:
    hard_deadline = deadline if deadline is not None else time.monotonic() + timeout
    request_headers = {
        "Accept": "application/json, application/atom+xml, application/xml;q=0.9",
        "User-Agent": USER_AGENT,
    }
    request_headers.update(headers or {})
    request = Request(url, headers=request_headers)
    for name, value in (sensitive_headers or {}).items():
        request.add_unredirected_header(name, value)
    try:
        remaining = _remaining_response_time(hard_deadline)
        with opener(request, timeout=min(float(timeout), remaining)) as response:
            return _read_limited(response, deadline=hard_deadline)
    except ProviderError:
        raise
    except HTTPError as error:
        if time.monotonic() >= hard_deadline:
            raise _response_timeout_error() from error
        if error.code == 429:
            raise ProviderError(
                "rate_limited", "Provider rate-limited the request.", retryable=True
            ) from error
        if error.code in {401, 403}:
            raise ProviderError(
                "access_denied", f"Provider denied the request (HTTP {error.code})."
            ) from error
        raise ProviderError(
            "http_error",
            f"Provider returned HTTP {error.code}.",
            retryable=500 <= error.code < 600,
        ) from error
    except TimeoutError as error:
        raise ProviderError("timeout", "Provider request timed out.", retryable=True) from error
    except URLError as error:
        if time.monotonic() >= hard_deadline:
            raise _response_timeout_error() from error
        status = "timeout" if isinstance(getattr(error, "reason", None), TimeoutError) else "network_error"
        message = "Provider request timed out." if status == "timeout" else "Provider network request failed."
        raise ProviderError(status, message, retryable=True) from error
    except (OSError, HTTPException) as error:
        if time.monotonic() >= hard_deadline:
            raise _response_timeout_error() from error
        raise ProviderError(
            "network_error", "Provider network request failed.", retryable=True
        ) from error


def _fetch_json(
    url: str,
    *,
    timeout: int,
    opener: Callable[..., Any],
    headers: Mapping[str, str] | None = None,
    sensitive_headers: Mapping[str, str] | None = None,
    deadline: float | None = None,
) -> Any:
    body = _fetch_bytes(
        url,
        timeout=timeout,
        opener=opener,
        headers=headers,
        sensitive_headers=sensitive_headers,
        deadline=deadline,
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError("invalid_response", "Provider returned invalid JSON.") from error


def _fetch_xml(
    url: str,
    *,
    timeout: int,
    opener: Callable[..., Any],
    deadline: float | None = None,
) -> ElementTree.Element:
    body = _fetch_bytes(url, timeout=timeout, opener=opener, deadline=deadline)
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ProviderError("invalid_response", "Provider returned invalid XML.") from error


def _record(
    source: str,
    source_id: Any,
    rank: int,
    title: Any,
    *,
    authors: Any = None,
    publication_date: Any = None,
    year: Any = None,
    abstract: Any = None,
    venue: Any = None,
    venue_id: Any = None,
    work_type: Any = None,
    publication_stage: str = "unknown",
    venue_status: Any = None,
    doi: Any = None,
    identifiers: Mapping[str, Any] | None = None,
    url: Any = None,
    pdf_url: Any = None,
    is_open_access: bool | None = None,
    citation_count: Any = None,
    license_name: Any = None,
    abstract_chars: int = 1_200,
) -> dict[str, Any] | None:
    cleaned_title = _clean_text(title, 2_000)
    cleaned_source_id = _clean_text(source_id, 1_000)
    if not cleaned_title or not cleaned_source_id:
        return None

    normalized_doi = _normalize_doi(doi)
    normalized_identifiers: dict[str, list[str]] = {}
    for namespace, values in (identifiers or {}).items():
        canonical_namespace = namespace.lower().replace("_", "-")
        for value in _as_list(values):
            normalized = _identifier_value(canonical_namespace, value)
            if normalized:
                normalized_identifiers.setdefault(canonical_namespace, [])
                if normalized not in normalized_identifiers[canonical_namespace]:
                    normalized_identifiers[canonical_namespace].append(normalized)
    if normalized_doi:
        normalized_identifiers.setdefault("doi", [])
        if normalized_doi not in normalized_identifiers["doi"]:
            normalized_identifiers["doi"].append(normalized_doi)

    cleaned_date = _normalize_date(publication_date)
    cleaned_year = _safe_int(year) or _year_from_date(cleaned_date)
    cleaned_citations = _safe_int(citation_count)
    if cleaned_citations is not None and cleaned_citations < 0:
        cleaned_citations = None

    return {
        "source": source,
        "source_id": cleaned_source_id,
        "rank": rank,
        "title": cleaned_title,
        "authors": _string_list(authors),
        "year": cleaned_year,
        "publication_date": cleaned_date,
        "abstract": _truncate_abstract(abstract, abstract_chars),
        "venue": _clean_text(venue, 1_000),
        "venue_id": _clean_text(venue_id, 1_000),
        "work_type": _clean_text(work_type, 200).lower(),
        "publication_stage": publication_stage,
        "venue_status": _clean_text(venue_status, 1_000),
        "doi": normalized_doi,
        "identifiers": normalized_identifiers,
        "url": _clean_url(url),
        "pdf_url": _clean_url(pdf_url),
        "is_open_access": is_open_access,
        "citation_count": cleaned_citations,
        "license": _clean_text(license_name, 1_000),
    }


def _search_arxiv(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    phrase = context.query.replace('"', " ")
    parameters = {
        "search_query": f'all:"{phrase}"',
        "start": 0,
        "max_results": context.per_source,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    with _provider_rate_gate("arxiv", context):
        root = _fetch_xml(
            f"https://export.arxiv.org/api/query?{urlencode(parameters)}",
            timeout=context.timeout,
            opener=context.opener,
            deadline=context.deadline,
        )
    namespaces = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    entries = root.findall("atom:entry", namespaces)
    records: list[dict[str, Any]] = []
    for rank, entry in enumerate(entries, start=1):
        identifier_url = entry.findtext("atom:id", default="", namespaces=namespaces)
        exact_source_id = re.sub(
            r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/",
            "",
            _clean_text(identifier_url, 1_000),
            flags=re.I,
        ).removesuffix(".pdf").strip()
        arxiv_id = _normalize_arxiv_id(exact_source_id)
        if not exact_source_id or not arxiv_id:
            continue
        links = entry.findall("atom:link", namespaces)
        pdf_url = next(
            (
                link.get("href")
                for link in links
                if link.get("title") == "pdf" or link.get("type") == "application/pdf"
            ),
            f"https://arxiv.org/pdf/{exact_source_id}",
        )
        alternate = next(
            (link.get("href") for link in links if link.get("rel") == "alternate"),
            f"https://arxiv.org/abs/{exact_source_id}",
        )
        authors = [
            author.findtext("atom:name", default="", namespaces=namespaces)
            for author in entry.findall("atom:author", namespaces)
        ]
        categories = [item.get("term", "") for item in entry.findall("atom:category", namespaces)]
        item = _record(
            "arxiv",
            exact_source_id,
            rank,
            entry.findtext("atom:title", default="", namespaces=namespaces),
            authors=authors,
            publication_date=entry.findtext("atom:published", default="", namespaces=namespaces),
            abstract=entry.findtext("atom:summary", default="", namespaces=namespaces),
            venue=entry.findtext("arxiv:journal_ref", default="", namespaces=namespaces),
            work_type="preprint",
            publication_stage="preprint",
            doi=entry.findtext("arxiv:doi", default="", namespaces=namespaces),
            identifiers={"arxiv": arxiv_id},
            url=alternate,
            pdf_url=pdf_url,
            is_open_access=True,
            license_name=entry.findtext("arxiv:license", default="", namespaces=namespaces),
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            if categories:
                item["subjects"] = _string_list(categories)
            records.append(item)
    return records, len(entries)


def _openreview_value(content: Mapping[str, Any], field: str, default: Any = None) -> Any:
    value = content.get(field, default)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _search_openreview(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    fetch_limit = min(max(context.per_source * 5, 25), 100)
    parameters = {
        "term": context.query,
        "type": "terms",
        "content": "all",
        "source": "forum",
        "limit": fetch_limit,
        "offset": 0,
    }
    payload = _fetch_json(
        f"https://api2.openreview.net/notes/search?{urlencode(parameters)}",
        timeout=context.timeout,
        opener=context.opener,
        deadline=context.deadline,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("notes"), list):
        raise ProviderError("invalid_response", "OpenReview response omitted notes.")
    notes = payload["notes"]
    records: list[dict[str, Any]] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        content = note.get("content") if isinstance(note.get("content"), dict) else {}
        venue_id = _clean_text(_openreview_value(content, "venueid", ""))
        venue_id_key = venue_id.lower()
        invitations = [item.lower() for item in _string_list(note.get("invitations"))]
        if venue_id_key.startswith("dblp.org/") or any(
            "dblp.org/" in item for item in invitations
        ):
            continue
        note_id = _clean_text(note.get("id"))
        forum_id = _clean_text(note.get("forum")) or note_id
        external_ids = note.get("externalIds")
        doi = _normalize_doi([_openreview_value(content, "doi", ""), external_ids])
        item = _record(
            "openreview",
            note_id,
            len(records) + 1,
            _openreview_value(content, "title", ""),
            authors=_openreview_value(content, "authors", []),
            publication_date=_date_from_millis(
                note.get("pdate") or note.get("cdate") or note.get("tcdate")
            ),
            abstract=_openreview_value(content, "abstract", ""),
            venue=_openreview_value(content, "venue", ""),
            venue_id=venue_id,
            work_type="submission",
            publication_stage="submission-record",
            venue_status=_openreview_value(content, "venue", ""),
            doi=doi,
            identifiers={"openreview": forum_id, "doi": doi},
            url=f"https://openreview.net/forum?id={quote(forum_id)}",
            pdf_url=_clean_url(_openreview_value(content, "pdf", ""), base="https://openreview.net"),
            license_name=note.get("license"),
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            records.append(item)
        if len(records) >= context.per_source:
            break
    return records, len(notes)


def _crossref_author(author: Any) -> str:
    if not isinstance(author, dict):
        return _clean_text(author)
    literal = _clean_text(author.get("name"))
    if literal:
        return literal
    return _clean_text(" ".join(filter(None, [author.get("given"), author.get("family")])))


def _search_crossref(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    parameters: dict[str, Any] = {
        "query.bibliographic": context.query,
        "rows": context.per_source,
    }
    contact = _clean_text(context.environment.get("BUILDSTUDIO_ACADEMIC_CONTACT"))
    if contact and "@" in contact:
        parameters["mailto"] = contact
    with _provider_rate_gate("crossref", context):
        payload = _fetch_json(
            f"https://api.crossref.org/works?{urlencode(parameters)}",
            timeout=context.timeout,
            opener=context.opener,
            deadline=context.deadline,
        )
    message = payload.get("message") if isinstance(payload, dict) else None
    items = message.get("items") if isinstance(message, dict) else None
    if not isinstance(items, list):
        raise ProviderError("invalid_response", "Crossref response omitted work items.")
    records: list[dict[str, Any]] = []
    for rank, work in enumerate(items, start=1):
        if not isinstance(work, dict):
            continue
        doi = _normalize_doi(work.get("DOI"))
        work_type = _clean_text(work.get("type")).lower()
        is_preprint = work_type in {"posted-content", "preprint"}
        publication_date = None
        for field in ("published-print", "published-online", "published", "issued"):
            publication_date = _date_from_parts(work.get(field))
            if publication_date:
                break
        links = [item for item in _as_list(work.get("link")) if isinstance(item, dict)]
        pdf_url = next(
            (
                item.get("URL")
                for item in links
                if item.get("content-type") == "application/pdf"
            ),
            None,
        )
        licenses = [item for item in _as_list(work.get("license")) if isinstance(item, dict)]
        item = _record(
            "crossref",
            doi or f"work-{rank}",
            rank,
            _first_text(work.get("title")),
            authors=[_crossref_author(author) for author in _as_list(work.get("author"))],
            publication_date=publication_date,
            abstract=work.get("abstract"),
            venue=_first_text(work.get("container-title")),
            work_type=work_type,
            publication_stage="preprint" if is_preprint else "published",
            doi=doi,
            identifiers={"doi": doi},
            url=work.get("URL") or (f"https://doi.org/{doi}" if doi else None),
            pdf_url=pdf_url,
            citation_count=work.get("is-referenced-by-count"),
            license_name=_first_text([entry.get("URL") for entry in licenses]),
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            records.append(item)
    return records, len(items)


def _openalex_abstract(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in list(value.items())[:20_000]:
        for index in _as_list(indexes)[:1_000]:
            number = _safe_int(index)
            if number is not None and number >= 0:
                positions.append((number, _clean_text(word)))
    positions.sort(key=lambda item: item[0])
    return " ".join(word for _, word in positions if word)


def _openalex_location(work: Mapping[str, Any]) -> tuple[str | None, str | None, str, str]:
    best_oa_location = work.get("best_oa_location")
    primary_location = work.get("primary_location")
    locations = [best_oa_location, primary_location]
    landing_url: str | None = None
    pdf_url: str | None = None
    primary_source = (
        primary_location.get("source")
        if isinstance(primary_location, Mapping)
        and isinstance(primary_location.get("source"), Mapping)
        else {}
    )
    venue = _clean_text(primary_source.get("display_name"))
    license_name = ""
    for location in locations:
        if not isinstance(location, Mapping):
            continue
        source = (
            location.get("source")
            if isinstance(location.get("source"), Mapping)
            else {}
        )
        landing_url = landing_url or _clean_url(location.get("landing_page_url"))
        pdf_url = pdf_url or _clean_url(location.get("pdf_url"))
        venue = venue or _clean_text(source.get("display_name"))
        license_name = license_name or _clean_text(location.get("license"))
    return landing_url, pdf_url, venue, license_name


def _openalex_publication_stage(
    work: Mapping[str, Any], work_type: str, doi: str | None
) -> str:
    """Classify the work from OpenAlex's version-level publication evidence."""
    locations = [work.get("primary_location"), work.get("best_oa_location")]
    saw_submitted = False
    saw_arxiv = False
    for location in locations:
        if not isinstance(location, Mapping):
            continue
        version = _clean_text(location.get("version")).lower()
        is_published = location.get("is_published")
        if is_published is True or version == "publishedversion":
            return "published"
        if version == "submittedversion":
            saw_submitted = True

        source = (
            location.get("source")
            if isinstance(location.get("source"), Mapping)
            else {}
        )
        source_id = _clean_text(source.get("id")).lower().rstrip("/")
        source_name = _clean_text(source.get("display_name")).lower()
        if source_id.endswith("/s4306400194") or source_name == "arxiv (cornell university)":
            saw_arxiv = True
        for field in ("landing_page_url", "pdf_url"):
            candidate = _clean_url(location.get(field))
            hostname = (urlparse(candidate).hostname or "").lower() if candidate else ""
            if hostname == "arxiv.org" or hostname.endswith(".arxiv.org"):
                saw_arxiv = True

    normalized_type = _clean_text(work_type).lower()
    if (
        "preprint" in normalized_type
        or normalized_type == "posted-content"
        or saw_submitted
        or saw_arxiv
        or bool(doi and doi.startswith("10.48550/arxiv."))
    ):
        return "preprint"
    return "unknown"


def _search_openalex(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    fields = (
        "id,doi,title,authorships,publication_date,publication_year,primary_location,"
        "best_oa_location,open_access,ids,type,type_crossref,cited_by_count,"
        "abstract_inverted_index"
    )
    parameters = {"search": context.query, "per_page": context.per_source, "select": fields}
    sensitive_headers: dict[str, str] = {}
    api_key = context.environment.get("OPENALEX_API_KEY", "").strip()
    if api_key:
        sensitive_headers["Authorization"] = f"Bearer {api_key}"
    payload = _fetch_json(
        f"https://api.openalex.org/works?{urlencode(parameters)}",
        timeout=context.timeout,
        opener=context.opener,
        sensitive_headers=sensitive_headers,
        deadline=context.deadline,
    )
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ProviderError("invalid_response", "OpenAlex response omitted results.")
    records: list[dict[str, Any]] = []
    for rank, work in enumerate(items, start=1):
        if not isinstance(work, dict):
            continue
        openalex_id = _identifier_value("openalex", work.get("id"))
        doi = _normalize_doi(work.get("doi"))
        authors: list[str] = []
        for authorship in _as_list(work.get("authorships")):
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author")
            if isinstance(author, dict):
                authors.append(_clean_text(author.get("display_name")))
        landing_url, pdf_url, venue, location_license = _openalex_location(work)
        access = work.get("open_access") if isinstance(work.get("open_access"), dict) else {}
        is_oa = access.get("is_oa") if isinstance(access.get("is_oa"), bool) else None
        external_ids = work.get("ids") if isinstance(work.get("ids"), dict) else {}
        identifiers: dict[str, Any] = {"openalex": openalex_id, "doi": doi}
        for namespace in ("pmid", "pmcid", "arxiv"):
            if external_ids.get(namespace):
                identifiers[namespace] = external_ids[namespace]
        work_type = _clean_text(work.get("type_crossref") or work.get("type")).lower()
        publication_stage = _openalex_publication_stage(work, work_type, doi)
        item = _record(
            "openalex",
            openalex_id or doi or f"work-{rank}",
            rank,
            work.get("title"),
            authors=authors,
            publication_date=work.get("publication_date"),
            year=work.get("publication_year"),
            abstract=_openalex_abstract(work.get("abstract_inverted_index")),
            venue=venue,
            work_type=work_type,
            publication_stage=publication_stage,
            doi=doi,
            identifiers=identifiers,
            url=landing_url or access.get("oa_url") or work.get("id"),
            pdf_url=pdf_url,
            is_open_access=is_oa,
            citation_count=work.get("cited_by_count"),
            license_name=location_license,
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            records.append(item)
    return records, len(items)


def _search_europe_pmc(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    parameters = {
        "query": context.query,
        "format": "json",
        "resultType": "core",
        "pageSize": context.per_source,
    }
    payload = _fetch_json(
        f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{urlencode(parameters)}",
        timeout=context.timeout,
        opener=context.opener,
        deadline=context.deadline,
    )
    result_list = payload.get("resultList") if isinstance(payload, dict) else None
    items = result_list.get("result") if isinstance(result_list, dict) else None
    if not isinstance(items, list):
        raise ProviderError("invalid_response", "Europe PMC response omitted results.")
    records: list[dict[str, Any]] = []
    for rank, work in enumerate(items, start=1):
        if not isinstance(work, dict):
            continue
        source_id = _clean_text(work.get("id") or work.get("pmid") or work.get("pmcid"))
        source = _clean_text(work.get("source") or "MED")
        doi = _normalize_doi(work.get("doi"))
        author_list = work.get("authorList") if isinstance(work.get("authorList"), dict) else {}
        authors = _string_list(author_list.get("author"))
        if not authors and work.get("authorString"):
            authors = [_clean_text(work.get("authorString"))]
        pub_types = work.get("pubTypeList") if isinstance(work.get("pubTypeList"), dict) else {}
        work_types = _string_list(pub_types.get("pubType"))
        type_text = "; ".join(work_types)
        preprint = source.upper() == "PPR" or "preprint" in type_text.lower()
        full_text_urls = work.get("fullTextUrlList")
        if isinstance(full_text_urls, dict):
            full_text_urls = full_text_urls.get("fullTextUrl")
        pdf_url = None
        for link in _as_list(full_text_urls):
            if not isinstance(link, dict):
                continue
            candidate = _clean_url(link.get("url"))
            if candidate and (
                _clean_text(link.get("documentStyle")).lower() == "pdf"
                or candidate.lower().endswith(".pdf")
            ):
                pdf_url = candidate
                break
        is_oa_value = work.get("isOpenAccess")
        is_oa = is_oa_value if isinstance(is_oa_value, bool) else None
        if isinstance(is_oa_value, str):
            is_oa = is_oa_value.strip().lower() in {"y", "yes", "true", "1"}
        item = _record(
            "europe-pmc",
            source_id,
            rank,
            work.get("title"),
            authors=authors,
            publication_date=work.get("firstPublicationDate"),
            year=work.get("pubYear"),
            abstract=work.get("abstractText"),
            venue=work.get("journalTitle"),
            work_type=type_text,
            publication_stage="preprint" if preprint else "published",
            doi=doi,
            identifiers={
                "doi": doi,
                "pmid": work.get("pmid"),
                "pmcid": work.get("pmcid"),
            },
            url=f"https://europepmc.org/article/{quote(source)}/{quote(source_id)}",
            pdf_url=pdf_url,
            is_open_access=is_oa,
            citation_count=work.get("citedByCount"),
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            records.append(item)
    return records, len(items)


def _datacite_title(value: Any) -> str:
    for item in _as_list(value):
        if isinstance(item, dict):
            title = _clean_text(item.get("title"))
        else:
            title = _clean_text(item)
        if title:
            return title
    return ""


def _datacite_authors(value: Any) -> list[str]:
    authors: list[str] = []
    for creator in _as_list(value):
        if not isinstance(creator, dict):
            name = _clean_text(creator)
        else:
            name = _clean_text(creator.get("name")) or _clean_text(
                " ".join(filter(None, [creator.get("givenName"), creator.get("familyName")]))
            )
        if name:
            authors.append(name)
    return authors


def _search_datacite(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    parameters = {
        "query": context.query,
        "page[size]": context.per_source,
        "sort": "relevance",
    }
    payload = _fetch_json(
        f"https://api.datacite.org/dois?{urlencode(parameters)}",
        timeout=context.timeout,
        opener=context.opener,
        deadline=context.deadline,
    )
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ProviderError("invalid_response", "DataCite response omitted DOI records.")
    records: list[dict[str, Any]] = []
    for rank, work in enumerate(items, start=1):
        if not isinstance(work, dict):
            continue
        attributes = work.get("attributes") if isinstance(work.get("attributes"), dict) else {}
        doi = _normalize_doi(attributes.get("doi") or work.get("id"))
        descriptions = [item for item in _as_list(attributes.get("descriptions")) if isinstance(item, dict)]
        abstract = next(
            (
                item.get("description")
                for item in descriptions
                if _clean_text(item.get("descriptionType")).lower() == "abstract"
            ),
            "",
        )
        date_value = attributes.get("published")
        if not date_value:
            dates = [item for item in _as_list(attributes.get("dates")) if isinstance(item, dict)]
            date_value = next(
                (
                    item.get("date")
                    for item in dates
                    if _clean_text(item.get("dateType")).lower() in {"issued", "available"}
                ),
                None,
            )
        type_data = attributes.get("types") if isinstance(attributes.get("types"), dict) else {}
        resource_type = _clean_text(type_data.get("resourceType")).lower()
        resource_type_general = _clean_text(type_data.get("resourceTypeGeneral")).lower()
        work_type = resource_type or resource_type_general
        preprint = resource_type_general == "preprint" or "preprint" in resource_type
        rights_items = [item for item in _as_list(attributes.get("rightsList")) if isinstance(item, dict)]
        license_name = _first_text(
            [entry.get("rights") or entry.get("rightsUri") for entry in rights_items]
        )
        rights_blob = " ".join(
            _clean_text(entry.get("rightsUri") or entry.get("rightsIdentifier"))
            for entry in rights_items
        ).lower()
        is_oa = True if "creativecommons.org" in rights_blob or "cc-" in rights_blob else None
        content_urls = [_clean_url(item) for item in _as_list(attributes.get("contentUrl"))]
        pdf_url = next((item for item in content_urls if item and item.lower().endswith(".pdf")), None)
        container = attributes.get("container") if isinstance(attributes.get("container"), dict) else {}
        publisher = attributes.get("publisher")
        venue = _first_text(container.get("title")) or _first_text(publisher)
        item = _record(
            "datacite",
            doi or work.get("id") or f"work-{rank}",
            rank,
            _datacite_title(attributes.get("titles")),
            authors=_datacite_authors(attributes.get("creators")),
            publication_date=date_value,
            year=attributes.get("publicationYear"),
            abstract=abstract,
            venue=venue,
            work_type=work_type,
            publication_stage="preprint" if preprint else "published",
            doi=doi,
            identifiers={"doi": doi, "datacite": work.get("id")},
            url=attributes.get("url") or (f"https://doi.org/{doi}" if doi else None),
            pdf_url=pdf_url,
            is_open_access=is_oa,
            license_name=license_name,
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            records.append(item)
    return records, len(items)


def _search_doaj(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    query_path = quote(context.query, safe="")
    parameters = {"pageSize": context.per_source, "page": 1}
    payload = _fetch_json(
        f"https://doaj.org/api/search/articles/{query_path}?{urlencode(parameters)}",
        timeout=context.timeout,
        opener=context.opener,
        deadline=context.deadline,
    )
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ProviderError("invalid_response", "DOAJ response omitted article results.")
    records: list[dict[str, Any]] = []
    for rank, work in enumerate(items, start=1):
        if not isinstance(work, dict):
            continue
        bibliography = work.get("bibjson") if isinstance(work.get("bibjson"), dict) else {}
        identifiers = [item for item in _as_list(bibliography.get("identifier")) if isinstance(item, dict)]
        doi = _normalize_doi(
            [item.get("id") for item in identifiers if _clean_text(item.get("type")).lower() == "doi"]
        )
        links = [item for item in _as_list(bibliography.get("link")) if isinstance(item, dict)]
        pdf_url = next(
            (
                item.get("url")
                for item in links
                if _clean_text(item.get("type")).lower() in {"fulltext", "pdf"}
                and _clean_text(item.get("url")).lower().endswith(".pdf")
            ),
            None,
        )
        journal = bibliography.get("journal") if isinstance(bibliography.get("journal"), dict) else {}
        license_items = [item for item in _as_list(bibliography.get("license")) if isinstance(item, dict)]
        license_name = _first_text(
            [item.get("title") or item.get("type") or item.get("url") for item in license_items]
        )
        article_id = _clean_text(work.get("id"))
        item = _record(
            "doaj",
            article_id,
            rank,
            bibliography.get("title"),
            authors=bibliography.get("author"),
            year=bibliography.get("year"),
            abstract=bibliography.get("abstract"),
            venue=journal.get("title"),
            work_type="journal-article",
            publication_stage="published",
            doi=doi,
            identifiers={"doi": doi, "doaj": article_id},
            url=f"https://doaj.org/article/{quote(article_id)}",
            pdf_url=pdf_url,
            is_open_access=True,
            license_name=license_name,
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            records.append(item)
    return records, len(items)


def _openaire_authors(value: Any) -> list[str]:
    authors: list[str] = []
    for author in _as_list(value):
        if isinstance(author, dict):
            name = _clean_text(
                author.get("fullName")
                or author.get("displayName")
                or author.get("name")
            )
        else:
            name = _clean_text(author)
        if name:
            authors.append(name)
    return authors


def _openaire_identifiers(work: Mapping[str, Any]) -> dict[str, Any]:
    identifiers: dict[str, list[str]] = {"openaire": [_clean_text(work.get("id"))]}
    pids = work.get("pids") or work.get("ids")
    if isinstance(pids, dict):
        iterable = [{"scheme": key, "value": value} for key, value in pids.items()]
    else:
        iterable = _as_list(pids)
    for item in iterable:
        if not isinstance(item, dict):
            continue
        namespace = _clean_text(
            item.get("scheme") or item.get("type") or item.get("name")
        ).lower()
        if namespace == "pmc":
            namespace = "pmcid"
        value = item.get("value") or item.get("id")
        if namespace and value:
            identifiers.setdefault(namespace, []).extend(_string_list(value))
    return identifiers


def _openaire_instance_metadata(
    work: Mapping[str, Any],
) -> tuple[str, str, list[str], str]:
    instance_types: list[str] = []
    license_name = ""
    urls: list[str] = []
    for instance in _as_list(work.get("instances")):
        if not isinstance(instance, Mapping):
            continue
        instance_type = _clean_text(instance.get("type"), 200)
        if instance_type and instance_type not in instance_types:
            instance_types.append(instance_type)
        if not license_name:
            license_name = _first_text(instance.get("license"))
        for candidate in _iter_strings(instance.get("urls")):
            cleaned = _clean_url(candidate)
            if cleaned and cleaned not in urls:
                urls.append(cleaned)
    return (
        _first_text(instance_types).lower(),
        license_name,
        urls,
        _openaire_publication_stage(instance_types),
    )


def _openaire_publication_stage(instance_types: str | Sequence[str]) -> str:
    candidates = [instance_types] if isinstance(instance_types, str) else instance_types
    normalized_types = [
        re.sub(r"[^a-z0-9]+", " ", _clean_text(candidate).lower()).strip()
        for candidate in candidates
    ]
    normalized_types = [item for item in normalized_types if item]
    if not normalized_types:
        return "unknown"
    published_types = {
        "article",
        "journal article",
        "conference object",
        "conference paper",
        "conference proceedings",
        "book",
        "book chapter",
        "part of book or chapter",
        "doctoral thesis",
        "master thesis",
        "thesis",
        "report",
        "review",
        "letter",
        "editorial",
    }
    if any(item in published_types for item in normalized_types):
        return "published"
    if any(
        marker in item
        for item in normalized_types
        for marker in ("preprint", "working paper", "submitted version")
    ):
        return "preprint"
    return "unknown"


def _search_openaire(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    parameters = {
        "search": context.query,
        "type": "publication",
        "page": 1,
        "pageSize": context.per_source,
    }
    payload = _fetch_json(
        f"https://api.openaire.eu/graph/v3/research-products?{urlencode(parameters)}",
        timeout=context.timeout,
        opener=context.opener,
        deadline=context.deadline,
    )
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ProviderError("invalid_response", "OpenAIRE response omitted research products.")
    records: list[dict[str, Any]] = []
    for rank, work in enumerate(items, start=1):
        if not isinstance(work, dict):
            continue
        identifiers = _openaire_identifiers(work)
        doi = _normalize_doi(identifiers.get("doi") or work.get("doi") or work.get("pids"))
        descriptions = _as_list(work.get("descriptions") or work.get("description"))
        abstract = ""
        for description in descriptions:
            if isinstance(description, dict):
                candidate = description.get("value") or description.get("description")
                kind = _clean_text(description.get("type")).lower()
                if kind in {"abstract", "summary", ""} and candidate:
                    abstract = candidate
                    break
            elif description:
                abstract = description
                break
        work_type, license_name, urls, publication_stage = _openaire_instance_metadata(work)
        access = work.get("bestAccessRight")
        access_label = (
            _clean_text(access.get("label")).upper()
            if isinstance(access, Mapping)
            else ""
        )
        if access_label == "OPEN":
            is_oa: bool | None = True
        elif access_label in {"CLOSED", "EMBARGO", "RESTRICTED"}:
            is_oa = False
        else:
            is_oa = None
        pdf_url = next((item for item in urls if item and item.lower().endswith(".pdf")), None)
        indicators = work.get("indicators")
        citation_impact = (
            indicators.get("citationImpact")
            if isinstance(indicators, Mapping)
            else None
        )
        citation_count = (
            citation_impact.get("citationCount")
            if isinstance(citation_impact, Mapping)
            else None
        )
        container = work.get("container")
        venue = (
            _first_text(container.get("name"))
            if isinstance(container, Mapping)
            else ""
        )
        venue = (
            venue
            or _first_text(work.get("journal"))
            or _first_text(work.get("publisher"))
        )
        source_id = _clean_text(work.get("id"))
        item = _record(
            "openaire",
            source_id,
            rank,
            work.get("mainTitle") or work.get("title"),
            authors=_openaire_authors(work.get("authors")),
            publication_date=work.get("publicationDate") or work.get("dateOfAcceptance"),
            year=work.get("publicationYear"),
            abstract=abstract,
            venue=venue,
            work_type=work_type,
            publication_stage=publication_stage,
            doi=doi,
            identifiers={**identifiers, "doi": doi},
            url=(next((item for item in urls if item), None) or (f"https://doi.org/{doi}" if doi else None)),
            pdf_url=pdf_url,
            is_open_access=is_oa,
            citation_count=citation_count,
            license_name=license_name,
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            records.append(item)
    return records, len(items)


def _search_semantic_scholar(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    fields = (
        "paperId,title,abstract,authors,year,publicationDate,venue,externalIds,url,"
        "openAccessPdf,publicationTypes,citationCount"
    )
    parameters = {"query": context.query, "limit": context.per_source, "fields": fields}
    sensitive_headers: dict[str, str] = {}
    api_key = context.environment.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if api_key:
        sensitive_headers["x-api-key"] = api_key
    payload = _fetch_json(
        f"https://api.semanticscholar.org/graph/v1/paper/search?{urlencode(parameters)}",
        timeout=context.timeout,
        opener=context.opener,
        sensitive_headers=sensitive_headers,
        deadline=context.deadline,
    )
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ProviderError("invalid_response", "Semantic Scholar response omitted papers.")
    records: list[dict[str, Any]] = []
    for rank, work in enumerate(items, start=1):
        if not isinstance(work, dict):
            continue
        external = work.get("externalIds") if isinstance(work.get("externalIds"), dict) else {}
        doi = _normalize_doi(external.get("DOI"))
        publication_types = _string_list(work.get("publicationTypes"))
        type_text = "; ".join(publication_types)
        preprint = "preprint" in type_text.lower() or (
            not publication_types and bool(_normalize_arxiv_id(external.get("ArXiv")))
        )
        oa_pdf = work.get("openAccessPdf") if isinstance(work.get("openAccessPdf"), dict) else {}
        paper_id = _clean_text(work.get("paperId"))
        identifiers: dict[str, Any] = {
            "semantic-scholar": paper_id,
            "doi": doi,
        }
        for source_key, namespace in (("ArXiv", "arxiv"), ("PubMed", "pmid"), ("DBLP", "dblp")):
            if external.get(source_key):
                identifiers[namespace] = external[source_key]
        item = _record(
            "semantic-scholar",
            paper_id,
            rank,
            work.get("title"),
            authors=work.get("authors"),
            publication_date=work.get("publicationDate"),
            year=work.get("year"),
            abstract=work.get("abstract"),
            venue=work.get("venue"),
            work_type=type_text,
            publication_stage="preprint" if preprint else "published",
            doi=doi,
            identifiers=identifiers,
            url=work.get("url"),
            pdf_url=oa_pdf.get("url"),
            is_open_access=True if oa_pdf.get("url") else None,
            citation_count=work.get("citationCount"),
            license_name=oa_pdf.get("license"),
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            records.append(item)
    return records, len(items)


def _dblp_text(value: Any) -> str:
    if isinstance(value, dict):
        return _clean_text(value.get("text") or value.get("#text") or value.get("value"))
    return _clean_text(value)


def _search_dblp(context: SearchContext) -> tuple[list[dict[str, Any]], int]:
    parameters = {
        "q": context.query,
        "format": "json",
        "h": context.per_source,
        "f": 0,
        "c": 0,
    }
    payload = _fetch_json(
        f"https://dblp.org/search/publ/api?{urlencode(parameters)}",
        timeout=context.timeout,
        opener=context.opener,
        deadline=context.deadline,
    )
    result = payload.get("result") if isinstance(payload, dict) else None
    hits = result.get("hits") if isinstance(result, dict) else None
    if not isinstance(hits, dict):
        raise ProviderError("invalid_response", "DBLP response omitted publication hits.")
    items = hits.get("hit") if isinstance(hits, dict) else None
    if items is None:
        if _safe_int(hits.get("@total")) == 0:
            return [], 0
        raise ProviderError("invalid_response", "DBLP response omitted publication hits.")
    items = _as_list(items)
    records: list[dict[str, Any]] = []
    for rank, hit in enumerate(items, start=1):
        if not isinstance(hit, dict):
            continue
        info = hit.get("info") if isinstance(hit.get("info"), dict) else {}
        author_data = info.get("authors") if isinstance(info.get("authors"), dict) else {}
        authors = [_dblp_text(author) for author in _as_list(author_data.get("author"))]
        electronic = [_clean_url(item) for item in _as_list(info.get("ee"))]
        doi = _normalize_doi([info.get("doi"), electronic])
        key = _clean_text(info.get("key"))
        work_type = _clean_text(info.get("type")).lower()
        preprint = "informal" in work_type or "preprint" in work_type
        item = _record(
            "dblp",
            key,
            rank,
            _dblp_text(info.get("title")),
            authors=authors,
            year=info.get("year"),
            venue=info.get("venue"),
            work_type=work_type,
            publication_stage="preprint" if preprint else "published",
            doi=doi,
            identifiers={"dblp": key, "doi": doi},
            url=next((item for item in electronic if item), None)
            or (f"https://dblp.org/rec/{quote(key)}" if key else None),
            abstract_chars=context.abstract_chars,
        )
        if item is not None:
            records.append(item)
    return records, len(items)


PROVIDERS: dict[str, Callable[[SearchContext], tuple[list[dict[str, Any]], int]]] = {
    "arxiv": _search_arxiv,
    "openreview": _search_openreview,
    "crossref": _search_crossref,
    "openalex": _search_openalex,
    "europe-pmc": _search_europe_pmc,
    "datacite": _search_datacite,
    "doaj": _search_doaj,
    "openaire": _search_openaire,
    "semantic-scholar": _search_semantic_scholar,
    "dblp": _search_dblp,
}


def _source_order(source: str) -> int:
    try:
        return ALL_SOURCES.index(source)
    except ValueError:
        return len(ALL_SOURCES)


def _normalize_sources(sources: Sequence[str] | None) -> tuple[str, ...]:
    aliases = {
        "europe_pmc": "europe-pmc",
        "semantic_scholar": "semantic-scholar",
        "s2": "semantic-scholar",
    }
    selected = DEFAULT_SOURCES if sources is None else tuple(sources)
    result: list[str] = []
    for source in selected:
        normalized = aliases.get(source.strip().lower(), source.strip().lower())
        if normalized not in PROVIDERS:
            raise ValueError(f"unsupported source: {source}")
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError("at least one source is required")
    return tuple(result)


def _title_key(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", html.unescape(title)).casefold()
    characters = [
        character if unicodedata.category(character)[0] in {"L", "N"} else " "
        for character in normalized
    ]
    return " ".join("".join(characters).split())


def _specific_title(key: str) -> bool:
    compact = key.replace(" ", "")
    words = key.split()
    return key not in {"editorial", "introduction", "preface", "correction"} and (
        len(compact) >= 25 or len(words) >= 4
    )


def _author_keys(authors: Sequence[str]) -> set[str]:
    keys: set[str] = set()
    for author in authors:
        normalized = _title_key(author)
        if not normalized:
            continue
        keys.add(normalized)
    return keys


def _safe_title_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    key = _title_key(str(left.get("title", "")))
    if key != _title_key(str(right.get("title", ""))) or not _specific_title(key):
        return False
    left_year = _safe_int(left.get("year"))
    right_year = _safe_int(right.get("year"))
    if left_year is not None and right_year is not None and abs(left_year - right_year) > 1:
        return False
    left_authors = _author_keys(left.get("authors") or [])
    right_authors = _author_keys(right.get("authors") or [])
    if (
        not left_authors
        or not right_authors
        or not left_authors.intersection(right_authors)
    ):
        return False
    return True


def _strong_identifiers(record: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    identifiers = record.get("identifiers")
    if isinstance(identifiers, Mapping):
        for raw_namespace, values in identifiers.items():
            namespace = str(raw_namespace).lower().replace("_", "-")
            if namespace not in STRONG_IDENTIFIER_NAMESPACES:
                continue
            for value in _as_list(values):
                normalized = _identifier_value(namespace, value)
                if normalized:
                    result.setdefault(namespace, set()).add(normalized.casefold())
    doi = _normalize_doi(record.get("doi"))
    if doi:
        result.setdefault("doi", set()).add(doi.casefold())
    return result


class _DisjointSet:
    def __init__(self, records: Sequence[Mapping[str, Any]]):
        self.parent = list(range(len(records)))
        self.strong_identifiers = [
            _strong_identifiers(record) for record in records
        ]

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(
        self, left: int, right: int, *, reject_strong_conflicts: bool = False
    ) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return True
        if reject_strong_conflicts:
            left_ids = self.strong_identifiers[left_root]
            right_ids = self.strong_identifiers[right_root]
            for namespace in STRONG_IDENTIFIER_NAMESPACES:
                left_values = left_ids.get(namespace, set())
                right_values = right_ids.get(namespace, set())
                if (
                    left_values
                    and right_values
                    and left_values.isdisjoint(right_values)
                ):
                    return False

        new_root = min(left_root, right_root)
        old_root = max(left_root, right_root)
        self.parent[old_root] = new_root
        for namespace, values in self.strong_identifiers[old_root].items():
            self.strong_identifiers[new_root].setdefault(namespace, set()).update(
                values
            )
        self.strong_identifiers[old_root] = {}
        return True


def _deduplicate(records: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    disjoint = _DisjointSet(records)
    exact_indexes: dict[tuple[str, str], list[int]] = {}
    for index, record in enumerate(records):
        identifiers = record.get("identifiers") or {}
        for namespace, values in identifiers.items():
            canonical_namespace = str(namespace).lower().replace("_", "-")
            for value in _as_list(values):
                normalized = _identifier_value(canonical_namespace, value)
                if not normalized:
                    continue
                key = (canonical_namespace, normalized.casefold())
                for other_index in exact_indexes.get(key, []):
                    disjoint.union(
                        index,
                        other_index,
                        reject_strong_conflicts=True,
                    )
                exact_indexes.setdefault(key, []).append(index)

    title_indexes: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        title = _title_key(record.get("title", ""))
        if _specific_title(title):
            for other_index in title_indexes.get(title, []):
                if _safe_title_match(record, records[other_index]):
                    disjoint.union(
                        index,
                        other_index,
                        reject_strong_conflicts=True,
                    )
            title_indexes.setdefault(title, []).append(index)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, record in enumerate(records):
        groups.setdefault(disjoint.find(index), []).append(record)
    return [groups[index] for index in sorted(groups)]


def _best_record(records: Sequence[dict[str, Any]], field: str, *, longest: bool = False) -> Any:
    candidates = [record for record in records if record.get(field) not in (None, "", [])]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda record: (
            len(record.get(field)) if longest and hasattr(record.get(field), "__len__") else 0,
            SOURCE_QUALITY.get(record["source"], 0),
            -record.get("rank", 999_999),
        ),
    ).get(field)


def _merge_group(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    identifiers: dict[str, list[str]] = {}
    for record in records:
        for namespace, values in (record.get("identifiers") or {}).items():
            for value in _as_list(values):
                normalized = _identifier_value(namespace, value)
                if normalized:
                    identifiers.setdefault(namespace, [])
                    if normalized not in identifiers[namespace]:
                        identifiers[namespace].append(normalized)
    identifiers = {
        namespace: sorted(values, key=str.casefold)
        for namespace, values in sorted(identifiers.items())
    }
    doi_values = identifiers.get("doi", [])
    doi = doi_values[0] if doi_values else None

    stage_priority = {"unknown": 0, "submission-record": 1, "preprint": 2, "published": 3}
    stage_record = max(
        records,
        key=lambda record: (
            stage_priority.get(record.get("publication_stage", "unknown"), 0),
            SOURCE_QUALITY.get(record["source"], 0),
        ),
    )
    publication_stage = stage_record.get("publication_stage", "unknown")
    work_type = stage_record.get("work_type") or _best_record(records, "work_type")
    title = _best_record(records, "title", longest=True) or "Untitled"
    authors = _best_record(records, "authors", longest=True) or []
    abstract = _best_record(records, "abstract", longest=True) or ""
    publication_date = _best_record(records, "publication_date")
    year = _year_from_date(publication_date) or _best_record(records, "year")
    venue = _best_record(records, "venue") or ""
    venue_id = _best_record(records, "venue_id") or ""
    venue_status = _best_record(records, "venue_status") or ""
    pdf_url = _best_record(records, "pdf_url")
    license_name = _best_record(records, "license") or ""

    access_values = [record.get("is_open_access") for record in records]
    if True in access_values:
        is_open_access: bool | None = True
    elif access_values and all(value is False for value in access_values):
        is_open_access = False
    else:
        is_open_access = None

    citations = [
        record["citation_count"]
        for record in records
        if isinstance(record.get("citation_count"), int)
    ]
    citation_count = max(citations) if citations else None

    best_rank_by_source: dict[str, int] = {}
    for record in records:
        best_rank_by_source[record["source"]] = min(
            record["rank"], best_rank_by_source.get(record["source"], 999_999)
        )
    score = round(sum(1.0 / (60 + rank) for rank in best_rank_by_source.values()), 8)
    sources = sorted(best_rank_by_source, key=_source_order)

    provenance: list[dict[str, Any]] = []
    seen_provenance: set[tuple[str, str]] = set()
    for record in sorted(records, key=lambda item: (_source_order(item["source"]), item["rank"])):
        key = (record["source"], record["source_id"])
        if key in seen_provenance:
            continue
        seen_provenance.add(key)
        detail = {
            "source": record["source"],
            "source_id": record["source_id"],
            "rank": record["rank"],
        }
        if record.get("url"):
            detail["url"] = record["url"]
        if record.get("venue_status"):
            detail["venue_status"] = record["venue_status"]
        if record.get("venue_id"):
            detail["venue_id"] = record["venue_id"]
        provenance.append(detail)

    preferred_identifier = None
    for namespace in (
        "arxiv",
        "pmid",
        "pmcid",
        "openreview",
        "openalex",
        "openaire",
        "doaj",
        "semantic-scholar",
        "dblp",
        "datacite",
    ):
        if identifiers.get(namespace):
            preferred_identifier = f"{namespace}:{identifiers[namespace][0]}"
            break
    record_id = (
        f"doi:{doi}"
        if doi
        else preferred_identifier
        or f"title:{hashlib.sha256(_title_key(title).encode('utf-8')).hexdigest()[:16]}"
    )
    url = f"https://doi.org/{doi}" if doi else _best_record(records, "url")

    return {
        "id": record_id,
        "title": title,
        "authors": authors,
        "year": year,
        "publication_date": publication_date,
        "abstract": abstract,
        "venue": venue,
        "venue_id": venue_id or None,
        "work_type": work_type or "unknown",
        "publication_stage": publication_stage,
        "venue_status": venue_status or None,
        "doi": doi,
        "identifiers": identifiers,
        "url": url,
        "pdf_url": pdf_url,
        "is_open_access": is_open_access,
        "citation_count": citation_count,
        "license": license_name or None,
        "sources": sources,
        "provenance": provenance,
        "score": score,
    }


def _run_provider(source: str, context: SearchContext) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        records, raw_returned = PROVIDERS[source](context)
        return records, {
            "source": source,
            "status": "ok",
            "returned": len(records),
            "raw_returned": raw_returned,
        }
    except ProviderError as error:
        return [], {
            "source": source,
            "status": error.status,
            "returned": 0,
            "raw_returned": 0,
            "retryable": error.retryable,
            "message": error.safe_message,
        }
    except Exception:
        return [], {
            "source": source,
            "status": "internal_error",
            "returned": 0,
            "raw_returned": 0,
            "retryable": False,
            "message": "Provider adapter failed unexpectedly.",
        }


@dataclass
class _ProviderWorkerState:
    source: str
    process: Any
    done: threading.Event
    output: bytes = b""
    communication_failed: bool = False
    thread: threading.Thread | None = None


def _provider_timeout_output(
    source: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return [], {
        "source": source,
        "status": "timeout",
        "returned": 0,
        "raw_returned": 0,
        "retryable": True,
        "message": "Provider request timed out.",
    }


def _provider_internal_error_output(
    source: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return [], {
        "source": source,
        "status": "internal_error",
        "returned": 0,
        "raw_returned": 0,
        "retryable": False,
        "message": "Provider adapter failed unexpectedly.",
    }


def _provider_worker_command(source: str) -> list[str]:
    return [
        sys.executable,
        os.path.abspath(__file__),
        "--internal-provider-worker",
        source,
    ]


def _communicate_provider_worker(
    state: _ProviderWorkerState, request_body: bytes
) -> None:
    try:
        stdout, _ = state.process.communicate(input=request_body)
        state.output = stdout
    except Exception:
        state.communication_failed = True
    finally:
        state.done.set()


def _terminate_and_reap_provider_workers(
    states: Sequence[_ProviderWorkerState],
) -> None:
    live_states = [state for state in states if state.process.poll() is None]
    for state in live_states:
        try:
            state.process.terminate()
        except (OSError, ProcessLookupError):
            pass

    grace_deadline = time.monotonic() + WORKER_TERMINATE_GRACE_SECONDS
    while live_states and time.monotonic() < grace_deadline:
        live_states = [state for state in live_states if state.process.poll() is None]
        if live_states:
            time.sleep(
                min(
                    WORKER_POLL_INTERVAL_SECONDS,
                    max(0.0, grace_deadline - time.monotonic()),
                )
            )

    for state in live_states:
        try:
            state.process.kill()
        except (OSError, ProcessLookupError):
            pass
    for state in states:
        try:
            state.process.wait()
        except (OSError, ChildProcessError):
            pass
    for state in states:
        if state.thread is not None and state.thread.ident is not None:
            state.thread.join()
        for stream in (state.process.stdin, state.process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _decode_provider_worker_output(
    state: _ProviderWorkerState,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if (
        state.communication_failed
        or state.process.returncode != 0
        or len(state.output) > MAX_WORKER_OUTPUT_BYTES
    ):
        return _provider_internal_error_output(state.source)
    try:
        decoded = json.loads(state.output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _provider_internal_error_output(state.source)
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not isinstance(decoded[0], list)
        or not isinstance(decoded[1], dict)
        or decoded[1].get("source") != state.source
    ):
        return _provider_internal_error_output(state.source)
    return decoded[0], decoded[1]


def _minimal_worker_process_environment() -> dict[str, str]:
    values = {
        name: value
        for name in WORKER_PROCESS_ENVIRONMENT_KEYS
        if isinstance((value := os.environ.get(name)), str) and value
    }
    values["PYTHONIOENCODING"] = "utf-8"
    values["PYTHONUTF8"] = "1"
    return values


def _provider_worker_environment(
    environment: Mapping[str, str], source: str
) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in PROVIDER_ENVIRONMENT_KEYS.get(source, ()):
        value = environment.get(name, "")
        values[name] = value if isinstance(value, str) else str(value)
    return values


def _provider_worker_request_body(source: str, context: SearchContext) -> bytes:
    return json.dumps(
        {
            "query": context.query,
            "per_source": context.per_source,
            "abstract_chars": context.abstract_chars,
            "timeout": context.timeout,
            "deadline": context.deadline,
            "environment": _provider_worker_environment(
                context.environment, source
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _ipv4_first_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
    """Prefer reachable IPv4 without removing IPv6 as a fallback."""

    addresses = list(_SYSTEM_GETADDRINFO(*args, **kwargs))
    return sorted(addresses, key=lambda item: item[0] != socket.AF_INET)


@contextmanager
def _prefer_ipv4_connections() -> Iterable[None]:
    """Scope address ordering to one isolated provider worker process."""

    previous = socket.getaddrinfo
    socket.getaddrinfo = _ipv4_first_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = previous


def _run_providers_in_subprocesses(
    sources: Sequence[str], context: SearchContext
) -> dict[str, tuple[list[dict[str, Any]], dict[str, Any]]]:
    if context.deadline is None:
        raise ValueError("production provider workers require a hard deadline")
    outputs: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    states: list[_ProviderWorkerState] = []

    try:
        for source in sources:
            if time.monotonic() >= context.deadline:
                outputs[source] = _provider_timeout_output(source)
                continue
            request_body = _provider_worker_request_body(source, context)
            if len(request_body) > MAX_WORKER_REQUEST_BYTES:
                outputs[source] = _provider_internal_error_output(source)
                continue
            try:
                process = subprocess.Popen(
                    _provider_worker_command(source),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    close_fds=True,
                    env=_minimal_worker_process_environment(),
                )
            except OSError:
                outputs[source] = _provider_internal_error_output(source)
                continue
            state = _ProviderWorkerState(
                source=source,
                process=process,
                done=threading.Event(),
            )
            state.thread = threading.Thread(
                target=_communicate_provider_worker,
                args=(state, request_body),
                name=f"academic-worker-ipc-{source}",
                daemon=True,
            )
            states.append(state)
            try:
                state.thread.start()
            except RuntimeError:
                state.communication_failed = True
                state.done.set()
                _terminate_and_reap_provider_workers([state])

        pending = {state.source: state for state in states}
        while pending:
            for source, state in list(pending.items()):
                if state.done.is_set():
                    outputs[source] = _decode_provider_worker_output(state)
                    pending.pop(source)
            if not pending:
                break
            remaining = context.deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(WORKER_POLL_INTERVAL_SECONDS, remaining))

        # Drain one last snapshot before classifying workers at the absolute
        # deadline. A result fully received by now is not spuriously killed.
        for source, state in list(pending.items()):
            if state.done.is_set():
                outputs[source] = _decode_provider_worker_output(state)
                pending.pop(source)
        timed_out_states = list(pending.values())
        for source in pending:
            outputs[source] = _provider_timeout_output(source)
        _terminate_and_reap_provider_workers(timed_out_states)
    finally:
        # Covers spawn/IPC exceptions and KeyboardInterrupt without leaving a
        # DNS-blocked child or its pipe reader behind.
        _terminate_and_reap_provider_workers(states)

    return outputs


def _provider_worker_main(source: str) -> int:
    try:
        raw_request = sys.stdin.buffer.read(MAX_WORKER_REQUEST_BYTES + 1)
        if len(raw_request) > MAX_WORKER_REQUEST_BYTES:
            return 2
        request = json.loads(raw_request.decode("utf-8"))
        if not isinstance(request, dict) or source not in PROVIDERS:
            return 2
        query = request.get("query")
        per_source = request.get("per_source")
        abstract_chars = request.get("abstract_chars")
        timeout = request.get("timeout")
        deadline = request.get("deadline")
        environment = request.get("environment")
        if (
            not isinstance(query, str)
            or not isinstance(per_source, int)
            or not isinstance(abstract_chars, int)
            or not isinstance(timeout, (int, float))
            or not isinstance(deadline, (int, float))
            or not isinstance(environment, dict)
        ):
            return 2
        safe_environment = {
            name: value
            for name, value in environment.items()
            if name in PROVIDER_ENVIRONMENT_KEYS.get(source, ())
            and isinstance(value, str)
        }
        with _prefer_ipv4_connections():
            output = _run_provider(
                source,
                SearchContext(
                    query=query,
                    per_source=per_source,
                    abstract_chars=abstract_chars,
                    timeout=timeout,
                    opener=urlopen,
                    environment=safe_environment,
                    deadline=float(deadline),
                ),
            )
        encoded = json.dumps(
            output, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_WORKER_OUTPUT_BYTES:
            return 2
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except (BrokenPipeError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 2
    except Exception:
        return 2


def _date_sort_number(value: Any) -> int:
    text = _clean_text(value)
    digits = "".join(character for character in text if character.isdigit())[:8]
    return int(digits.ljust(8, "0")) if digits else 0


def _sort_results(results: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    def stable_tail(item: Mapping[str, Any]) -> tuple[str, str]:
        return (_title_key(str(item.get("title", ""))), str(item.get("id", "")))

    if order == "newest":
        return sorted(
            results,
            key=lambda item: (
                -_date_sort_number(item.get("publication_date") or item.get("year")),
                -float(item.get("score", 0)),
                *stable_tail(item),
            ),
        )
    if order == "citations":
        return sorted(
            results,
            key=lambda item: (
                -(item.get("citation_count") if isinstance(item.get("citation_count"), int) else -1),
                -float(item.get("score", 0)),
                *stable_tail(item),
            ),
        )
    return sorted(
        results,
        key=lambda item: (
            -float(item.get("score", 0)),
            -len(item.get("sources") or []),
            -(_safe_int(item.get("year")) or 0),
            *stable_tail(item),
        ),
    )


def search_academic(
    query: str,
    *,
    sources: Sequence[str] | None = None,
    max_results: int = 20,
    per_source: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    open_access_only: bool = False,
    published_only: bool = False,
    sort: str = "relevance",
    abstract_chars: int = 1_200,
    timeout: int = DEFAULT_TIMEOUT,
    strict_sources: bool = False,
    min_success_sources: int = 1,
    opener: Callable[..., Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    cleaned_query = _clean_text(query)
    if not cleaned_query:
        raise ValueError("query must not be empty")
    if len(cleaned_query) > 2_000:
        raise ValueError("query must not exceed 2000 characters")
    if not 1 <= max_results <= 100:
        raise ValueError("max_results must be between 1 and 100")
    if not 1 <= per_source <= 50:
        raise ValueError("per_source must be between 1 and 50")
    if not 0 <= abstract_chars <= 5_000:
        raise ValueError("abstract_chars must be between 0 and 5000")
    if not 1 <= timeout <= 60:
        raise ValueError("timeout must be between 1 and 60")
    if sort not in {"relevance", "newest", "citations"}:
        raise ValueError(f"unsupported sort order: {sort}")
    for label, year in (("year_from", year_from), ("year_to", year_to)):
        if year is not None and not 1000 <= year <= 3000:
            raise ValueError(f"{label} must be between 1000 and 3000")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ValueError("year_from must not exceed year_to")

    selected_sources = _normalize_sources(sources)
    if not 1 <= min_success_sources <= len(selected_sources):
        raise ValueError("min_success_sources must be between 1 and the source count")

    production_opener = opener is None
    context = SearchContext(
        query=cleaned_query,
        per_source=per_source,
        abstract_chars=abstract_chars,
        timeout=timeout,
        opener=opener or urlopen,
        environment=environment if environment is not None else os.environ,
        deadline=time.monotonic() + timeout,
    )
    provider_outputs: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    if production_opener:
        provider_outputs = _run_providers_in_subprocesses(selected_sources, context)
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(selected_sources)), thread_name_prefix="academic-search"
        ) as executor:
            futures = {
                source: executor.submit(_run_provider, source, context)
                for source in selected_sources
            }
            for source in selected_sources:
                provider_outputs[source] = futures[source].result()

    records: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for source in selected_sources:
        source_records, status = provider_outputs[source]
        records.extend(source_records)
        statuses.append(status)

    merged = [_merge_group(group) for group in _deduplicate(records)]
    deduplicated_count = len(merged)
    if year_from is not None:
        merged = [item for item in merged if isinstance(item.get("year"), int) and item["year"] >= year_from]
    if year_to is not None:
        merged = [item for item in merged if isinstance(item.get("year"), int) and item["year"] <= year_to]
    if open_access_only:
        merged = [item for item in merged if item.get("is_open_access") is True]
    if published_only:
        merged = [item for item in merged if item.get("publication_stage") == "published"]
    filtered_count = len(merged)
    merged = _sort_results(merged, sort)[:max_results]

    successful_count = sum(status["status"] == "ok" for status in statuses)
    partial = successful_count != len(statuses)
    all_sources_failed = successful_count == 0
    source_policy_satisfied = (
        not all_sources_failed
        and successful_count >= min_success_sources
        and (not strict_sources or not partial)
    )
    return {
        "schema_version": 1,
        "query": cleaned_query,
        "requested_sources": list(selected_sources),
        "successful_source_count": successful_count,
        "partial": partial,
        "all_sources_failed": all_sources_failed,
        "source_policy_satisfied": source_policy_satisfied,
        "source_status": statuses,
        "raw_result_count": sum(status.get("raw_returned", 0) for status in statuses),
        "normalized_result_count": len(records),
        "deduplicated_count": deduplicated_count,
        "filtered_count": filtered_count,
        "result_count": len(merged),
        "filters": {
            "year_from": year_from,
            "year_to": year_to,
            "open_access_only": open_access_only,
            "published_only": published_only,
            "sort": sort,
        },
        "results": merged,
    }


def _print_text(payload: Mapping[str, Any]) -> None:
    print(
        f"Academic search returned {payload['result_count']} result(s) from "
        f"{payload['successful_source_count']}/{len(payload['requested_sources'])} successful source(s)."
    )
    for status in payload["source_status"]:
        line = f"- {status['source']}: {status['status']}"
        if status["status"] == "ok":
            line += f" ({status['returned']} normalized)"
        elif status.get("message"):
            line += f" — {status['message']}"
        print(line)
    print()
    for index, result in enumerate(payload["results"], start=1):
        print(f"{index}. {result['title']}")
        print(f"   Sources: {', '.join(result['sources'])}")
        if result.get("authors"):
            print(f"   Authors: {', '.join(result['authors'])}")
        details = [result.get("publication_stage")]
        if result.get("year"):
            details.append(str(result["year"]))
        if result.get("venue"):
            details.append(result["venue"])
        print(f"   Record: {' | '.join(filter(None, details))}")
        if result.get("venue_status"):
            print(f"   OpenReview venue/status: {result['venue_status']}")
        if result.get("venue_id"):
            print(f"   OpenReview venue ID: {result['venue_id']}")
        if result.get("doi"):
            print(f"   DOI: {result['doi']}")
        if result.get("abstract"):
            print(f"   Abstract: {result['abstract']}")
        if result.get("url"):
            print(f"   URL: {result['url']}")
        if result.get("pdf_url"):
            print(f"   PDF: {result['pdf_url']}")
        print()


def _configure_cli_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _bounded_integer(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be between {minimum} and {maximum}"
            )
        return number

    return parse


def _source_listing() -> dict[str, Any]:
    return {
        "default_sources": list(DEFAULT_SOURCES),
        "optional_sources": list(OPTIONAL_SOURCES),
        "sources": [
            {"name": name, **SOURCE_DETAILS[name]}
            for name in ALL_SOURCES
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    _configure_cli_streams()
    parser = argparse.ArgumentParser(
        description="Search public scholarly APIs and merge duplicate paper records."
    )
    parser.add_argument("query", nargs="?", help="Paper topic, title, author, or DOI")
    parser.add_argument("--source", action="append", help="Source name; repeat as needed")
    parser.add_argument("--all-sources", action="store_true", help="Include optional/degraded sources")
    parser.add_argument("--list-sources", action="store_true", help="List available providers and exit")
    parser.add_argument("--max", dest="max_results", type=_bounded_integer(1, 100), default=20)
    parser.add_argument("--per-source", type=_bounded_integer(1, 50), default=10)
    parser.add_argument("--year-from", type=_bounded_integer(1000, 3000))
    parser.add_argument("--year-to", type=_bounded_integer(1000, 3000))
    parser.add_argument("--open-access-only", action="store_true")
    parser.add_argument("--published-only", action="store_true")
    parser.add_argument("--sort", choices=("relevance", "newest", "citations"), default="relevance")
    parser.add_argument("--abstract-chars", type=_bounded_integer(0, 5_000), default=1_200)
    parser.add_argument(
        "--timeout",
        type=_bounded_integer(1, 60),
        default=DEFAULT_TIMEOUT,
        help="Shared wall-clock deadline for the complete provider search in seconds",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.add_argument("--require-results", action="store_true")
    parser.add_argument("--strict-sources", action="store_true")
    parser.add_argument("--min-success-sources", type=_bounded_integer(1, len(ALL_SOURCES)), default=1)
    args = parser.parse_args(argv)

    if args.list_sources:
        listing = _source_listing()
        if args.json:
            print(json.dumps(listing, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            for source in listing["sources"]:
                default = "default" if source["default"] else source["status"]
                print(f"{source['name']}: {default} — {source['description']}")
        return 0
    if not args.query:
        parser.error("query is required unless --list-sources is used")

    selected_sources: Sequence[str] | None
    if args.all_sources:
        selected_sources = ALL_SOURCES
    else:
        selected_sources = args.source
    try:
        payload = search_academic(
            args.query,
            sources=selected_sources,
            max_results=args.max_results,
            per_source=args.per_source,
            year_from=args.year_from,
            year_to=args.year_to,
            open_access_only=args.open_access_only,
            published_only=args.published_only,
            sort=args.sort,
            abstract_chars=args.abstract_chars,
            timeout=args.timeout,
            strict_sources=args.strict_sources,
            min_success_sources=args.min_success_sources,
        )
    except ValueError as error:
        print(f"Academic search failed: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_text(payload)

    if payload["all_sources_failed"]:
        print("All requested academic sources failed.", file=sys.stderr)
        return 2
    if not payload["source_policy_satisfied"]:
        print("Academic source success policy was not satisfied.", file=sys.stderr)
        return 4
    if args.require_results and not payload["results"]:
        print("Academic search returned no usable results.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--internal-provider-worker":
        raise SystemExit(_provider_worker_main(sys.argv[2]))
    raise SystemExit(main())
