"""Fathom-sourced evidence storage (Section 11.5; FR-030; Prompt 13).

Mirrors `project_context.services.calendar_ingestion` closely — same
parser registry, same content-hash dedup, same immutable-version
storage, same "match provenance persisted, only a bounded portion of
the stored text is ever chunked for extraction" shape. Two deliberate
differences from every other connector's ingestion module:

- **Primary vs. secondary evidence** (Prompt 13: "transcript ...
  along with meeting metadata and action items/summary only as
  secondary source evidence"). The stored `normalized_text` always
  contains all three sections — metadata header, transcript, and
  Fathom's own summary/action items — so every part is visible and
  quotable in the evidence viewer. Only the **transcript** section is
  ever chunked and handed to extraction; the metadata header and the
  summary/action-items section never are, for two independent reasons:
  (1) the header is pure metadata, exactly Calendar's "must not create
  a decision merely because a meeting exists" concern, and (2) Fathom's
  own summary/action items are its own derived interpretation of the
  same transcript already being extracted — sending both would let one
  real conversation manufacture two independent sets of proposals for
  the same fact, and would let a Fathom-generated action item become an
  extracted `commitment` observation as if the transcript itself said
  so. This is what makes FR's "Fathom-produced action items are source
  evidence, not automatically accepted ledger commitments" true by
  construction rather than by a special-cased reconciliation rule: they
  are stored, cited, and human-readable, but never enter the
  extraction/reconciliation pipeline that could turn them into a
  ledger item at all. A person reading the evidence card can still see
  them and manually create/correct a ledger item from what they read.
- **No separate cue-timestamp field.** Unlike the VTT parser (which
  keeps a cue's timestamp in `TextBlock.location_label`), each
  synthesized transcript turn embeds its `[HH:MM:SS]` timestamp
  directly in the paragraph text itself (see `_merge_transcript_turns`)
  — a deliberate simplification: Fathom's transcript items are not VTT
  cues, and reusing the plain-paragraph TXT parser (rather than forcing
  this into a synthesized WEBVTT document) keeps this module a plain,
  auditable string builder instead of a lossy WEBVTT round-trip. The
  timestamp is still fully present and citable, just inline in the
  quoted text rather than in separate location metadata.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_context.chunking import chunk_blocks
from project_context.connectors.protocol import ArtifactMetadata, RawArtifact
from project_context.db import evidence_repository
from project_context.domain.evidence import (
    ArtifactAvailability,
    AssignmentMethod,
    SourceArtifact,
    SourceChunk,
    SourceContent,
)
from project_context.domain.fathom_matching import RULE_TIER_SCHEDULED_EVENT
from project_context.evidence_store import store_bytes
from project_context.parsers import MIME_TYPES, EvidenceKind, detect_kind, parse

_SECONDARY_HEADING = "Fathom-generated summary/action items (secondary source evidence)"


@dataclass(frozen=True)
class FathomStoreResult:
    artifact: SourceArtifact
    content: SourceContent
    chunks: tuple[SourceChunk, ...]
    created_new_version: bool


def get_or_create_fathom_artifact(
    conn: sqlite3.Connection, project_id: str, source_id: str, metadata: ArtifactMetadata
) -> SourceArtifact:
    """Look up this meeting by its real Fathom `recording_id` (Prompt
    13: "Treat `recording_id` as the stable external identity"),
    creating it — with its match provenance — on first sight. Never
    touches content.

    A meeting whose *only* matching tier is `scheduled_event` (a
    meeting-URL or bounded-time-window coincidence with no participant/
    domain corroboration — see `domain.fathom_matching`'s module
    docstring for why that tier alone is never trusted) is stored as
    `ArtifactAvailability.UNASSIGNED` instead of `AVAILABLE` — Prompt
    13: "Ambiguity goes to unassigned/manual review." It is still a
    real, evidence-linked artifact a person can review and confirm from
    the Unassigned Evidence view; it is just never treated as
    automatically belonging to this project's ledger.

    Deliberately does **not** reuse `match_rule` (unlike
    `calendar_ingestion.get_or_create_calendar_artifact`): that column's
    CHECK constraint (migrations/0010) only allows Calendar's own four
    tier names, and this prompt does not touch that migration to widen
    it. The full tier name is still fully recorded — just folded into
    the unconstrained `match_reason` text as `"{tier}: {reason}"`
    instead of split across the two columns. `match_rule` stays `NULL`
    for every Fathom artifact."""
    existing = evidence_repository.get_artifact_by_external_id(
        conn, project_id, source_id, metadata.external_id
    )
    if existing is not None:
        return existing
    tier = metadata.extra.get("match_rule")
    reason = metadata.extra.get("match_reason")
    availability = (
        ArtifactAvailability.UNASSIGNED
        if tier == RULE_TIER_SCHEDULED_EVENT
        else ArtifactAvailability.AVAILABLE
    )
    return evidence_repository.insert_artifact(
        conn,
        project_id,
        source_id,
        external_id=metadata.external_id,
        artifact_type=metadata.artifact_type,
        title=metadata.title,
        author=metadata.author,
        occurred_at=metadata.occurred_at,
        external_url=metadata.external_url,
        source_type=metadata.source_type,
        assignment_method=AssignmentMethod.BOUNDARY_MATCH,
        match_reason=f"{tier}: {reason}" if tier else reason,
        availability=availability,
    )


def _distinct_speaker_names(transcript_items: list[dict[str, Any]]) -> list[str]:
    """Normalized "speakers" list (Prompt 13: "Normalize ... speakers"),
    in first-seen order — distinct from `calendar_invitees`, since a
    transcript speaker is not guaranteed to match an invited attendee
    (a dial-in guest, a name Fathom could not resolve to an invitee)."""
    seen: dict[str, None] = {}
    for item in transcript_items:
        name = (item.get("speaker") or {}).get("display_name")
        if name and name not in seen:
            seen[name] = None
    return list(seen)


def _merge_transcript_turns(transcript_items: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Adjacent same-speaker transcript entries merged into one turn
    (Prompt 13: "normalize ... transcript turns/timestamps" — the same
    "merge adjacent cues from the same speaker" rule Section 8 already
    applies to VTT). Returns `(timestamp, speaker, text)` — `timestamp`
    is the *first* entry's own timestamp, kept even as later entries
    merge in."""
    turns: list[list[str]] = []
    for item in transcript_items:
        speaker = (item.get("speaker") or {}).get("display_name") or "(unknown speaker)"
        text = (item.get("text") or "").strip()
        if not text:
            continue
        timestamp = item.get("timestamp") or ""
        if turns and turns[-1][1] == speaker:
            turns[-1][2] = f"{turns[-1][2]} {text}"
        else:
            turns.append([timestamp, speaker, text])
    return [(t[0], t[1], t[2]) for t in turns]


def _build_header_lines(meeting: dict[str, Any], artifact: ArtifactMetadata) -> list[str]:
    recorded_by = meeting.get("recorded_by") or {}
    lines = [
        f"Title: {artifact.title}",
        f"Recording ID: {meeting.get('recording_id')}",
    ]
    if meeting.get("meeting_type"):
        lines.append(f"Meeting type: {meeting['meeting_type']}")
    if recorded_by.get("email"):
        team = f" ({recorded_by['team']})" if recorded_by.get("team") else ""
        lines.append(f"Recorded by: {recorded_by.get('name') or ''} <{recorded_by['email']}>{team}")
    if meeting.get("meeting_url"):
        lines.append(f"Meeting URL: {meeting['meeting_url']}")
    if meeting.get("share_url"):
        lines.append(f"Share URL: {meeting['share_url']}")
    if meeting.get("url"):
        lines.append(f"Playback URL: {meeting['url']}")
    created = meeting.get("created_at")
    if created:
        lines.append(f"Created: {created}")
    scheduled = meeting.get("scheduled_start_time")
    if scheduled:
        lines.append(f"Scheduled start: {scheduled}")
    recorded = meeting.get("recording_start_time")
    if recorded:
        lines.append(f"Recording start: {recorded}")
    invitees = meeting.get("calendar_invitees") or []
    if invitees:
        rendered = ", ".join(
            f"{i.get('name') or '(unknown)'} <{i['email']}>" for i in invitees if i.get("email")
        )
        if rendered:
            lines.append(f"Calendar invitees: {rendered}")
    speaker_names = _distinct_speaker_names(meeting.get("transcript") or [])
    if speaker_names:
        lines.append(f"Speakers: {', '.join(speaker_names)}")
    if meeting.get("transcript_language"):
        lines.append(f"Transcript language: {meeting['transcript_language']}")
    match_reason = artifact.extra.get("match_reason")
    match_rule = artifact.extra.get("match_rule")
    if match_reason:
        lines.append(f"Match reason: {match_rule}: {match_reason}" if match_rule else match_reason)
    return lines


def _build_secondary_section(meeting: dict[str, Any]) -> str:
    paragraphs: list[str] = []
    summary = (meeting.get("default_summary") or {}).get("markdown_formatted")
    if summary and summary.strip():
        paragraphs.append(f"{_SECONDARY_HEADING} — summary:\n{summary.strip()}")
    action_items = meeting.get("action_items") or []
    if action_items:
        lines = [f"{_SECONDARY_HEADING} — action items:"]
        for item in action_items:
            description = (item.get("description") or "").strip()
            if not description:
                continue
            bits = []
            assignee = (item.get("assignee") or {}).get("name")
            if assignee:
                bits.append(f"assignee: {assignee}")
            if item.get("recording_timestamp"):
                bits.append(f"at {item['recording_timestamp']}")
            if item.get("completed"):
                bits.append("marked complete by Fathom")
            suffix = f" ({', '.join(bits)})" if bits else ""
            lines.append(f"- {description}{suffix}")
        if len(lines) > 1:
            paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs)


def build_normalized_meeting_text(
    meeting: dict[str, Any], artifact: ArtifactMetadata
) -> tuple[str, int, int]:
    """Build the complete stored evidence text and return
    `(text, transcript_start, transcript_end)` — the exact character
    range of the transcript-turns section, so `store_fathom_artifact`
    can select only the blocks inside it for chunking. `transcript_start
    == transcript_end` (an empty range, right after the header) when
    this meeting has no transcript yet (Section 11.5: "missing
    transcript/post-processing pending") — metadata is still stored and
    visible; nothing is chunked for extraction, and nothing is treated
    as a failure. A later sync's overlap rescan re-fetches this same
    meeting; once Fathom's processing finishes, `_version_marker`
    changes and a new content version is created and chunked normally."""
    header_block = "\n".join(_build_header_lines(meeting, artifact))

    turns = _merge_transcript_turns(meeting.get("transcript") or [])
    transcript_section = "\n\n".join(
        f"[{timestamp}] {speaker}: {text}" for timestamp, speaker, text in turns
    )
    secondary_section = _build_secondary_section(meeting)

    parts = [header_block]
    cursor = len(header_block)
    if transcript_section:
        cursor += 2
        transcript_start = cursor
        parts.append(transcript_section)
        cursor += len(transcript_section)
        transcript_end = cursor
    else:
        transcript_start = transcript_end = cursor

    if secondary_section:
        cursor += 2
        parts.append(secondary_section)
        cursor += len(secondary_section)

    return "\n\n".join(parts), transcript_start, transcript_end


def store_fathom_artifact(
    conn: sqlite3.Connection,
    project_id: str,
    artifact: SourceArtifact,
    raw: RawArtifact,
    *,
    evidence_dir: Path,
    chunk_target_chars: int,
    chunk_overlap_ratio: float,
) -> FathomStoreResult:
    """Parse, chunk (transcript section only), and store one freshly
    built Fathom meeting as a new content version."""
    filename = raw.filename or f"fathom-{raw.metadata.external_id}.txt"
    kind = detect_kind(filename=filename, data=raw.data)
    assert kind is EvidenceKind.TXT, "Fathom meetings are always synthesized as plain text"
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
        return FathomStoreResult(
            artifact=artifact,
            content=existing_by_hash,
            chunks=tuple(existing_chunks),
            created_new_version=False,
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

    chunks: list[SourceChunk] = []
    meeting = raw.metadata.extra.get("meeting", {})
    _full_text, transcript_start, transcript_end = build_normalized_meeting_text(
        meeting, raw.metadata
    )
    if parse_result.blocks and transcript_end > transcript_start:
        visible_blocks = [
            block
            for block in parse_result.blocks
            if block.char_start >= transcript_start and block.char_end <= transcript_end
        ]
        if visible_blocks:
            chunk_specs = chunk_blocks(
                visible_blocks, target_chars=chunk_target_chars, overlap_ratio=chunk_overlap_ratio
            )
            chunks = evidence_repository.insert_chunks(conn, project_id, content.id, chunk_specs)

    return FathomStoreResult(
        artifact=artifact, content=content, chunks=tuple(chunks), created_new_version=True
    )
