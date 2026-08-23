"""Tests for content-addressed evidence storage: SHA-256 dedup, atomic
writes, and path-escape prevention (Section 15/16)."""

from __future__ import annotations

import pytest

from project_context.evidence_store import (
    UnsafeEvidencePathError,
    content_exists,
    content_path,
    read_bytes,
    sha256_bytes,
    store_bytes,
)


def test_sha256_bytes_matches_hashlib(tmp_path):
    import hashlib

    data = b"hello world"
    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def test_store_and_read_round_trip(tmp_path):
    sha256, path = store_bytes(tmp_path, b"hello world")

    assert path.exists()
    assert path.read_bytes() == b"hello world"
    assert read_bytes(tmp_path, sha256) == b"hello world"


def test_identical_content_is_deduplicated_to_the_same_path(tmp_path):
    sha1, path1 = store_bytes(tmp_path, b"identical content")
    sha2, path2 = store_bytes(tmp_path, b"identical content")

    assert sha1 == sha2
    assert path1 == path2


def test_different_content_is_stored_at_different_paths(tmp_path):
    _, path1 = store_bytes(tmp_path, b"content A")
    _, path2 = store_bytes(tmp_path, b"content B")

    assert path1 != path2


def test_store_does_not_rewrite_existing_content(tmp_path):
    sha256, path = store_bytes(tmp_path, b"original")
    mtime_before = path.stat().st_mtime_ns

    store_bytes(tmp_path, b"original")

    assert path.stat().st_mtime_ns == mtime_before


def test_store_leaves_no_temp_files_behind(tmp_path):
    store_bytes(tmp_path, b"some content")

    leftover_temp_files = list(tmp_path.rglob(".tmp-*"))
    assert leftover_temp_files == []


def test_content_exists_reflects_storage_state(tmp_path):
    sha256, _ = store_bytes(tmp_path, b"exists now")
    missing_sha = "0" * 64

    assert content_exists(tmp_path, sha256) is True
    assert content_exists(tmp_path, missing_sha) is False


def test_content_path_rejects_short_string(tmp_path):
    with pytest.raises(UnsafeEvidencePathError):
        content_path(tmp_path, "short")


def test_content_path_rejects_traversal_like_string(tmp_path):
    with pytest.raises(UnsafeEvidencePathError):
        content_path(tmp_path, "../../../../etc/passwd" + "0" * 40)


def test_content_path_rejects_uppercase_hex(tmp_path):
    with pytest.raises(UnsafeEvidencePathError):
        content_path(tmp_path, "A" * 64)


def test_content_path_rejects_non_hex_characters(tmp_path):
    with pytest.raises(UnsafeEvidencePathError):
        content_path(tmp_path, "g" * 64)


def test_content_path_resolves_inside_evidence_dir(tmp_path):
    sha256 = "a" * 64
    path = content_path(tmp_path, sha256)

    assert path.is_relative_to(tmp_path.resolve())


def test_read_bytes_rejects_unsafe_digest(tmp_path):
    with pytest.raises(UnsafeEvidencePathError):
        read_bytes(tmp_path, "not-a-real-digest")


def test_store_creates_sharded_directory_structure(tmp_path):
    sha256, path = store_bytes(tmp_path, b"shard test")

    assert path.parent.name == sha256[:2]
    assert path.parent.parent.name == "objects"
