"""Person/alias/stakeholder domain model (Section 9, tables `people`,
`person_aliases`, `project_people`).

`Person` and `PersonAlias` are deliberately NOT project-scoped — Section
9 describes canonical identity as shared across projects ("One person
may have multiple email addresses; identity merging is manual in MVP",
Section 4's assumptions; Section 16's "Delete/anonymize with all
associated projects"). `ProjectPerson` is the project-scoped join that
carries a per-project stakeholder role. See
migrations/0005_ledger_and_review.sql for the exact schema.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class PersonStatus(StrEnum):
    """Matches the `people.status` CHECK constraint exactly."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class AliasType(StrEnum):
    """Matches the `person_aliases.alias_type` CHECK constraint exactly."""

    EMAIL = "email"
    NAME = "name"
    SPEAKER_LABEL = "speaker_label"


class ProjectPersonStatus(StrEnum):
    """Matches the `project_people.status` CHECK constraint exactly."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class Person(BaseModel):
    """A person row as stored. See Section 9, table `people`."""

    id: str
    display_name: str
    primary_email: str | None
    organization: str | None
    status: PersonStatus
    created_at: str
    updated_at: str


class PersonAlias(BaseModel):
    """A person-alias row as stored. See Section 9, table `person_aliases`."""

    id: str
    person_id: str
    alias_type: AliasType
    alias_value: str
    normalized_value: str
    source: str | None
    created_at: str


class ProjectPerson(BaseModel):
    """A project-scoped stakeholder-role row as stored. See Section 9,
    table `project_people`."""

    id: str
    project_id: str
    person_id: str
    role: str | None
    organization: str | None
    is_internal: bool
    status: ProjectPersonStatus
    first_seen_at: str
    last_seen_at: str
    evidence_link_id: str | None
    created_at: str
    updated_at: str


class PersonResolution(BaseModel):
    """The outcome of resolving an email or name to a canonical person
    (Prompt 6: "Person/alias repository with exact-email resolution
    first and explicit ambiguous-name result").

    - `"resolved"`: exactly one person matched; `person_id` is set.
    - `"ambiguous"`: more than one distinct person matched a name/alias
      lookup; `candidate_person_ids` lists them and `person_id` is None.
      Never silently picked — Section 10.13: human review is required
      for "new commitments attributed to someone not named in evidence,"
      and an ambiguous name is exactly that kind of uncertainty.
    - `"unknown"`: nothing matched at all.
    """

    outcome: Literal["resolved", "ambiguous", "unknown"]
    person_id: str | None = None
    candidate_person_ids: tuple[str, ...] = ()
