"""Project domain models: the typed shape of a project and its validated
create/edit inputs.

See docs/Project_Context_Product_Plan_v1.md FR-001 ("Project lifecycle")
and Section 9 ("projects" table). Validation here mirrors, and is
stricter than, the database CHECK constraints in
migrations/0001_evidence_foundation.sql: the database guards against
corrupt data reaching any table regardless of caller; these models
additionally enforce length bounds and archive semantics that would be
awkward to express in SQL and should fail before a query is even built.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, field_validator

#: Length bounds enforced on project fields. Not database CHECK
#: constraints (Section 9 only requires non-blank name/objective there);
#: these are product-level bounds enforced once, here, for every caller.
NAME_MAX_LENGTH = 200
OBJECTIVE_MAX_LENGTH = 4000
DESCRIPTION_MAX_LENGTH = 20000
STAGE_MAX_LENGTH = 200
CLIENT_NAME_MAX_LENGTH = 200


class ProjectStatus(StrEnum):
    """Matches the `projects.status` CHECK constraint exactly."""

    ACTIVE = "active"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"
    ARCHIVED = "archived"


#: Statuses settable directly through create/edit. `ARCHIVED` is reachable
#: only through the dedicated archive action (which also stamps
#: `archived_at`), and left only through the dedicated restore action —
#: see project_context.services.projects. This is "archive semantics":
#: archiving/restoring are lifecycle transitions, not ordinary metadata
#: edits.
EDITABLE_STATUSES: tuple[ProjectStatus, ...] = (
    ProjectStatus.ACTIVE,
    ProjectStatus.ON_HOLD,
    ProjectStatus.COMPLETED,
)


def _clean_required(value: str, field_name: str, max_length: int) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be empty or whitespace-only")
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return stripped


def _clean_optional(value: str | None, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return stripped


class _ProjectFields(BaseModel):
    """Shared field definitions/validators for create and edit input.

    Edit uses full-replacement semantics deliberately: the edit form is
    always pre-populated with the project's current values, so there is
    no need for partial-update ("only touch the fields I sent") support
    yet, and avoiding it sidesteps a None-means-"unset"-vs-"clear"
    ambiguity for the optional fields.
    """

    name: str
    objective: str
    description: str | None = None
    stage: str | None = None
    client_name: str | None = None
    status: ProjectStatus = ProjectStatus.ACTIVE

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_required(value, "name", NAME_MAX_LENGTH)

    @field_validator("objective")
    @classmethod
    def _validate_objective(cls, value: str) -> str:
        return _clean_required(value, "objective", OBJECTIVE_MAX_LENGTH)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        return _clean_optional(value, "description", DESCRIPTION_MAX_LENGTH)

    @field_validator("stage")
    @classmethod
    def _validate_stage(cls, value: str | None) -> str | None:
        return _clean_optional(value, "stage", STAGE_MAX_LENGTH)

    @field_validator("client_name")
    @classmethod
    def _validate_client_name(cls, value: str | None) -> str | None:
        return _clean_optional(value, "client_name", CLIENT_NAME_MAX_LENGTH)

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: ProjectStatus) -> ProjectStatus:
        if value not in EDITABLE_STATUSES:
            allowed = ", ".join(s.value for s in EDITABLE_STATUSES)
            raise ValueError(
                f"status must be one of [{allowed}]; archiving a project requires "
                "the dedicated archive action, not create/edit"
            )
        return value


class ProjectCreateInput(_ProjectFields):
    """Validated input for creating a project."""


class ProjectUpdateInput(_ProjectFields):
    """Validated input for editing project metadata."""


class Project(BaseModel):
    """A project record as stored. See Section 9, table `projects`.

    `id` is immutable once created — nothing in this module or its
    repository/service ever writes to it after `INSERT`.
    """

    id: str
    name: str
    objective: str
    description: str | None
    stage: str | None
    client_name: str | None
    status: ProjectStatus
    created_at: str
    updated_at: str
    archived_at: str | None
