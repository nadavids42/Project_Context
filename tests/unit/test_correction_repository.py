"""Tests for the correction repository: retention fields (before/after,
field, reason, model/prompt context, materiality, actor) and the
`error_signature` computation used for the repeated-correction-rate
metric (Prompt 6, FR-016)."""

from __future__ import annotations

import sqlite3

import pytest

from project_context.db import correction_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.review import (
    Correction,
    CorrectionMateriality,
    CorrectionReasonCode,
    CorrectionTargetType,
    compute_error_signature,
)
from project_context.services.projects import create_project


@pytest.fixture
def conn(tmp_path, migrations_dir):
    connection = connect(tmp_path / "app.db")
    run_migrations(connection, migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def project_id(conn):
    return create_project(
        conn, ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")
    ).id


def test_insert_correction_retains_all_fr016_fields(conn, project_id):
    correction = correction_repository.insert_correction(
        conn,
        project_id,
        target_type=CorrectionTargetType.OBSERVATION,
        target_id="obs-1",
        field_name="owner_text",
        reason_code=CorrectionReasonCode.WRONG_OWNER,
        materiality=CorrectionMateriality.MATERIAL,
        original={"owner_text": "Bob"},
        corrected={"owner_text": "Priya"},
        review_id=None,
        model_id="gpt-5.6-terra",
        prompt_version="extraction_v1",
        actor="local-user",
    )

    assert isinstance(correction, Correction)
    assert correction.project_id == project_id
    assert correction.target_type is CorrectionTargetType.OBSERVATION
    assert correction.target_id == "obs-1"
    assert correction.field_name == "owner_text"
    assert correction.original == {"owner_text": "Bob"}
    assert correction.corrected == {"owner_text": "Priya"}
    assert correction.reason_code is CorrectionReasonCode.WRONG_OWNER
    assert correction.materiality is CorrectionMateriality.MATERIAL
    assert correction.model_id == "gpt-5.6-terra"
    assert correction.prompt_version == "extraction_v1"
    assert correction.actor == "local-user"
    assert correction.error_signature == compute_error_signature(
        target_type=CorrectionTargetType.OBSERVATION,
        field_name="owner_text",
        reason_code=CorrectionReasonCode.WRONG_OWNER,
    )


def test_actor_defaults_to_local_user(conn, project_id):
    correction = correction_repository.insert_correction(
        conn,
        project_id,
        target_type=CorrectionTargetType.LEDGER_ITEM,
        target_id="item-1",
        field_name="due_date",
        reason_code=CorrectionReasonCode.WRONG_DATE,
        materiality=CorrectionMateriality.MINOR,
    )
    assert correction.actor == "local-user"


def test_error_signature_is_stable_and_content_free():
    """Same (target_type, field_name, reason_code) always produces the
    same signature, and the signature never contains the corrected
    values themselves — only the classification."""
    sig_1 = compute_error_signature(
        target_type=CorrectionTargetType.OBSERVATION,
        field_name="owner_text",
        reason_code=CorrectionReasonCode.WRONG_OWNER,
    )
    sig_2 = compute_error_signature(
        target_type=CorrectionTargetType.OBSERVATION,
        field_name="owner_text",
        reason_code=CorrectionReasonCode.WRONG_OWNER,
    )
    assert sig_1 == sig_2
    assert "Bob" not in sig_1 and "Priya" not in sig_1


def test_error_signature_differs_by_field_name():
    sig_owner = compute_error_signature(
        target_type=CorrectionTargetType.OBSERVATION,
        field_name="owner_text",
        reason_code=CorrectionReasonCode.WRONG_OWNER,
    )
    sig_date = compute_error_signature(
        target_type=CorrectionTargetType.OBSERVATION,
        field_name="date_value",
        reason_code=CorrectionReasonCode.WRONG_DATE,
    )
    assert sig_owner != sig_date


def test_list_by_error_signature_finds_repeated_corrections(conn, project_id):
    for _ in range(3):
        correction_repository.insert_correction(
            conn,
            project_id,
            target_type=CorrectionTargetType.OBSERVATION,
            target_id="obs-1",
            field_name="owner_text",
            reason_code=CorrectionReasonCode.WRONG_OWNER,
            materiality=CorrectionMateriality.MATERIAL,
        )
    signature = compute_error_signature(
        target_type=CorrectionTargetType.OBSERVATION,
        field_name="owner_text",
        reason_code=CorrectionReasonCode.WRONG_OWNER,
    )
    matches = correction_repository.list_by_error_signature(conn, project_id, signature)
    assert len(matches) == 3


def test_list_for_project_returns_every_correction(conn, project_id):
    correction_repository.insert_correction(
        conn,
        project_id,
        target_type=CorrectionTargetType.OBSERVATION,
        target_id="obs-1",
        field_name="owner_text",
        reason_code=CorrectionReasonCode.WRONG_OWNER,
        materiality=CorrectionMateriality.MINOR,
    )
    correction_repository.insert_correction(
        conn,
        project_id,
        target_type=CorrectionTargetType.LEDGER_VERSION,
        target_id="ver-1",
        field_name="status",
        reason_code=CorrectionReasonCode.WRONG_STATUS,
        materiality=CorrectionMateriality.MATERIAL,
    )
    assert len(correction_repository.list_for_project(conn, project_id)) == 2


def test_correction_requires_a_reason_code_and_materiality(conn, project_id):
    with pytest.raises(TypeError):
        correction_repository.insert_correction(
            conn,
            project_id,
            target_type=CorrectionTargetType.OBSERVATION,
            target_id="obs-1",
            field_name="owner_text",
        )


def test_review_id_may_be_null(conn, project_id):
    correction = correction_repository.insert_correction(
        conn,
        project_id,
        target_type=CorrectionTargetType.OBSERVATION,
        target_id="obs-1",
        field_name="owner_text",
        reason_code=CorrectionReasonCode.WRONG_OWNER,
        materiality=CorrectionMateriality.MINOR,
        review_id=None,
    )
    assert correction.review_id is None


def test_correction_requires_a_real_review_when_given(conn, project_id):
    with pytest.raises(sqlite3.IntegrityError):
        correction_repository.insert_correction(
            conn,
            project_id,
            target_type=CorrectionTargetType.OBSERVATION,
            target_id="obs-1",
            field_name="owner_text",
            reason_code=CorrectionReasonCode.WRONG_OWNER,
            materiality=CorrectionMateriality.MINOR,
            review_id="nonexistent-review",
        )
