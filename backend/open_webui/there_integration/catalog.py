"""Read-only, pinned AAS catalog access inside the THERE application.

The published manifest and content-index format belong to
https://github.com/sickn33/agentic-awesome-skills at
b1aebac60a88dffa0f5723cb3816f22cc0af6b13 (v16.9.1). No upstream program or
skill is executed. Skill text is inert, untrusted content; a digest identifies
the exact text a user reviews before importing it into their native skill store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

DEFAULT_ROOT = "/home/buildstudio/BuildStudio-There/shared/aas/current"
PINNED_VERSION = "16.9.1"
PINNED_DIGEST = "sha256-c093b522827764ab5af5d07917ee32ccb863ab5149d7029147ef09a84d362e7c"
MAX_MANIFEST_BYTES = 512 * 1024
MAX_CATALOG_BYTES = 64 * 1024 * 1024
MAX_SKILL_BYTES = 1024 * 1024
MAX_SKILLS = 100_000
CATALOG_PATH = "data/catalog.json"
INDEX_PATH = "data/aas-v1/skill-content-index.v1.json"
CONTENT_PATH = "data/aas-v1/skill-content.v1.ndjson"
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*")
DIGEST_PATTERN = re.compile(r"sha256-[a-f0-9]{64}")


class CatalogError(RuntimeError):
    """A fixed, path-free error safe for an API response."""

    def __init__(self, message: str, code: str = "catalog_unavailable") -> None:
        super().__init__(message)
        self.code = code


def _digest(payload: bytes) -> str:
    return "sha256-" + hashlib.sha256(payload).hexdigest()


def _json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise CatalogError("Catalog metadata is invalid.", "catalog_invalid") from error
    if not isinstance(value, dict):
        raise CatalogError("Catalog metadata is invalid.", "catalog_invalid")
    return value


def _integer(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 256 and bool(ID_PATTERN.fullmatch(value))


class Catalog:
    """A pinned catalog reader. The optional expected identity supports tests/upgrades.

    ``current`` may be an operator-managed release symlink. Once resolved, no
    descendant symlink, hard link, non-regular file or path outside that release
    is accepted. Every metadata read is bounded and integrity checked.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        expected_version: str = PINNED_VERSION,
        expected_digest: str = PINNED_DIGEST,
    ) -> None:
        try:
            self.root = Path(root or os.environ.get("THERE_AAS_ROOT", DEFAULT_ROOT)).resolve(strict=True)
            if not self.root.is_dir():
                raise OSError("not a directory")
        except (OSError, ValueError, RuntimeError) as error:
            raise CatalogError("The skill catalog is unavailable.") from error
        if not isinstance(expected_digest, str) or not DIGEST_PATTERN.fullmatch(expected_digest):
            raise CatalogError("Catalog identity is invalid.", "catalog_invalid")
        package = _json(self._read("package.json", MAX_MANIFEST_BYTES))
        manifest = _json(self._read("data/aas-v1/catalog-manifest.v1.json", MAX_MANIFEST_BYTES))
        if (
            package.get("name") != "agentic-awesome-skills"
            or package.get("version") != expected_version
            or manifest.get("package") != package["name"]
            or manifest.get("packageVersion") != expected_version
            or manifest.get("catalogDigest") != expected_digest
            or type(manifest.get("digestVersion")) is not int
            or manifest["digestVersion"] != 1
        ):
            raise CatalogError("Catalog identity does not match the pinned release.", "catalog_integrity")
        assets = manifest.get("assets")
        if not isinstance(assets, list) or not assets or len(assets) > 1024:
            raise CatalogError("Catalog manifest is invalid.", "catalog_invalid")
        self.assets: dict[str, dict[str, Any]] = {}
        for asset in assets:
            if (
                not isinstance(asset, dict)
                or set(asset) != {"path", "size", "sha256"}
                or not isinstance(asset["path"], str)
                or asset["path"] in self.assets
                or not _integer(asset["size"], 0, MAX_CATALOG_BYTES)
                or not isinstance(asset["sha256"], str)
                or not DIGEST_PATTERN.fullmatch(asset["sha256"])
            ):
                raise CatalogError("Catalog manifest is invalid.", "catalog_invalid")
            self._parts(asset["path"])
            self.assets[asset["path"]] = asset
        identity = json.dumps(
            {"digestVersion": 1, "assets": assets},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        if _digest(identity) != expected_digest:
            raise CatalogError("Catalog manifest integrity check failed.", "catalog_integrity")
        catalog = _json(self._asset(CATALOG_PATH))
        skills = catalog.get("skills")
        count = manifest.get("skillCount")
        if (
            not _integer(count, 1, MAX_SKILLS)
            or not isinstance(skills, list)
            or len(skills) != count
            or catalog.get("total") != count
        ):
            raise CatalogError("Catalog skill count is invalid.", "catalog_invalid")
        self.skills: dict[str, dict[str, Any]] = {}
        for skill in skills:
            if (
                not isinstance(skill, dict)
                or not _valid_id(skill.get("id"))
                or skill["id"] in self.skills
                or any(not isinstance(skill.get(key), str) for key in ("name", "description", "category", "path"))
                or any(
                    not isinstance(skill.get(key), list)
                    or not all(isinstance(item, str) for item in skill[key])
                    for key in ("tags", "triggers")
                )
            ):
                raise CatalogError("Catalog skill metadata is invalid.", "catalog_invalid")
            self._parts(skill["path"])
            # Discovery IDs can include a grouping prefix (agent-squad/alex),
            # while the authenticated index/NDJSON use the canonical ID (alex).
            # Resolve only from pinned metadata, never from a request parameter.
            canonical_id = skill.get("canonical_id", "")
            if not isinstance(canonical_id, str):
                raise CatalogError("Catalog canonical skill id is invalid.", "catalog_invalid")
            canonical_id = canonical_id or skill["id"]
            if not _valid_id(canonical_id):
                raise CatalogError("Catalog canonical skill id is invalid.", "catalog_invalid")
            self.skills[skill["id"]] = {**skill, "canonical_id": canonical_id}
        self.version = manifest["packageVersion"]
        self.digest = expected_digest
        self._content_index: dict[str, Any] | None = None

    @staticmethod
    def _parts(relative: str) -> list[str]:
        if (
            not relative or len(relative) > 512
            or any(character in relative for character in ("\\", "\x00", ":"))
            or any(part in ("", ".", "..") for part in relative.split("/"))
        ):
            raise CatalogError("Catalog asset path is unsafe.", "catalog_unsafe")
        return relative.split("/")

    def _read(self, relative: str, maximum: int, *, offset: int = 0, length: int | None = None) -> bytes:
        parts = self._parts(relative)
        descriptors: list[int] = []
        try:
            # openat plus O_NOFOLLOW also closes the ancestor-symlink race on
            # production Linux. Windows fixtures use strict containment checks.
            if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
                parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptors.append(parent)
                for part in parts[:-1]:
                    parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    descriptors.append(parent)
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            else:
                candidate = self.root
                for part in parts:
                    candidate = candidate / part
                    if candidate.is_symlink():
                        raise CatalogError("Catalog asset links are not allowed.", "catalog_unsafe")
                candidate.resolve(strict=True).relative_to(self.root)
                descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise CatalogError("Catalog asset is not a regular file without extra hard links.", "catalog_unsafe")
            if metadata.st_size > maximum or offset < 0 or (length is not None and offset + length > metadata.st_size):
                raise CatalogError("Catalog asset exceeds its size limit.", "catalog_too_large")
            os.lseek(descriptor, offset, os.SEEK_SET)
            remaining = length if length is not None else maximum + 1
            chunks = []
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > maximum or (length is not None and len(payload) != length):
                raise CatalogError("Catalog asset size is invalid.", "catalog_too_large")
            return payload
        except CatalogError:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            raise CatalogError("Catalog asset is missing or unsafe.", "catalog_unsafe") from error
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _asset(self, relative: str) -> bytes:
        record = self.assets.get(relative)
        if record is None:
            raise CatalogError("Required catalog asset is missing.", "catalog_invalid")
        payload = self._read(relative, record["size"])
        if len(payload) != record["size"] or _digest(payload) != record["sha256"]:
            raise CatalogError("Catalog asset integrity check failed.", "catalog_integrity")
        return payload

    def search(self, q: str = "", limit: int = 30) -> dict[str, Any]:
        if not isinstance(q, str) or len(q) > 256 or not _integer(limit, 1, 100):
            raise ValueError("Catalog query must be at most 256 characters and limit between 1 and 100.")
        terms = q.casefold().split()
        matches = []
        for skill in self.skills.values():
            text = " ".join([
                skill["id"], skill["canonical_id"], skill["name"], skill["description"], skill["category"],
                *skill["tags"], *skill["triggers"],
            ]).casefold()
            if all(term in text for term in terms):
                matches.append({key: skill[key] for key in ("id", "canonical_id", "name", "description", "category")})
        return {"items": matches[:limit], "total": len(matches), "version": self.version, "catalog_digest": self.digest}

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        if not _valid_id(skill_id):
            raise ValueError("Skill id is invalid.")
        skill = self.skills.get(skill_id)
        if skill is None:
            raise CatalogError("Skill was not found in the catalog.", "skill_not_found")
        if self._content_index is None:
            index = _json(self._asset(INDEX_PATH))
            entries = index.get("entries")
            if not isinstance(entries, dict):
                raise CatalogError("Catalog content index is invalid.", "catalog_invalid")
            # Cache only after manifest-bound integrity verification. Each new
            # Catalog instance re-verifies its own release identity and index.
            self._content_index = entries
        canonical_id = skill["canonical_id"]
        record = self._content_index.get(canonical_id)
        if (
            not isinstance(record, dict)
            or not _integer(record.get("offset"), 0, MAX_CATALOG_BYTES)
            or not _integer(record.get("length"), 1, MAX_SKILL_BYTES)
            or not isinstance(record.get("sha256"), str)
            or not DIGEST_PATTERN.fullmatch(record["sha256"])
        ):
            raise CatalogError("Skill content reference is invalid.", "catalog_invalid")
        content_asset = self.assets.get(CONTENT_PATH)
        if not content_asset:
            raise CatalogError("Skill content asset is unavailable.", "catalog_invalid")
        # The signed-in-release index binds the exact range hash. Reading just
        # that range avoids loading the entire 16 MB content bundle per request.
        payload = self._read(CONTENT_PATH, content_asset["size"], offset=record["offset"], length=record["length"])
        if _digest(payload) != record["sha256"]:
            raise CatalogError("Skill content integrity check failed.", "catalog_integrity")
        content = _json(payload)
        text = content.get("text")
        if (
            content.get("id") != canonical_id or not isinstance(text, str)
            or len(text.encode("utf-8")) > MAX_SKILL_BYTES or "\x00" in text
            or content.get("sha256") != _digest(text.encode("utf-8"))
        ):
            raise CatalogError("Skill content record is invalid.", "catalog_integrity")
        return {
            "id": skill_id, "canonical_id": canonical_id,
            "name": skill["name"], "description": skill["description"],
            "content": text, "digest": _digest(text.encode("utf-8")), "version": self.version,
            "catalog_digest": self.digest, "authority": "untrusted", "source": "agentic-awesome-skills",
        }


def search(q: str = "", limit: int = 30, *, root: str | Path | None = None) -> dict[str, Any]:
    return Catalog(root).search(q, limit)


def get_skill(skill_id: str, *, root: str | Path | None = None) -> dict[str, Any]:
    return Catalog(root).get_skill(skill_id)
