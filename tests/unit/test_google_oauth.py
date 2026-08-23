"""Tests for `project_context.connectors.google_oauth` (Section 11.2,
16; Prompt 10). `run_local_server_flow` opens a real browser/socket, so
even its own plumbing test replaces `InstalledAppFlow.from_client_config`
entirely — nothing here ever performs real network I/O or opens a
browser (Section 15: "Keep live credential setup outside automated
tests")."""

from __future__ import annotations

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from project_context.connectors import google_oauth
from project_context.credentials.service import TokenRefreshError


def test_drive_readonly_scope_is_exactly_the_restricted_readonly_scope():
    assert google_oauth.DRIVE_READONLY_SCOPE == "https://www.googleapis.com/auth/drive.readonly"
    assert "write" not in google_oauth.DRIVE_READONLY_SCOPE


def test_gmail_readonly_scope_is_exactly_the_restricted_readonly_scope():
    assert google_oauth.GMAIL_READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"
    for forbidden in ("modify", "compose", "send", "settings", "write"):
        assert forbidden not in google_oauth.GMAIL_READONLY_SCOPE


def test_build_client_config_has_the_installed_app_shape():
    config = google_oauth.build_client_config(client_id="cid-123", client_secret="csecret-456")
    installed = config["installed"]
    assert installed["client_id"] == "cid-123"
    assert installed["client_secret"] == "csecret-456"
    assert installed["redirect_uris"] == ["http://localhost"]
    assert installed["token_uri"] == google_oauth.GOOGLE_TOKEN_URI


def test_run_local_server_flow_delegates_to_installed_app_flow_without_real_io(monkeypatch):
    calls: dict = {}

    class FakeFlow:
        def run_local_server(self, port, **kwargs):
            calls["port"] = port
            calls["kwargs"] = kwargs
            return "fake-credentials-object"

    def fake_from_client_config(client_config, scopes):
        calls["client_config"] = client_config
        calls["scopes"] = scopes
        return FakeFlow()

    monkeypatch.setattr(
        InstalledAppFlow, "from_client_config", staticmethod(fake_from_client_config)
    )

    client_config = google_oauth.build_client_config(client_id="cid", client_secret="csecret")
    result = google_oauth.run_local_server_flow(
        client_config, [google_oauth.DRIVE_READONLY_SCOPE], port=54321
    )

    assert result == "fake-credentials-object"
    assert calls["port"] == 54321
    assert calls["scopes"] == [google_oauth.DRIVE_READONLY_SCOPE]
    # Explicitly requested so a refresh token is reliably returned, not
    # only on the account's very first-ever consent for this client.
    assert calls["kwargs"] == {"access_type": "offline", "prompt": "consent"}
    assert calls["client_config"] == client_config


def test_exchange_refresh_token_returns_the_fresh_access_token(monkeypatch):
    def fake_refresh(self, request):
        self.token = "fresh-access-token"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)

    token = google_oauth.exchange_refresh_token(
        "stored-refresh-token", client_id="cid", client_secret="csecret"
    )
    assert token == "fresh-access-token"


def test_exchange_refresh_token_raises_token_refresh_error_on_revoked_token(monkeypatch):
    def fake_refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)

    with pytest.raises(TokenRefreshError):
        google_oauth.exchange_refresh_token(
            "revoked-refresh-token", client_id="cid", client_secret="csecret"
        )


def test_exchange_refresh_token_propagates_transient_errors_unchanged(monkeypatch):
    def fake_refresh(self, request):
        raise ConnectionError("network blip")

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)

    with pytest.raises(ConnectionError):
        google_oauth.exchange_refresh_token(
            "refresh-token", client_id="cid", client_secret="csecret"
        )


def test_exchange_refresh_token_defaults_to_drive_scope(monkeypatch):
    seen_scopes = {}

    def fake_refresh(self, request):
        seen_scopes["scopes"] = self.scopes
        self.token = "fresh-access-token"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)

    google_oauth.exchange_refresh_token(
        "stored-refresh-token", client_id="cid", client_secret="csecret"
    )
    assert seen_scopes["scopes"] == [google_oauth.DRIVE_READONLY_SCOPE]


def test_exchange_refresh_token_accepts_an_explicit_gmail_scope(monkeypatch):
    seen_scopes = {}

    def fake_refresh(self, request):
        seen_scopes["scopes"] = self.scopes
        self.token = "fresh-access-token"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)

    token = google_oauth.exchange_refresh_token(
        "stored-refresh-token", client_id="cid", client_secret="csecret",
        scopes=[google_oauth.GMAIL_READONLY_SCOPE],
    )
    assert token == "fresh-access-token"
    assert seen_scopes["scopes"] == [google_oauth.GMAIL_READONLY_SCOPE]
