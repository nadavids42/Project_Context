"""Content-addressed local evidence storage.

Raw bytes are written once per unique SHA-256 digest, under a sharded
path inside the configured evidence directory
(`<evidence_dir>/objects/<sha[:2]>/<sha>`). Multiple artifacts/versions
that happen to share identical bytes share one file on disk; this is
exactly the storage-layer counterpart of `source_contents`'s unique
`(artifact_id, sha256)` dedup (Section 9).

Two things this module is deliberately careful about (Section 16,
"Source-content storage and logs" and Section 15's path-safety
requirement):

- **Paths are never derived from untrusted input.** The only input to
  `content_path()` is a SHA-256 hex digest, which is validated against a
  strict `^[0-9a-f]{64}$` pattern before it touches a path at all —
  original filenames are metadata (stored in the database), never part
  of a filesystem path.
- **Every path is checked to stay inside the evidence directory** before
  any read/write, even though a validated hex digest cannot structurally
  escape it — defense in depth, and directly testable.

Writes are atomic: content is written to a temporary file in the same
shard directory, fsynced, then moved into place with `os.replace`
(atomic on the same filesystem). If the destination already exists
(same content, already stored), the write is skipped entirely.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

#: Shard directory name, kept distinct from any future non-content-
#: addressed storage under the same evidence directory.
_OBJECTS_DIRNAME = "objects"


class UnsafeEvidencePathError(ValueError):
    """Raised when a path would (or, after resolution, does) fall outside
    the configured evidence directory, or a digest fails validation
    before it is ever used to build a path."""


def sha256_bytes(data: bytes) -> str:
    """The lowercase hex SHA-256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def _require_valid_digest(sha256: str) -> str:
    if not _SHA256_HEX_RE.match(sha256):
        raise UnsafeEvidencePathError(f"invalid SHA-256 digest: {sha256!r}")
    return sha256


def content_path(evidence_dir: Path, sha256: str) -> Path:
    """The on-disk path for `sha256`'s content, without touching disk.

    Raises `UnsafeEvidencePathError` if `sha256` is not a well-formed
    64-character hex digest, or if the resulting path would not resolve
    to somewhere inside `evidence_dir`.
    """
    digest = _require_valid_digest(sha256)
    evidence_dir = evidence_dir.resolve()
    path = (evidence_dir / _OBJECTS_DIRNAME / digest[:2] / digest).resolve()
    if not path.is_relative_to(evidence_dir):
        raise UnsafeEvidencePathError(
            f"resolved content path {path} escapes evidence directory {evidence_dir}"
        )
    return path


def store_bytes(evidence_dir: Path, data: bytes) -> tuple[str, Path]:
    """Store `data` content-addressed under `evidence_dir`.

    Returns `(sha256, path)`. If content with this digest is already
    stored, the existing file is left untouched and no write occurs —
    this is the storage-layer half of "identical resubmission is
    detected by hash" (FR-005/FR-006).
    """
    sha256 = sha256_bytes(data)
    path = content_path(evidence_dir, sha256)
    if path.exists():
        return sha256, path

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return sha256, path


def read_bytes(evidence_dir: Path, sha256: str) -> bytes:
    """Read back previously stored content by its digest."""
    return content_path(evidence_dir, sha256).read_bytes()


def content_exists(evidence_dir: Path, sha256: str) -> bool:
    return content_path(evidence_dir, sha256).exists()
