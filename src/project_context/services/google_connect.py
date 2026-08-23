"""Google Drive first-time connect: runs the OAuth desktop flow and
stores the resulting refresh token through the credential service
(Section 11.2, 16; Prompt 10).

`flow_runner` defaults to the real, browser-opening
`google_oauth.run_local_server_flow` — tests always inject a fake here
instead (Section 15: "Keep live credential setup outside automated
tests"); nothing in this module's own tests ever performs real
network/browser I/O.
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
    client_config = google_oauth.build_client_config(
        client_id=client_id, client_secret=client_secret
    )
    credentials = flow_runner(
        client_config, [google_oauth.DRIVE_READONLY_SCOPE], port=redirect_port
    )
    refresh_token = getattr(credentials, "refresh_token", None)
    if not refresh_token:
        raise GoogleConnectError(
            "Google did not return a refresh token for this connection. Try disconnecting "
            "this app's access at https://myaccount.google.com/permissions and connecting again."
        )
    return credential_service.connect(conn, project_id, source_id, secret=refresh_token)
