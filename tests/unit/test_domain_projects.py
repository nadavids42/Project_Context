"""Tests for project domain validation: required fields, length bounds,
and allowed status values (FR-001)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from project_context.domain.projects import (
    CLIENT_NAME_MAX_LENGTH,
    NAME_MAX_LENGTH,
    OBJECTIVE_MAX_LENGTH,
    ProjectCreateInput,
    ProjectStatus,
    ProjectUpdateInput,
)


def test_minimal_valid_input_is_accepted():
    data = ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot")

    assert data.name == "Acme Rollout"
    assert data.objective == "Ship the pilot"
    assert data.description is None
    assert data.stage is None
    assert data.client_name is None
    assert data.status is ProjectStatus.ACTIVE


@pytest.mark.parametrize("field", ["name", "objective"])
def test_blank_required_field_is_rejected(field):
    fields = {"name": "Acme Rollout", "objective": "Ship the pilot"}
    fields[field] = "   "

    with pytest.raises(ValidationError):
        ProjectCreateInput(**fields)


def test_name_over_max_length_is_rejected():
    with pytest.raises(ValidationError):
        ProjectCreateInput(name="x" * (NAME_MAX_LENGTH + 1), objective="Ship the pilot")


def test_objective_over_max_length_is_rejected():
    with pytest.raises(ValidationError):
        ProjectCreateInput(name="Acme Rollout", objective="x" * (OBJECTIVE_MAX_LENGTH + 1))


def test_client_name_over_max_length_is_rejected():
    with pytest.raises(ValidationError):
        ProjectCreateInput(
            name="Acme Rollout",
            objective="Ship the pilot",
            client_name="x" * (CLIENT_NAME_MAX_LENGTH + 1),
        )


def test_whitespace_only_optional_fields_normalize_to_none():
    data = ProjectCreateInput(
        name="Acme Rollout",
        objective="Ship the pilot",
        description="   ",
        stage="  ",
        client_name=" ",
    )

    assert data.description is None
    assert data.stage is None
    assert data.client_name is None


def test_required_fields_are_stripped():
    data = ProjectCreateInput(name="  Acme Rollout  ", objective="  Ship the pilot  ")

    assert data.name == "Acme Rollout"
    assert data.objective == "Ship the pilot"


@pytest.mark.parametrize(
    "status", [ProjectStatus.ACTIVE, ProjectStatus.ON_HOLD, ProjectStatus.COMPLETED]
)
def test_editable_statuses_are_accepted(status):
    data = ProjectCreateInput(name="Acme Rollout", objective="Ship the pilot", status=status)

    assert data.status is status


def test_archived_status_is_rejected_on_create():
    with pytest.raises(ValidationError):
        ProjectCreateInput(
            name="Acme Rollout", objective="Ship the pilot", status=ProjectStatus.ARCHIVED
        )


def test_archived_status_is_rejected_on_edit():
    with pytest.raises(ValidationError):
        ProjectUpdateInput(
            name="Acme Rollout", objective="Ship the pilot", status=ProjectStatus.ARCHIVED
        )


def test_unknown_status_value_is_rejected():
    with pytest.raises(ValidationError):
        ProjectCreateInput(
            name="Acme Rollout", objective="Ship the pilot", status="not_a_real_status"
        )
