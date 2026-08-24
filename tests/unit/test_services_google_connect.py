"""Tests for `project_context.services.google_connect`: the OAuth
first-connect flow, with `flow_runner` always faked — nothing here
opens a real browser or touches the network (Section 15; Prompt 10)."""

from __future__ import annotations

import pytest

from project_context.credentials.service import CredentialService
from project_context.credentials.store import CredentialStore
from project_context.db import sources_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceHealthStatus, SourceKind
from project_context.services.google_connect import (
    GoogleConnectError,
    connect_calendar,
    connect_gmail,
    connect_google_drive,
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


@pytest.fixture
def source_id(conn, project_id):
    return sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive folder"
    ).id


@pytest.fixture
def gmail_source_id(conn, project_id):
    return sources_repository.insert_source(
        conn, project_id, kind=SourceKind.GMAIL, display_name="Gmail label/query"
    ).id


@pytest.fixture
def calendar_source_id(conn, project_id):
    return sources_repository.insert_source(
        conn, project_id, kind=SourceKind.CALENDAR, display_name="Calendar rules"
    ).id


@pytest.fixture
def credential_service(tmp_path):
    store = CredentialStore(credentials_dir=tmp_path / "creds", prefer_keyring=False)
    return CredentialService(store)


class _FakeCredentials:
    def __init__(self, refresh_token):
        self.refresh_token = refresh_token


def test_connect_google_drive_stores_the_refresh_token(
    conn, project_id, source_id, credential_service
):
    calls = {}

    def fake_flow_runner(client_config, scopes, *, port=0):
        calls["client_config"] = client_config
        calls["scopes"] = scopes
        calls["port"] = port
        return _FakeCredentials(refresh_token="rt-abc123")

    source = connect_google_drive(
        conn,
        project_id,
        source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=fake_flow_runner,
    )

    assert source.health_status is SourceHealthStatus.READY
    assert source.credential_ref is not None
    assert credential_service.get_secret(conn, project_id, source_id) == "rt-abc123"
    assert calls["scopes"] == ["https://www.googleapis.com/auth/drive.readonly"]


def test_connect_google_drive_never_requests_a_write_scope(
    conn, project_id, source_id, credential_service
):
    seen_scopes = []

    def fake_flow_runner(client_config, scopes, *, port=0):
        seen_scopes.extend(scopes)
        return _FakeCredentials(refresh_token="rt")

    connect_google_drive(
        conn,
        project_id,
        source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=fake_flow_runner,
    )
    assert all("write" not in scope for scope in seen_scopes)


def test_connect_google_drive_raises_when_no_refresh_token_is_returned(
    conn, project_id, source_id, credential_service
):
    def fake_flow_runner(client_config, scopes, *, port=0):
        return _FakeCredentials(refresh_token=None)

    with pytest.raises(GoogleConnectError):
        connect_google_drive(
            conn,
            project_id,
            source_id,
            credential_service=credential_service,
            client_id="cid",
            client_secret="csecret",
            flow_runner=fake_flow_runner,
        )


def test_connect_gmail_stores_the_refresh_token(
    conn, project_id, gmail_source_id, credential_service
):
    calls = {}

    def fake_flow_runner(client_config, scopes, *, port=0):
        calls["scopes"] = scopes
        return _FakeCredentials(refresh_token="rt-gmail-123")

    source = connect_gmail(
        conn,
        project_id,
        gmail_source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=fake_flow_runner,
    )

    assert source.health_status is SourceHealthStatus.READY
    assert credential_service.get_secret(conn, project_id, gmail_source_id) == "rt-gmail-123"
    assert calls["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_connect_gmail_never_requests_a_write_or_send_scope(
    conn, project_id, gmail_source_id, credential_service
):
    seen_scopes = []

    def fake_flow_runner(client_config, scopes, *, port=0):
        seen_scopes.extend(scopes)
        return _FakeCredentials(refresh_token="rt")

    connect_gmail(
        conn,
        project_id,
        gmail_source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=fake_flow_runner,
    )
    for scope in seen_scopes:
        for forbidden in ("modify", "compose", "send", "settings", "write"):
            assert forbidden not in scope


def test_connect_gmail_raises_when_no_refresh_token_is_returned(
    conn, project_id, gmail_source_id, credential_service
):
    def fake_flow_runner(client_config, scopes, *, port=0):
        return _FakeCredentials(refresh_token=None)

    with pytest.raises(GoogleConnectError):
        connect_gmail(
            conn,
            project_id,
            gmail_source_id,
            credential_service=credential_service,
            client_id="cid",
            client_secret="csecret",
            flow_runner=fake_flow_runner,
        )


def test_connect_gmail_and_connect_drive_keep_independent_credentials(
    conn, project_id, source_id, gmail_source_id, credential_service
):
    """Section 11.3: "same Google OAuth connection; incremental
    authorization only when user enables Gmail." This prototype keeps
    each connector source's credential independent (see
    `project_context.services.google_connect`'s module docstring) —
    connecting Gmail must never touch Drive's stored token."""

    def drive_flow(client_config, scopes, *, port=0):
        return _FakeCredentials(refresh_token="drive-token")

    def gmail_flow(client_config, scopes, *, port=0):
        return _FakeCredentials(refresh_token="gmail-token")

    connect_google_drive(
        conn,
        project_id,
        source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=drive_flow,
    )
    connect_gmail(
        conn,
        project_id,
        gmail_source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=gmail_flow,
    )

    assert credential_service.get_secret(conn, project_id, source_id) == "drive-token"
    assert credential_service.get_secret(conn, project_id, gmail_source_id) == "gmail-token"


def test_connect_calendar_stores_the_refresh_token(
    conn, project_id, calendar_source_id, credential_service
):
    calls = {}

    def fake_flow_runner(client_config, scopes, *, port=0):
        calls["scopes"] = scopes
        return _FakeCredentials(refresh_token="rt-calendar-123")

    source = connect_calendar(
        conn,
        project_id,
        calendar_source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=fake_flow_runner,
    )

    assert source.health_status is SourceHealthStatus.READY
    assert credential_service.get_secret(conn, project_id, calendar_source_id) == "rt-calendar-123"
    assert calls["scopes"] == ["https://www.googleapis.com/auth/calendar.events.readonly"]


def test_connect_calendar_never_requests_a_write_scope(
    conn, project_id, calendar_source_id, credential_service
):
    seen_scopes = []

    def fake_flow_runner(client_config, scopes, *, port=0):
        seen_scopes.extend(scopes)
        return _FakeCredentials(refresh_token="rt")

    connect_calendar(
        conn,
        project_id,
        calendar_source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=fake_flow_runner,
    )
    for scope in seen_scopes:
        assert "write" not in scope


def test_connect_calendar_raises_when_no_refresh_token_is_returned(
    conn, project_id, calendar_source_id, credential_service
):
    def fake_flow_runner(client_config, scopes, *, port=0):
        return _FakeCredentials(refresh_token=None)

    with pytest.raises(GoogleConnectError):
        connect_calendar(
            conn,
            project_id,
            calendar_source_id,
            credential_service=credential_service,
            client_id="cid",
            client_secret="csecret",
            flow_runner=fake_flow_runner,
        )


def test_connect_calendar_keeps_independent_credentials_from_drive_and_gmail(
    conn, project_id, source_id, gmail_source_id, calendar_source_id, credential_service
):
    def flow_for(token):
        def runner(client_config, scopes, *, port=0):
            return _FakeCredentials(refresh_token=token)

        return runner

    connect_google_drive(
        conn,
        project_id,
        source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=flow_for("drive-token"),
    )
    connect_gmail(
        conn,
        project_id,
        gmail_source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=flow_for("gmail-token"),
    )
    connect_calendar(
        conn,
        project_id,
        calendar_source_id,
        credential_service=credential_service,
        client_id="cid",
        client_secret="csecret",
        flow_runner=flow_for("calendar-token"),
    )

    assert credential_service.get_secret(conn, project_id, source_id) == "drive-token"
    assert credential_service.get_secret(conn, project_id, gmail_source_id) == "gmail-token"
    assert credential_service.get_secret(conn, project_id, calendar_source_id) == "calendar-token"
