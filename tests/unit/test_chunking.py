"""Tests for deterministic chunking: never split a block unless
oversized, overlap at block boundaries, stable ordinals/char ranges, and
hash/token-estimate fields (Section 8)."""

from __future__ import annotations

import pytest

from project_context.chunking import chunk_blocks, estimate_tokens
from project_context.parsers.models import TextBlock


def _blocks(*texts: str) -> list[TextBlock]:
    blocks = []
    cursor = 0
    for i, text in enumerate(texts):
        blocks.append(
            TextBlock(
                text=text, char_start=cursor, char_end=cursor + len(text), section_path=f"b{i}"
            )
        )
        cursor += len(text) + 1
    return blocks


def test_empty_blocks_produce_no_chunks():
    assert chunk_blocks([], target_chars=100, overlap_ratio=0.0) == []


def test_small_input_produces_one_chunk():
    blocks = _blocks("Hello.", "World.")

    chunks = chunk_blocks(blocks, target_chars=1000, overlap_ratio=0.0)

    assert len(chunks) == 1
    assert chunks[0].text == "Hello.\n\nWorld."
    assert chunks[0].ordinal == 0


def test_chunks_never_split_a_block_unless_it_is_individually_oversized():
    blocks = [
        TextBlock(
            text=f"Paragraph number {i} with a handful of words in it.", char_start=0, char_end=0
        )
        for i in range(20)
    ]
    block_texts = {b.text for b in blocks}

    chunks = chunk_blocks(blocks, target_chars=120, overlap_ratio=0.0)

    assert len(chunks) > 1
    for chunk in chunks:
        for part in chunk.text.split("\n\n"):
            assert part in block_texts, f"chunk contains a non-whole-block fragment: {part!r}"


def test_chunks_respect_target_size_without_overlap():
    blocks = _blocks(*[f"word{i}" for i in range(50)])
    longest_block_len = max(len(b.text) for b in blocks)

    chunks = chunk_blocks(blocks, target_chars=30, overlap_ratio=0.0)

    for chunk in chunks:
        # The target bounds the *sum of block text*, not the joined
        # string (which also includes "\n\n" separators) — compare
        # against the reconstructed block contents, with one block's
        # worth of slack for the "always make forward progress" case.
        block_texts = chunk.text.split("\n\n")
        content_len = sum(len(t) for t in block_texts)
        assert content_len <= 30 + longest_block_len


def test_oversized_single_block_is_hard_split_at_whitespace():
    long_text = " ".join(f"word{i}" for i in range(200))
    block = TextBlock(text=long_text, char_start=0, char_end=len(long_text), section_path="huge")

    chunks = chunk_blocks([block], target_chars=50, overlap_ratio=0.0)

    assert len(chunks) > 1
    reconstructed = " ".join(chunk.text for chunk in chunks)
    assert reconstructed.split() == long_text.split()
    for chunk in chunks:
        assert len(chunk.text) <= 50


def test_overlap_carries_trailing_blocks_into_next_chunk():
    blocks = _blocks(*[f"Blk{i}" for i in range(10)])

    chunks = chunk_blocks(blocks, target_chars=20, overlap_ratio=0.5)

    assert len(chunks) > 1
    first_chunk_blocks = chunks[0].text.split("\n\n")
    second_chunk_blocks = chunks[1].text.split("\n\n")
    overlap = set(first_chunk_blocks) & set(second_chunk_blocks)
    assert overlap, "expected at least one block to be carried over as overlap"


def test_chunk_ordinals_are_sequential_from_zero():
    blocks = _blocks(*[f"Paragraph {i} has some words." for i in range(10)])

    chunks = chunk_blocks(blocks, target_chars=40, overlap_ratio=0.1)

    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_chunk_char_range_matches_first_and_last_included_block():
    blocks = _blocks("Alpha paragraph.", "Beta paragraph.", "Gamma paragraph.")

    chunks = chunk_blocks(blocks, target_chars=1000, overlap_ratio=0.0)

    assert chunks[0].char_start == blocks[0].char_start
    assert chunks[0].char_end == blocks[-1].char_end


def test_chunk_sha256_is_stable_and_content_dependent():
    blocks_a = _blocks("Same text.")
    blocks_b = _blocks("Same text.")
    blocks_c = _blocks("Different text.")

    chunk_a = chunk_blocks(blocks_a, target_chars=1000, overlap_ratio=0.0)[0]
    chunk_b = chunk_blocks(blocks_b, target_chars=1000, overlap_ratio=0.0)[0]
    chunk_c = chunk_blocks(blocks_c, target_chars=1000, overlap_ratio=0.0)[0]

    assert chunk_a.sha256 == chunk_b.sha256
    assert chunk_a.sha256 != chunk_c.sha256
    assert len(chunk_a.sha256) == 64


def test_chunk_token_estimate_is_positive_and_scales_with_length():
    short_chunk = chunk_blocks(_blocks("word"), target_chars=1000, overlap_ratio=0.0)[0]
    long_chunk = chunk_blocks(
        _blocks(" ".join(["word"] * 100)), target_chars=1000, overlap_ratio=0.0
    )[0]

    assert short_chunk.token_estimate >= 1
    assert long_chunk.token_estimate > short_chunk.token_estimate


def test_estimate_tokens_is_at_least_one_for_nonempty_text():
    assert estimate_tokens("a") == 1
    assert estimate_tokens("") == 1


@pytest.mark.parametrize("target_chars", [0, -1])
def test_invalid_target_chars_is_rejected(target_chars):
    with pytest.raises(ValueError, match="target_chars"):
        chunk_blocks(_blocks("x"), target_chars=target_chars, overlap_ratio=0.0)


@pytest.mark.parametrize("overlap_ratio", [-0.1, 1.0, 1.5])
def test_invalid_overlap_ratio_is_rejected(overlap_ratio):
    with pytest.raises(ValueError, match="overlap_ratio"):
        chunk_blocks(_blocks("x"), target_chars=100, overlap_ratio=overlap_ratio)
