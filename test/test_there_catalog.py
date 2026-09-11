from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from open_webui.there_integration import catalog


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return "sha256-" + hashlib.sha256(value).hexdigest()


class CatalogFixture:
    def __init__(self, root):
        self.root = root
        self.text = "---\nname: postgres-review\n---\nReview PostgreSQL tables.\nDo not execute catalog scripts.\n"
        self.version = "16.9.1"
        self.skills = [
            {"id": "postgres-review", "name": "Postgres Review", "description": "Review PostgreSQL tables.", "category": "database", "tags": ["sql"], "triggers": ["postgres"], "path": "skills/postgres-review/SKILL.md"},
            {"id": "python/testing", "name": "Python Testing", "description": "Write Python tests.", "category": "testing", "tags": ["pytest"], "triggers": ["unittest"], "path": "skills/python/testing/SKILL.md"},
        ]
        self.content = encode({"id": "postgres-review", "text": self.text, "sha256": digest(self.text.encode())}) + b"\n"
        self.index = {"schemaVersion": 1, "entries": {"postgres-review": {"offset": 0, "length": len(self.content), "sha256": digest(self.content), "files": []}}}
        self.payloads = {
            catalog.CATALOG_PATH: encode({"skills": self.skills, "total": len(self.skills)}),
            catalog.INDEX_PATH: encode(self.index),
            catalog.CONTENT_PATH: self.content,
        }
        self.publish()

    def publish(self):
        (self.root / "data" / "aas-v1").mkdir(parents=True, exist_ok=True)
        assets = []
        for name, payload in self.payloads.items():
            (self.root / name).write_bytes(payload)
            assets.append({"path": name, "size": len(payload), "sha256": digest(payload)})
        self.digest = digest(encode({"digestVersion": 1, "assets": assets}))
        self.manifest = {
            "package": "agentic-awesome-skills", "packageVersion": self.version,
            "catalogDigest": self.digest, "digestVersion": 1, "skillCount": len(self.skills), "assets": assets,
        }
        (self.root / "package.json").write_bytes(encode({"name": "agentic-awesome-skills", "version": self.version}))
        (self.root / "data/aas-v1/catalog-manifest.v1.json").write_bytes(encode(self.manifest))

    def load(self):
        return catalog.Catalog(self.root, expected_version=self.version, expected_digest=self.digest)


@pytest.fixture
def fixture(tmp_path):
    return CatalogFixture(tmp_path / "release")


def test_search_reads_metadata_in_stable_order_without_content(fixture):
    reader = fixture.load()
    (fixture.root / catalog.CONTENT_PATH).unlink()
    result = reader.search("", 1)
    assert result["total"] == 2
    assert result["items"][0]["id"] == "postgres-review"
    assert "content" not in result["items"][0]
    assert reader.search("PYTHON tests")["items"][0]["id"] == "python/testing"
    assert reader.search("postgres missing")["total"] == 0
    assert reader.search("unittest")["total"] == 1


def test_get_skill_verifies_content_and_returns_review_digest(fixture):
    item = fixture.load().get_skill("postgres-review")
    assert item["content"] == fixture.text
    assert item["digest"] == digest(fixture.text.encode())
    assert item["version"] == fixture.version
    assert item["catalog_digest"] == fixture.digest
    assert item["authority"] == "untrusted"
    assert item["source"] == "agentic-awesome-skills"


def test_discovery_id_uses_authenticated_canonical_content_reference(fixture):
    # Exact upstream v16.9.1 shape: discovery entry agent-squad/alex, but
    # content-index.entries.alex and the NDJSON record both name canonical alex.
    fixture.skills[0].update({"id": "agent-squad/alex", "canonical_id": "alex", "name": "Alex", "path": "skills/agent-squad/alex/SKILL.md"})
    payload = encode({"id": "alex", "text": fixture.text, "sha256": digest(fixture.text.encode())}) + b"\n"
    fixture.index["entries"] = {"alex": {"offset": 0, "length": len(payload), "sha256": digest(payload), "files": []}}
    fixture.payloads[catalog.CATALOG_PATH] = encode({"skills": fixture.skills, "total": len(fixture.skills)})
    fixture.payloads[catalog.INDEX_PATH] = encode(fixture.index)
    fixture.payloads[catalog.CONTENT_PATH] = payload
    fixture.publish()
    reader = fixture.load()
    result = reader.get_skill("agent-squad/alex")
    assert result["id"] == "agent-squad/alex"
    assert result["canonical_id"] == "alex"
    assert result["content"] == fixture.text
    assert reader.search("alex")["items"][0]["id"] == "agent-squad/alex"
    # The browser cannot bypass discovery membership by requesting an index key.
    with pytest.raises(catalog.CatalogError) as failure:
        reader.get_skill("alex")
    assert failure.value.code == "skill_not_found"


@pytest.mark.parametrize("canonical_id", ["../outside", "/etc/passwd", "C:/secret", "a\\b", "x" * 257, 123, [], None])
def test_canonical_id_must_be_safe_even_in_a_self_consistent_fixture(fixture, canonical_id):
    fixture.skills[0]["canonical_id"] = canonical_id
    fixture.payloads[catalog.CATALOG_PATH] = encode({"skills": fixture.skills, "total": len(fixture.skills)})
    fixture.publish()
    with pytest.raises(catalog.CatalogError, match="canonical skill id"):
        fixture.load()


def test_verified_index_is_cached_per_instance_without_accepting_later_tampering(fixture, monkeypatch):
    reader = fixture.load()
    calls = []
    original = reader._asset

    def tracked_asset(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(reader, "_asset", tracked_asset)
    first = reader.get_skill("postgres-review")
    (fixture.root / catalog.INDEX_PATH).write_bytes(b"{}")
    # The old instance retains the verified index, not the newly modified file.
    assert reader.get_skill("postgres-review") == first
    assert calls.count(catalog.INDEX_PATH) == 1
    # A new instance is not allowed to trust another instance's cached identity.
    with pytest.raises(catalog.CatalogError) as failure:
        fixture.load().get_skill("postgres-review")
    assert failure.value.code == "catalog_integrity"


@pytest.mark.parametrize("value", ["../secret", "/etc/passwd", "a/../b", "C:/secret", "a\\b", "a\x00b", "x" * 257])
def test_skill_id_never_becomes_a_user_controlled_path(fixture, value):
    with pytest.raises(ValueError):
        fixture.load().get_skill(value)


@pytest.mark.parametrize("query,limit", [("x" * 257, 2), (None, 2), ("", 0), ("", 101), ("", True)])
def test_query_bounds(fixture, query, limit):
    with pytest.raises(ValueError):
        fixture.load().search(query, limit)


def test_unknown_skill_has_fixed_error(fixture):
    with pytest.raises(catalog.CatalogError) as failure:
        fixture.load().get_skill("not-found")
    assert failure.value.code == "skill_not_found"


def test_pinned_identity_cannot_be_replaced_with_self_consistent_manifest(fixture):
    with pytest.raises(catalog.CatalogError) as failure:
        catalog.Catalog(fixture.root)
    assert failure.value.code == "catalog_integrity"


def test_catalog_tamper_fails_before_search(fixture):
    (fixture.root / catalog.CATALOG_PATH).write_bytes(b"{}")
    with pytest.raises(catalog.CatalogError) as failure:
        fixture.load()
    assert failure.value.code == "catalog_integrity"


def test_manifest_asset_rewrite_does_not_bypass_pinned_digest(fixture):
    fixture.manifest["assets"][0]["sha256"] = "sha256-" + "a" * 64
    (fixture.root / "data/aas-v1/catalog-manifest.v1.json").write_bytes(encode(fixture.manifest))
    with pytest.raises(catalog.CatalogError, match="manifest integrity"):
        fixture.load()


@pytest.mark.parametrize("asset", [catalog.INDEX_PATH, catalog.CONTENT_PATH])
def test_content_and_index_tampering_are_detected(fixture, asset):
    reader = fixture.load()
    target = fixture.root / asset
    payload = bytearray(target.read_bytes())
    payload[10] ^= 1
    target.write_bytes(payload)
    with pytest.raises(catalog.CatalogError) as failure:
        reader.get_skill("postgres-review")
    assert failure.value.code == "catalog_integrity"


@pytest.mark.parametrize("replacement", [{"id": "another"}, {"sha256": "sha256-" + "f" * 64}, {"text": "\u0000"}])
def test_content_record_is_bound_to_skill_and_text_hash(fixture, replacement):
    record = {"id": "postgres-review", "text": fixture.text, "sha256": digest(fixture.text.encode())}
    record.update(replacement)
    payload = encode(record) + b"\n"
    fixture.index["entries"]["postgres-review"].update({"length": len(payload), "sha256": digest(payload)})
    fixture.payloads[catalog.CONTENT_PATH] = payload
    fixture.payloads[catalog.INDEX_PATH] = encode(fixture.index)
    fixture.publish()
    with pytest.raises(catalog.CatalogError) as failure:
        fixture.load().get_skill("postgres-review")
    assert failure.value.code == "catalog_integrity"


@pytest.mark.parametrize("offset,length", [(-1, 2), (0, 0), (0, catalog.MAX_SKILL_BYTES + 1), (True, 2)])
def test_invalid_or_oversized_content_range_is_rejected(fixture, offset, length):
    fixture.index["entries"]["postgres-review"].update({"offset": offset, "length": length})
    fixture.payloads[catalog.INDEX_PATH] = encode(fixture.index)
    fixture.publish()
    with pytest.raises(catalog.CatalogError, match="reference is invalid"):
        fixture.load().get_skill("postgres-review")


def test_catalog_metadata_cannot_name_traversal_path(fixture):
    fixture.skills[0]["path"] = "../secret"
    fixture.payloads[catalog.CATALOG_PATH] = encode({"skills": fixture.skills, "total": len(fixture.skills)})
    fixture.publish()
    with pytest.raises(catalog.CatalogError) as failure:
        fixture.load()
    assert failure.value.code == "catalog_unsafe"


def test_hard_linked_asset_is_rejected(fixture, tmp_path):
    asset = fixture.root / catalog.CATALOG_PATH
    os.link(asset, tmp_path / "hard-link.json")
    with pytest.raises(catalog.CatalogError) as failure:
        fixture.load()
    assert failure.value.code == "catalog_unsafe"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink privilege is not assumed; production Linux checks every openat component")
@pytest.mark.parametrize("directory_link", [False, True])
def test_symlink_escape_is_rejected_even_with_matching_bytes(fixture, tmp_path, directory_link):
    if directory_link:
        original = fixture.root / "data"
        outside = tmp_path / "outside-data"
        original.rename(outside)
        original.symlink_to(outside, target_is_directory=True)
    else:
        asset = fixture.root / catalog.CATALOG_PATH
        outside = tmp_path / "outside.json"
        outside.write_bytes(asset.read_bytes())
        asset.unlink()
        asset.symlink_to(outside)
    with pytest.raises(catalog.CatalogError) as failure:
        fixture.load()
    assert failure.value.code == "catalog_unsafe"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink privilege is not assumed")
def test_release_current_symlink_is_allowed(tmp_path):
    fixture = CatalogFixture(tmp_path / "release")
    current = tmp_path / "current"
    current.symlink_to(fixture.root, target_is_directory=True)
    assert catalog.Catalog(current, expected_digest=fixture.digest).search()["total"] == 2


def test_missing_root_error_does_not_leak_local_path(tmp_path):
    secret_path = tmp_path / "private-tenant-4821"
    with pytest.raises(catalog.CatalogError) as failure:
        catalog.Catalog(secret_path)
    assert str(secret_path) not in str(failure.value)
