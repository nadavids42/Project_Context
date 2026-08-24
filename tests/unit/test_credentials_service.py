"""Tests for `project_context.credentials.service.CredentialService`:
connect/refresh/mask/disconnect/reauthorization-required (Section 16;
FR-032; Prompt 10)."""

from __future__ import annotations

import pytest

from project_context.credentials.service import (
    CredentialService,
    TokenRefreshError,
    mask_secret,
)
from project_context.credentials.store import CredentialStore
from project_context.db import sources_repository
from project_context.db.connection import connect
from project_context.db.migrations import run_migrations
from project_context.domain.projects import ProjectCreateInput
from project_context.domain.sources import SourceHealthStatus, SourceKind
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
        conn, project_id, kind=SourceKind.DRIVE, display_name="Project Drive folder"
    ).id


@pytest.fixture
def service(tmp_path):
    store = CredentialStore(credentials_dir=tmp_path / "credentials", prefer_keyring=False)
    return CredentialService(store)


def test_connect_stores_secret_and_marks_ready(conn, project_id, source_id, service):
    source = service.connect(
        conn,
        project_id,
        source_id,
        secret="refresh-token-123",
        external_account_id="user@example.com",
    )
    assert source.credential_ref is not None
    assert source.external_account_id == "user@example.com"
    assert source.health_status is SourceHealthStatus.READY
    assert service.get_secret(conn, project_id, source_id) == "refresh-token-123"


def test_the_raw_secret_never_appears_anywhere_in_the_sources_row(
    conn, project_id, source_id, service
):
    """FR-032: "Secrets never enter SQLite content tables ... repository
    scan/test fixtures find no raw keys/tokens." Reads the row back with
    a bare SQL query — not through the `Source` model, which would only
    prove the *typed* fields are clean — and checks every column's raw
    text for the secret."""
    secret = "sk-EXTREMELY-SENSITIVE-REFRESH-TOKEN-VALUE"
    service.connect(conn, project_id, source_id, secret=secret)

    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    for key, value in dict(row).items():
        if value is not None:
            assert secret not in str(value), f"secret leaked into sources.{key}"


def test_refresh_success_rotates_secret_and_marks_healthy(conn, project_id, source_id, service):
    service.connect(conn, project_id, source_id, secret="old-token")

    def refresh_fn(current: str) -> str:
        assert current == "old-token"
        return "new-token"

    source = service.refresh(conn, project_id, source_id, refresh_fn=refresh_fn)
    assert source.health_status is SourceHealthStatus.HEALTHY
    assert service.get_secret(conn, project_id, source_id) == "new-token"


def test_refresh_with_token_refresh_error_marks_reauth_required(
    conn, project_id, source_id, service
):
    service.connect(conn, project_id, source_id, secret="old-token")

    def refresh_fn(current: str) -> str:
        raise TokenRefreshError("token revoked")

    source = service.refresh(conn, project_id, source_id, refresh_fn=refresh_fn)
    assert source.health_status is SourceHealthStatus.REAUTH_REQUIRED
    assert source.last_error_code == "auth"
    # The old (now-untrustworthy-but-not-yet-rotated) secret is left in
    # place rather than destroyed — a future successful reconnect can
    # still overwrite it, and nothing here silently deletes credential
    # material outside of an explicit disconnect.
    assert service.get_secret(conn, project_id, source_id) == "old-token"


def test_refresh_propagates_transient_errors_without_marking_reauth_required(
    conn, project_id, source_id, service
):
    service.connect(conn, project_id, source_id, secret="old-token")

    def refresh_fn(current: str) -> str:
        raise ConnectionError("network blip")

    with pytest.raises(ConnectionError):
        service.refresh(conn, project_id, source_id, refresh_fn=refresh_fn)

    source = sources_repository.get_source(conn, project_id, source_id)
    assert source.health_status is SourceHealthStatus.READY  # unchanged, not downgraded


def test_refresh_without_a_prior_connect_marks_reauth_required(
    conn, project_id, source_id, service
):
    def refresh_fn(current: str) -> str:
        raise AssertionError("must not be called: nothing was ever connected")

    source = service.refresh(conn, project_id, source_id, refresh_fn=refresh_fn)
    assert source.health_status is SourceHealthStatus.REAUTH_REQUIRED


def test_disconnect_deletes_credential_material_and_disables_source(
    conn, project_id, source_id, service
):
    service.connect(conn, project_id, source_id, secret="token")
    source = service.disconnect(conn, project_id, source_id)

    assert source.credential_ref is None
    assert source.enabled is False
    assert source.health_status is SourceHealthStatus.DISABLED
    assert service.get_secret(conn, project_id, source_id) is None


def test_disconnect_before_any_connect_is_safe(conn, project_id, source_id, service):
    source = service.disconnect(conn, project_id, source_id)
    assert source.health_status is SourceHealthStatus.DISABLED


def test_mark_reauth_required_does_not_touch_other_sources(conn, project_id, service):
    source_a = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive A"
    )
    source_b = sources_repository.insert_source(
        conn, project_id, kind=SourceKind.DRIVE, display_name="Drive B"
    )
    service.connect(conn, project_id, source_a.id, secret="token-a")
    service.connect(conn, project_id, source_b.id, secret="token-b")

    service.mark_reauth_required(conn, project_id, source_a.id, error_code="auth")

    refreshed_a = sources_repository.get_source(conn, project_id, source_a.id)
    refreshed_b = sources_repository.get_source(conn, project_id, source_b.id)
    assert refreshed_a.health_status is SourceHealthStatus.REAUTH_REQUIRED
    assert refreshed_b.health_status is SourceHealthStatus.READY


def test_mask_secret_never_reveals_any_part_of_the_real_secret():
    secret = "sk-extremely-sensitive-oauth-refresh-token-value"
    masked = mask_secret(secret)
    assert secret not in masked
    for chunk in (secret[:4], secret[-4:]):
        assert chunk not in masked


def test_mask_secret_of_none_reads_not_connected():
    assert mask_secret(None) == "Not connected"


def test_mask_secret_is_constant_width_regardless_of_secret_length():
    assert mask_secret("short") == mask_secret("a-much-much-longer-secret-value-than-that-one")
