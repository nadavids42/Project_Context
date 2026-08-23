"""Deterministic chunking over parser output.

Chunk boundaries are whole `TextBlock`s (paragraphs, pages, or merged
transcript turns) — a chunk is never split in the middle of one unless
that single block alone exceeds `target_chars`, matching Section 8's
pipeline note: "never split a speaker turn unless oversized." When a
chunk boundary rolls over, a small overlap of trailing blocks (Section
8: "10% overlap only at paragraph/turn boundaries") is carried into the
next chunk so nearby context survives the split.

No model tokenizer dependency: `token_estimate` is a documented,
approximate characters-per-token heuristic (Section 8: "configurable
character/token target without adding a model tokenizer dependency
unless already justified") — good enough for budgeting, never used for
anything requiring exactness.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from project_context.parsers.models import TextBlock

_CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(text: str) -> int:
    """A rough, dependency-free token-count proxy. Not a real tokenizer —
    do not use for anything requiring an exact count."""
    return max(1, len(text) // _CHARS_PER_TOKEN_ESTIMATE)


@dataclass(frozen=True)
class ChunkSpec:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    section_path: str | None
    sha256: str
    token_estimate: int


def _split_oversized_block(block: TextBlock, target_chars: int) -> list[TextBlock]:
    """Hard-split a single block that alone exceeds `target_chars`, at
    whitespace boundaries where possible. The only case chunking ever
    breaks inside a natural boundary — an oversized block has no smaller
    natural boundary to prefer."""
    text = block.text
    base_start = block.char_start
    parts: list[TextBlock] = []
    pos = 0
    length = len(text)
    while pos < length:
        end = min(pos + target_chars, length)
        if end < length:
            split_at = text.rfind(" ", pos, end)
            if split_at > pos:
                end = split_at
        sub_text = text[pos:end]
        if sub_text.strip():
            parts.append(
                TextBlock(
                    text=sub_text,
                    char_start=base_start + pos,
                    char_end=base_start + end,
                    section_path=block.section_path,
                    location_label=block.location_label,
                )
            )
        pos = end
        while pos < length and text[pos] == " ":
            pos += 1
    return parts


def _build_chunk_spec(blocks: Sequence[TextBlock], ordinal: int) -> ChunkSpec:
    text = "\n\n".join(block.text for block in blocks)
    return ChunkSpec(
        ordinal=ordinal,
        text=text,
        char_start=blocks[0].char_start,
        char_end=blocks[-1].char_end,
        section_path=blocks[0].section_path,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        token_estimate=estimate_tokens(text),
    )


def chunk_blocks(
    blocks: Sequence[TextBlock], *, target_chars: int, overlap_ratio: float
) -> list[ChunkSpec]:
    """Group `blocks` into chunks of roughly `target_chars`, never
    splitting a block unless it alone exceeds `target_chars`, with a
    trailing-block overlap carried across each boundary."""
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if not (0.0 <= overlap_ratio < 1.0):
        raise ValueError("overlap_ratio must be in [0.0, 1.0)")
    if not blocks:
        return []

    normalized: list[TextBlock] = []
    for block in blocks:
        if len(block.text) <= target_chars:
            normalized.append(block)
        else:
            normalized.extend(_split_oversized_block(block, target_chars))

    overlap_chars = int(target_chars * overlap_ratio)
    chunks: list[ChunkSpec] = []
    carry: list[TextBlock] = []
    index = 0
    total = len(normalized)

    while index < total:
        current = list(carry)
        current_len = sum(len(block.text) for block in current)
        carry = []
        made_progress = False

        while index < total:
            block = normalized[index]
            if current and current_len + len(block.text) > target_chars:
                break
            current.append(block)
            current_len += len(block.text)
            index += 1
            made_progress = True

        if not made_progress:
            # The carried-over overlap alone already fills the target and
            # the next block still doesn't fit alongside it. Force-add it
            # anyway — guaranteed forward progress beats a marginally
            # over-target chunk.
            block = normalized[index]
            current.append(block)
            current_len += len(block.text)
            index += 1

        chunks.append(_build_chunk_spec(current, len(chunks)))

        if index < total and overlap_chars > 0:
            carry_len = 0
            for block in reversed(current):
                if carry_len + len(block.text) > overlap_chars:
                    break
                carry.insert(0, block)
                carry_len += len(block.text)

    return chunks
