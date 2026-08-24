"""Google OAuth desktop flow: authorization, token exchange, and
refresh (Section 11.2, Section 16; FR-004, FR-032; Prompt 10).

Live authorization (`run_local_server_flow`) opens a real browser and a
local HTTP server, then blocks until the user completes or cancels
consent. **Nothing in the automated test suite calls it** (Section 15:
"Keep live credential setup outside automated tests") — tests exercise
`build_client_config`, `exchange_refresh_token`'s error mapping, and
this module's own call shape against `InstalledAppFlow.from_client_config`
monkeypatched away entirely, never a real socket/browser.
"""

from __future__ import annotations

from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from project_context.credentials.service import TokenRefreshError

#: Section 11.2: "Use https://www.googleapis.com/auth/drive.readonly for
#: the functional private prototype." THIS IS A GOOGLE-CLASSIFIED
#: *RESTRICTED* SCOPE (Section 16, "Google verification reality"; Section
#: 11.2's "Critical scope caveat") — unsuitable for a casual public
#: launch. Google requires restricted-scope app verification, and
#: storing/transmitting restricted-scope data through a third-party
#: server can require an annual third-party security assessment. This
#: prototype requests it only for a private, local, single-user,
#: test-mode/unverified OAuth client. Do not point a production/public
#: deployment at this scope without first completing that review, or
#: redesigning around `drive.file` + Picker per Section 11.2's
#: commercial alternative. See docs/Project_Context_Product_Plan_v1.md
#: Sections 11.2 and 16 before changing this.
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

#: Never add a Drive write scope here (Section 16: "Never request ...
#: Drive write scopes"; FR-004: "No connector writes to an external
#: service"). `DRIVE_READONLY_SCOPE` is the only Drive scope this
#: application is ever allowed to request.

#: Section 11.3: "https://www.googleapis.com/auth/gmail.readonly to
#: read bodies; this is a restricted scope." THIS IS ALSO A
#: GOOGLE-CLASSIFIED *RESTRICTED* SCOPE — the same commercialization
#: caveat as `DRIVE_READONLY_SCOPE` above applies verbatim (Section 16,
#: "Google verification reality"). Requested only on incremental
#: consent, only when the user explicitly enables Gmail for one project
#: (Prompt 11: "Request incremental Google consent only when the user
#: enables Gmail").
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

#: Never add gmail.modify, gmail.compose, gmail.send, or any Gmail
#: settings/write scope here (Prompt 11: "Never request modify,
#: compose, send, settings, or full-mailbox write permissions").
#: `GMAIL_READONLY_SCOPE` is the only Gmail scope this application is
#: ever allowed to request.

#: Section 11.4/16: "https://www.googleapis.com/auth/calendar.events.readonly
#: (or calendar.readonly only if calendar metadata beyond events is
#: required)." Event-read-only is the narrower of the two and this
#: connector never reads anything beyond events (no calendar list
#: management, no settings), so it is the one requested — Section 16:
#: "Use event-read-only Calendar scope." Reading calendar events is a
#: Google-classified *sensitive* (not restricted) scope — a materially
#: lower commercialization bar than Drive/Gmail's restricted scopes,
#: but still requires verification for a public launch (Section 16,
#: "Google verification reality"). Requested only on incremental
#: consent, only when the user explicitly enables Calendar for one
#: project (Prompt 12: "Request incremental consent only when enabled").
CALENDAR_EVENTS_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"

#: Never add calendar write/events-owned-write/settings scopes here
#: (Prompt 12: "Do not request Calendar write access"; FR-004).
#: `CALENDAR_EVENTS_READONLY_SCOPE` is the only Calendar scope this
#: application is ever allowed to request.
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"


def build_client_config(*, client_id: str, client_secret: str) -> dict[str, Any]:
    """The "installed app" client config shape `InstalledAppFlow`
    expects, built from configuration rather than a downloaded
    `client_secret.json` file — nothing Google-specific needs to live
    on disk outside the credential store."""
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def run_local_server_flow(
    client_config: dict[str, Any], scopes: list[str], *, port: int = 0
) -> Credentials:
    """Open a real browser and a local HTTP callback server, and block
    until the user authorizes (or closes the tab). `port=0` lets the OS
    pick a free localhost port for the callback (Section 11.2:
    "OAuth 2.0 desktop client with localhost callback"). Requests
    `access_type=offline` and `prompt=consent` explicitly so Google
    reliably returns a refresh token on every connect, not only the
    first time this Google account ever authorized this client. Never
    call this from an automated test — see the module docstring."""
    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    return flow.run_local_server(port=port, access_type="offline", prompt="consent")


def exchange_refresh_token(
    refresh_token: str,
    *,
    client_id: str,
    client_secret: str,
    scopes: list[str] | None = None,
) -> str:
    """Redeem a stored refresh token for a fresh, short-lived access
    token. `scopes` defaults to `[DRIVE_READONLY_SCOPE]` for backward
    compatibility with the Drive-only callers this function predates; a
    Gmail caller passes `[GMAIL_READONLY_SCOPE]` instead. The value is
    informational on a refresh-token grant (Google returns whatever
    scope was actually granted at consent time regardless of what is
    requested here), but keeping it accurate avoids a misleading local
    `Credentials` object. Raises `TokenRefreshError` (mapped from
    Google's `invalid_grant`/`RefreshError`) when the refresh token
    itself is invalid or revoked — any other failure (network, 5xx)
    propagates unchanged so the caller retries rather than mistaking it
    for revocation."""
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes or [DRIVE_READONLY_SCOPE],
    )
    try:
        credentials.refresh(GoogleAuthRequest())
    except RefreshError as exc:
        raise TokenRefreshError("Google rejected the stored refresh token") from exc
    assert credentials.token is not None
    return credentials.token
