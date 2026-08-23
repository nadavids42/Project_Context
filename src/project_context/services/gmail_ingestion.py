"""Gmail-sourced evidence storage (Section 11.3; FR-028; Prompt 11).

Mirrors `project_context.services.drive_ingestion` closely — same
parser registry, same content-hash dedup, same immutable-version
storage — with exactly one deliberate difference: **what gets chunked
for extraction is limited to the text before the conservatively
detected quoted-history/signature boundary** (Prompt 11: "Trim obvious
quoted history/signatures conservatively for the extraction view while
retaining the complete imported normalized body as evidence/versioned
content"). The stored `normalized_text` — what the evidence viewer
shows and what future imports get compared against — is always the
complete message; only the *set of blocks handed to the chunker* is
narrowed. Because the quote boundary is found within the same text the
TXT parser already produced offsets for, no block's `char_start`/
`char_end` needs to be recomputed — a block is simply included or
excluded based on whether it lies entirely before the boundary, so
every emitted chunk's offsets still resolve correctly against the
stored content (FR-008).

This also directly serves Section 11.3's "do not let one long thread
generate every historical commitment again" — quoted prior messages
inside a reply are visible as evidence but are never sent to
extraction a second time.

`get_or_create_gmail_artifact` is not redefined here: identity/lookup
by `(source_id, external_id)` has no Gmail-specific behavior, so the
Drive-named function is reused directly (see its own docstring — nothing
about it is actually Drive-specific)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from project_context.chunking import chunk_blocks
from project_context.connectors.protocol import RawArtifact
from project_context.db import evidence_repository
from project_context.domain.email_normalization import find_quote_boundary
from project_context.domain.evidence import SourceArtifact
from project_context.evidence_store import store_bytes
from project_context.parsers import MIME_TYPES, EvidenceKind, ParseResult, detect_kind, parse
from project_context.services.drive_ingestion import DriveStoreResult
from project_context.services.drive_ingestion import (
    get_or_create_drive_artifact as get_or_create_gmail_artifact,
)

__all__ = ["GmailStoreResult", "get_or_create_gmail_artifact", "store_gmail_artifact"]

#: `DriveStoreResult` is a plain, connector-agnostic result shape (an
#: artifact/content/chunks tuple plus a "created new version" flag) —
#: reused here under a clearer local name rather than duplicated.
GmailStoreResult = DriveStoreResult


def _visible_blocks(parse_result: ParseResult, *, header_block_end: int, body_text: str):
    """Every block that starts before the quote boundary. The header
    block(s) — Subject/From/.../Message-ID, always at the very top of
    the normalized text — are never trimmed; only blocks inside the
    body (i.e. starting at or after `header_block_end`) are subject to
    the boundary. A block straddling the boundary is dropped whole
    rather than sliced — conservative, and it keeps every retained
    block's offsets exactly as the parser produced them."""
    quote_boundary_in_body = find_quote_boundary(body_text)
    visible_end = header_block_end + quote_boundary_in_body
    return [block for block in parse_result.blocks if block.char_end <= visible_end]


def store_gmail_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    artifact: SourceArtifact,
    raw: RawArtifact,
    *,
    evidence_dir: Path,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
) -> GmailStoreResult:
    """Parse, chunk (extraction-visible portion only), and store one
    freshly fetched Gmail message as a new content version. `raw.data`
    is always the connector's complete normalized text (header block +
    full body, including any quoted history) — see
    `project_context.connectors.gmail.GmailConnector.fetch` — so the
    stored `normalized_text` is always the complete message regardless
    of how much gets chunked."""
    filename = raw.filename or f"gmail-{raw.metadata.external_id}.txt"
    kind = detect_kind(filename=filename, data=raw.data)
    assert kind is EvidenceKind.TXT, "Gmail messages are always synthesized as plain text"
    mime_type = MIME_TYPES[kind]
    parse_result = parse(kind, raw.data)

    sha256, storage_path = store_bytes(evidence_dir, raw.data)
    existing_by_hash = evidence_repository.get_content_by_sha256(conn, artifact.id, sha256)
    if existing_by_hash is not None:
        if artifact.current_content_id != existing_by_hash.id:
            evidence_repository.set_current_content(
                conn, project_id, artifact.id, existing_by_hash.id
            )
        existing_chunks = evidence_repository.list_chunks_for_content(
            conn, project_id, existing_by_hash.id
        )
        return GmailStoreResult(
            artifact=artifact, content=existing_by_hash,
            chunks=tuple(existing_chunks), created_new_version=False,
        )

    try:
        relative_storage_path = str(storage_path.relative_to(evidence_dir.resolve()))
    except ValueError:
        relative_storage_path = str(storage_path)

    content = evidence_repository.insert_content(
        conn,
        project_id,
        artifact.id,
        sha256=sha256,
        raw_storage_path=relative_storage_path,
        mime_type=mime_type,
        byte_size=len(raw.data),
        normalized_text=parse_result.normalized_text or None,
        parser_name=parse_result.parser_name,
        parser_version=parse_result.parser_version,
        parse_status=parse_result.status,
        location_map=parse_result.location_map(),
        original_filename=filename,
        version_key=raw.metadata.version_marker,
    )
    evidence_repository.set_current_content(conn, project_id, artifact.id, content.id)

    chunks = []
    if parse_result.blocks:
        full_text = parse_result.normalized_text
        # The header block is always the text before the first blank
        # line (see `email_normalization.build_normalized_email_text`);
        # everything after it is the body the quote boundary applies to.
        header_end = full_text.find("\n\n")
        header_end = header_end + 2 if header_end != -1 else 0
        body_text = full_text[header_end:]
        visible_blocks = _visible_blocks(
            parse_result, header_block_end=header_end, body_text=body_text
        )
        if visible_blocks:
            chunk_specs = chunk_blocks(
                visible_blocks, target_chars=chunk_target_chars, overlap_ratio=chunk_overlap_ratio
            )
            chunks = evidence_repository.insert_chunks(conn, project_id, content.id, chunk_specs)

    return GmailStoreResult(
        artifact=artifact, content=content, chunks=tuple(chunks), created_new_version=True
    )
