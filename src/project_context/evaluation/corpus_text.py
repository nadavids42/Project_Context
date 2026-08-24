"""Turns a `project_context.evaluation.schema.CorpusArtifact`'s raw bytes
into normalized text/blocks using the **real** application parsers
(`project_context.parsers`) — never a hand-rolled re-implementation of
parser behavior. Both the ledger runner (via real ingestion) and the
baseline runner (via this module, reading text directly) therefore see
byte-for-byte identical normalized text for the same artifact, which is
exactly what Section 13.4's "same source text" control requires.
"""

from __future__ import annotations

from dataclasses import dataclass

from project_context.evaluation.schema import ARTIFACT_KIND_MAPPING, ArtifactKind, CorpusArtifact
from project_context.parsers.models import ParseResult, TextBlock
from project_context.parsers.txt_parser import parse_text
from project_context.parsers.vtt_parser import parse_vtt


@dataclass(frozen=True)
class ParsedArtifact:
    text: str
    blocks: tuple[TextBlock, ...]


def parse_artifact(artifact: CorpusArtifact) -> ParsedArtifact:
    """Parse `artifact.raw_bytes` with the same parser real ingestion
    would select for its kind (see `ARTIFACT_KIND_MAPPING`)."""
    _source_type, _suffix, is_markdown = ARTIFACT_KIND_MAPPING[artifact.kind]
    result: ParseResult
    if artifact.kind is ArtifactKind.MEETING_TRANSCRIPT:
        result = parse_vtt(artifact.raw_bytes)
    else:
        result = parse_text(artifact.raw_bytes, markdown=is_markdown)
    if result.normalized_text is None:
        raise ValueError(f"artifact {artifact.artifact_id!r} failed to parse: {result.warnings}")
    return ParsedArtifact(text=result.normalized_text, blocks=result.blocks)


def artifact_text(artifact: CorpusArtifact) -> str:
    """The normalized text baseline reads and ground-truth statements are
    validated against — always derived from `raw_bytes` through the real
    parser, never authored/stored separately."""
    return parse_artifact(artifact).text
