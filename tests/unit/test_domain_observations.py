"""Tests for exact-duplicate observation fingerprinting (Section 10.2
#3; Prompt 6: "exact-source fingerprint deduplication"). Pure domain
logic — no database."""

from __future__ import annotations

from project_context.domain.observations import compute_fingerprint

_SPANS = [("chunk-1", 0, 10)]


def _fp(*, content_id="c1", kind="commitment", statement="Send it.", evidence_spans=_SPANS):
    return compute_fingerprint(
        content_id=content_id, kind=kind, statement=statement, evidence_spans=evidence_spans
    )


def test_identical_inputs_produce_identical_fingerprints():
    assert _fp() == _fp()


def test_different_content_id_changes_the_fingerprint():
    assert _fp(content_id="c1") != _fp(content_id="c2")


def test_different_kind_changes_the_fingerprint():
    assert _fp(kind="commitment") != _fp(kind="risk")


def test_different_statement_changes_the_fingerprint():
    assert _fp(statement="Send it.") != _fp(statement="Send it tomorrow.")


def test_different_evidence_spans_change_the_fingerprint():
    assert _fp(evidence_spans=_SPANS) != _fp(evidence_spans=[("chunk-2", 0, 10)])


def test_whitespace_differences_do_not_change_the_fingerprint():
    assert _fp(statement="Send   it.") == _fp(statement="Send\nit.")


def test_case_differences_do_not_change_the_fingerprint():
    assert _fp(statement="Send It.") == _fp(statement="send it.")


def test_trailing_punctuation_differences_do_not_change_the_fingerprint():
    assert _fp(statement="Send it.") == _fp(statement="Send it")


def test_evidence_span_order_does_not_change_the_fingerprint():
    spans_a = [("chunk-1", 0, 10), ("chunk-1", 20, 30)]
    spans_b = [("chunk-1", 20, 30), ("chunk-1", 0, 10)]
    assert _fp(evidence_spans=spans_a) == _fp(evidence_spans=spans_b)


def test_fingerprint_is_a_hex_sha256_digest():
    fp = _fp()
    assert len(fp) == 64
    int(fp, 16)  # raises ValueError if not valid hex
