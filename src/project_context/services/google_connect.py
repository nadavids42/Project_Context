"""Google Drive/Gmail/Calendar first-time connect: runs the OAuth
desktop flow and stores the resulting refresh token through the
credential service (Section 11.2, 11.3, 11.4, 16; Prompt 10, Prompt 11,
Prompt 12).

`flow_runner` defaults to the real, browser-opening
`google_oauth.run_local_server_flow` — tests always inject a fake here
instead (Section 15: "Keep live credential setup outside automated
tests"); nothing in this module's own tests ever performs real
network/browser I/O.

`connect_google_drive` and `connect_gmail` each run their own,
independent OAuth flow requesting only their own connector's minimal
scope — a deliberate simplification of the product plan's "same Google
OAuth connection; incremental authorization only when user enables
Gmail" (Section 11.3): rather than pooling one Google token across
connector kinds and adding a scope to it, each connector source keeps
its own credential_ref/refresh token from its own consent, which this
prototype's existing per-source credential model already supports with
no schema change. The practical effect is the same or better
least-privilege outcome — a Gmail-only token never carries Drive
access, and vice versa — at the cost of a second consent screen if a
project owner enables both. See the Prompt 11 report for why this
tradeoff was made under the prompt's time box.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from google.oauth2.credentials import Credentials

from project_context.connectors import google_oauth
from project_context.credentials.service import CredentialService
from project_context.domain.sources import Source

FlowRunner = Callable[[dict[str, Any], list[str]], Credentials]


class GoogleConnectError(RuntimeError):
    """Raised when the OAuth flow completes but Google did not return
    usable offline credentials (most commonly: no refresh token, which
    happens if a caller forgets `access_type=offline`/`prompt=consent`
    — `google_oauth.run_local_server_flow` always sets both, so this
    should only fire for a genuinely unusual provider response)."""


def connect_google_drive(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    credential_service: CredentialService,
    client_id: str,
    client_secret: str,
    redirect_port: int = 0,
    flow_runner: FlowRunner = google_oauth.run_local_server_flow,
) -> Source:
    """Run the desktop OAuth flow (real browser + localhost callback
    unless `flow_runner` is overridden) and store the resulting refresh
    token as this source's credential. Never requests any scope beyond
    `DRIVE_READONLY_SCOPE` (Section 16: "Never request ... Drive write
    scopes")."""
    return _run_connect_flow(
        conn, project_id, source_id, credential_service=credential_service,
        client_id=client_id, client_secret=client_secret, redirect_port=redirect_port,
        flow_runner=flow_runner, scope=google_oauth.DRIVE_READONLY_SCOPE,
    )


def connect_gmail(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    credential_service: CredentialService,
    client_id: str,
    client_secret: str,
    redirect_port: int = 0,
    flow_runner: FlowRunner = google_oauth.run_local_server_flow,
) -> Source:
    """Run the desktop OAuth flow requesting only `GMAIL_READONLY_SCOPE`
    (Prompt 11: "Request incremental Google consent only when the user
    enables Gmail"; "Minimum functional scope:
    https://www.googleapis.com/auth/gmail.readonly"; "Never request
    modify, compose, send, settings, or full-mailbox write
    permissions"). Called only from a project's Gmail source, entirely
    independent of whether that project has also connected Drive — each
    connector source keeps its own credential/refresh token (see this
    module's docstring for why this prototype does not pool a single
    Google token across connector kinds)."""
    return _run_connect_flow(
        conn, project_id, source_id, credential_service=credential_service,
        client_id=client_id, client_secret=client_secret, redirect_port=redirect_port,
        flow_runner=flow_runner, scope=google_oauth.GMAIL_READONLY_SCOPE,
    )


def connect_calendar(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    credential_service: CredentialService,
    client_id: str,
    client_secret: str,
    redirect_port: int = 0,
    flow_runner: FlowRunner = google_oauth.run_local_server_flow,
) -> Source:
    """Run the desktop OAuth flow requesting only
    `CALENDAR_EVENTS_READONLY_SCOPE` (Prompt 12: "Request incremental
    consent only when enabled"; "Do not request Calendar write
    access"). Independent of whether this project has also connected
    Drive/Gmail — same per-source credential model, same tradeoff, as
    documented in this module's docstring."""
    return _run_connect_flow(
        conn, project_id, source_id, credential_service=credential_service,
        client_id=client_id, client_secret=client_secret, redirect_port=redirect_port,
        flow_runner=flow_runner, scope=google_oauth.CALENDAR_EVENTS_READONLY_SCOPE,
    )


def _run_connect_flow(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    *,
    credential_service: CredentialService,
    client_id: str,
    client_secret: str,
    redirect_port: int,
    flow_runner: FlowRunner,
    scope: str,
) -> Source:
    client_config = google_oauth.build_client_config(
        client_id=client_id, client_secret=client_secret
    )
    credentials = flow_runner(client_config, [scope], port=redirect_port)
    refresh_token = getattr(credentials, "refresh_token", None)
    if not refresh_token:
        raise GoogleConnectError(
            "Google did not return a refresh token for this connection. Try disconnecting "
            "this app's access at https://myaccount.google.com/permissions and connecting again."
        )
    return credential_service.connect(conn, project_id, source_id, secret=refresh_token)
