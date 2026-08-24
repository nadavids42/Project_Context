"""Shared authoring helpers for the three corpus builders
(``project_context.evaluation.corpus_data.implementation``/``advisory``/
``launch``).

The one property every corpus artifact with material content must
satisfy: after real parsing, each authored "block" (a VTT turn, a
Markdown/TXT paragraph) becomes **exactly one chunk** — the same trick
``tests/golden_projects/golden_fixtures.py`` uses (see that module's
docstring), scaled up and made self-verifying instead of hand-tuned. This
is what lets fake/deterministic-mode extraction be scripted response-per-
chunk: ``project_context.evaluation.reviewer_protocol``'s fake extraction
path assumes one authored fact per chunk, in order.

``pad`` guarantees every block is at least ``_PAD_MIN_LEN`` characters
(so no two adjacent short blocks could ever be mistaken for fitting in
one chunk); ``choose_chunk_target_chars`` then computes, from the actual
authored block lengths, a target strictly between the longest single
block and the shortest adjacent-pair sum — the same relationship
``tests/golden_projects/golden_fixtures.py`` documents for its own fixed
constant, computed here instead of hand-verified, so a future edit to any
project's wording can never silently violate it.
"""

from __future__ import annotations

_PAD_MIN_LEN = 110
_PAD_SUFFIX = " This was noted for the project record."


def pad(statement: str, *, min_len: int = _PAD_MIN_LEN) -> str:
    """Pad `statement` up to at least `min_len` characters by appending a
    fixed, meaning-free suffix — never truncates, never changes the
    substring a ground-truth transition cites (the padded text always
    starts with the exact, unpadded `statement`)."""
    if len(statement) >= min_len:
        return statement
    return statement + _PAD_SUFFIX


def vtt_bytes(turns: list[tuple[str, str]]) -> bytes:
    """Build a minimal, valid WEBVTT transcript, one cue per turn, each
    turn its own `<v Speaker>` voice tag. Callers must alternate speakers
    turn-to-turn (or otherwise ensure no two adjacent turns share a
    speaker) — `project_context.parsers.vtt_parser` merges adjacent
    same-speaker cues into one block, which would silently combine two
    authored facts into one chunk."""
    lines = ["WEBVTT", ""]
    start_seconds = 0
    for speaker, text in turns:
        end_seconds = start_seconds + 2
        start = f"00:{start_seconds // 60:02d}:{start_seconds % 60:02d}.000"
        end = f"00:{end_seconds // 60:02d}:{end_seconds % 60:02d}.000"
        lines.append(f"{start} --> {end}")
        lines.append(f"<v {speaker}>{text}</v>")
        lines.append("")
        start_seconds = end_seconds + 1
    return ("\n".join(lines)).encode("utf-8")


def paragraphs_bytes(paragraphs: list[str]) -> bytes:
    """Build TXT/Markdown bytes from paragraph blocks — blank-line
    separated, exactly what `project_context.parsers.txt_parser` expects
    as one-block-per-paragraph."""
    return ("\n\n".join(paragraphs)).encode("utf-8")


def choose_chunk_target_chars(block_groups: list[list[str]]) -> int:
    """Given every artifact's ordered list of *rendered* block texts
    (VTT: `"Speaker: statement"`; TXT/Markdown: the bare paragraph),
    return a `chunk_target_chars` value that places exactly one block per
    chunk for every group, by construction: strictly greater than the
    longest single block, strictly less than the shortest sum of two
    adjacent blocks within any one group.

    Raises `ValueError` (with the offending lengths) if no such value
    exists — a corpus author's signal to shorten or re-pad a statement,
    exactly like `tests/golden_projects/golden_fixtures.py`'s own
    hand-verified comment describes doing manually.
    """
    max_single = 0
    min_pair: int | None = None
    for group in block_groups:
        for text in group:
            max_single = max(max_single, len(text))
        for left, right in zip(group, group[1:], strict=False):
            pair_len = len(left) + len(right)
            min_pair = pair_len if min_pair is None else min(min_pair, pair_len)

    if min_pair is not None and max_single >= min_pair:
        raise ValueError(
            f"no valid chunk_target_chars exists: longest single block is {max_single} "
            f"chars, shortest adjacent pair is {min_pair} chars (pad/shorten to fix)"
        )
    return max_single + 1
