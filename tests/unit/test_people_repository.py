"""Tests for the people/alias/stakeholder repository: exact-email
resolution, known-alias resolution, ambiguous names, unknown people, and
project-scoped stakeholder upserts (Prompt 6)."""

from __future__ import annotations

import sqlite3

import pytest

from project_context.db import people_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.people import AliasType, PersonStatus, ProjectPersonStatus
from project_context.domain.projects import ProjectCreateInput
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


# --- people / aliases -------------------------------------------------


def test_create_person_round_trips(conn):
    person = people_repository.create_person(
        conn, display_name="Priya Patel", primary_email="priya@example.com"
    )
    assert person.display_name == "Priya Patel"
    assert person.status is PersonStatus.ACTIVE
    assert people_repository.get_person(conn, person.id) == person


def test_get_person_by_email_is_case_and_whitespace_insensitive(conn):
    people_repository.create_person(conn, display_name="Priya", primary_email="Priya@Example.com")
    found = people_repository.get_person_by_email(conn, "  priya@example.com  ")
    assert found is not None
    assert found.display_name == "Priya"


def test_add_alias_normalizes_email(conn):
    person = people_repository.create_person(conn, display_name="Priya")
    alias = people_repository.add_alias(
        conn, person.id, alias_type=AliasType.EMAIL, alias_value="Priya.P@Example.COM"
    )
    assert alias.normalized_value == "priya.p@example.com"


def test_email_alias_conflicting_with_another_person_raises(conn):
    person_a = people_repository.create_person(conn, display_name="Priya A")
    person_b = people_repository.create_person(conn, display_name="Priya B")
    people_repository.add_alias(
        conn, person_a.id, alias_type=AliasType.EMAIL, alias_value="shared@example.com"
    )
    with pytest.raises(sqlite3.IntegrityError):
        people_repository.add_alias(
            conn, person_b.id, alias_type=AliasType.EMAIL, alias_value="shared@example.com"
        )


# --- resolution: the four required scenarios ---------------------------


def test_resolve_by_exact_email_on_primary_email(conn):
    person = people_repository.create_person(
        conn, display_name="Priya", primary_email="priya@example.com"
    )
    result = people_repository.resolve_person(conn, email="priya@example.com")
    assert result.outcome == "resolved"
    assert result.person_id == person.id


def test_resolve_by_known_email_alias(conn):
    person = people_repository.create_person(conn, display_name="Priya")
    people_repository.add_alias(
        conn, person.id, alias_type=AliasType.EMAIL, alias_value="priya.old@example.com"
    )
    result = people_repository.resolve_person(conn, email="priya.old@example.com")
    assert result.outcome == "resolved"
    assert result.person_id == person.id


def test_resolve_by_known_name_alias_when_unique(conn):
    person = people_repository.create_person(conn, display_name="Priya Patel")
    people_repository.add_alias(conn, person.id, alias_type=AliasType.NAME, alias_value="Priya")
    result = people_repository.resolve_person(conn, name="priya")
    assert result.outcome == "resolved"
    assert result.person_id == person.id


def test_resolve_by_name_is_ambiguous_when_multiple_people_share_it(conn):
    person_a = people_repository.create_person(conn, display_name="Alex Kim")
    person_b = people_repository.create_person(conn, display_name="Alex Chen")
    people_repository.add_alias(conn, person_a.id, alias_type=AliasType.NAME, alias_value="Alex")
    people_repository.add_alias(conn, person_b.id, alias_type=AliasType.NAME, alias_value="Alex")

    result = people_repository.resolve_person(conn, name="Alex")

    assert result.outcome == "ambiguous"
    assert result.person_id is None
    assert set(result.candidate_person_ids) == {person_a.id, person_b.id}


def test_resolve_unknown_person_returns_unknown(conn):
    result = people_repository.resolve_person(conn, email="nobody@example.com")
    assert result.outcome == "unknown"
    assert result.person_id is None
    assert result.candidate_person_ids == ()


def test_resolve_prefers_email_over_name_when_both_given(conn):
    by_email = people_repository.create_person(
        conn, display_name="Priya", primary_email="priya@example.com"
    )
    other = people_repository.create_person(conn, display_name="Priya Other")
    people_repository.add_alias(conn, other.id, alias_type=AliasType.NAME, alias_value="Priya")

    result = people_repository.resolve_person(conn, email="priya@example.com", name="Priya")

    assert result.outcome == "resolved"
    assert result.person_id == by_email.id


def test_resolve_falls_back_to_name_when_email_does_not_match(conn):
    person = people_repository.create_person(conn, display_name="Priya")
    people_repository.add_alias(conn, person.id, alias_type=AliasType.NAME, alias_value="Priya")

    result = people_repository.resolve_person(conn, email="nobody@example.com", name="Priya")

    assert result.outcome == "resolved"
    assert result.person_id == person.id


def test_resolve_with_neither_email_nor_name_is_unknown(conn):
    result = people_repository.resolve_person(conn)
    assert result.outcome == "unknown"


# --- project_people ----------------------------------------------------


def test_upsert_project_person_creates_then_updates(conn, project_id):
    person = people_repository.create_person(conn, display_name="Priya")

    created = people_repository.upsert_project_person(
        conn, project_id, person.id, role="sponsor", organization="Acme"
    )
    assert created.status is ProjectPersonStatus.ACTIVE
    assert created.first_seen_at == created.last_seen_at

    updated = people_repository.upsert_project_person(
        conn,
        project_id,
        person.id,
        role="sponsor",
        organization="Acme Corp",
        seen_at="2026-09-01T00:00:00Z",
    )
    assert updated.id == created.id
    assert updated.organization == "Acme Corp"
    assert updated.first_seen_at == created.first_seen_at
    assert updated.last_seen_at == "2026-09-01T00:00:00Z"


def test_project_people_scoped_by_project(conn, project_id):
    other_project = create_project(
        conn, ProjectCreateInput(name="Other Project", objective="Other")
    ).id
    person = people_repository.create_person(conn, display_name="Priya")
    people_repository.upsert_project_person(conn, project_id, person.id, role="sponsor")

    assert len(people_repository.list_project_people(conn, project_id)) == 1
    assert people_repository.list_project_people(conn, other_project) == []


def test_project_people_distinguishes_different_roles_for_the_same_person(conn, project_id):
    person = people_repository.create_person(conn, display_name="Priya")
    people_repository.upsert_project_person(conn, project_id, person.id, role="sponsor")
    people_repository.upsert_project_person(conn, project_id, person.id, role="reviewer")

    assert len(people_repository.list_project_people(conn, project_id)) == 2
