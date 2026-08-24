"""Read-only Gmail label/query connector (Section 11.3; FR-004, FR-028;
Prompt 11).

Implements `project_context.connectors.protocol.Connector`:
`users.messages.list` against one configured label and/or search query
(Gmail's `q=` search syntax), then `users.messages.get` per returned ID
— list, then get, exactly as Section 11.3 specifies. Every call is
`GET` — never a write (FR-004).

**Minimum scope, by design (Prompt 11 requirement, Section 16):** this
connector only ever requests `google_oauth.GMAIL_READONLY_SCOPE`
(`https://www.googleapis.com/auth/gmail.readonly`) — a Google-classified
**restricted** scope and, per the product plan, "the single largest
commercialization obstacle" this application has. It is requested only
on incremental consent, only when a project owner explicitly enables
Gmail (see `project_context.services.google_connect.connect_gmail`),
and never alongside modify/compose/send/settings scopes. See
`docs/Project_Context_Product_Plan_v1.md` Sections 11.3 and 16, and
the README's Gmail setup section, before pointing this at anything
other than a private, single-user, test-mode OAuth client.

**Observed simplification, documented per this prompt's instruction**:
rather than resolving a label name to a Gmail label ID via a separate
`users.labels.list` call, this connector folds the configured label
into the `q=` search string using Gmail's own `label:` search operator
(`label:"Project Alpha"`), which matches by name directly. This keeps
list/preview to one call shape instead of two, at the cost of Gmail's
own quoting rules for label names containing spaces/punctuation — a
label name is quoted defensively (see `_quote_label`) rather than
passed through raw.

**Not implemented, deliberately (Prompt 11 explicitly excludes them):**
the Gmail History API, push notifications, attachments, and any
labels-modification/send path.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

from project_context.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorParseError,
    ConnectorPermissionError,
)
from project_context.connectors.http import HttpTransport, RequestsHttpTransport, request_with_retry
from project_context.connectors.protocol import (
    ArtifactMetadata,
    ConnectorHealth,
    ConnectorHealthStatus,
    DiscoveryPage,
    RawArtifact,
)
from project_context.domain.email_normalization import (
    EmailDecodeError,
    build_normalized_email_text,
    extract_plain_text_body,
    get_header,
    parse_rfc2822_date,
)
from project_context.domain.evidence import ArtifactType, EvidenceSourceType

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_MESSAGES_LIST_URL = f"{GMAIL_API_BASE}/messages"
GMAIL_PROFILE_URL = f"{GMAIL_API_BASE}/profile"

#: `format=metadata` restricted to just these headers keeps
#: list-time/preview calls cheap — the full body is fetched only once,
#: in `fetch()`, for a message actually being ingested.
_METADATA_HEADERS = ("Subject", "From", "To", "Cc", "Date")

_DEFAULT_PAGE_SIZE = 50
#: Gmail's own documented ceiling for `messages.list.maxResults`.
_GMAIL_MAX_PAGE_SIZE = 500


def _quote_label(label: str) -> str:
    """Defensive quoting for Gmail's `label:` search operator — a bare
    label name containing a space would otherwise be parsed as two
    separate search terms."""
    escaped = label.replace('"', '\\"')
    return f'label:"{escaped}"'


def _build_query(*, label: str | None, query: str | None, since_date: str | None) -> str:
    terms: list[str] = []
    if label:
        terms.append(_quote_label(label))
    if query:
        terms.append(f"({query})")
    if since_date:
        terms.append(f"after:{since_date}")
    return " ".join(terms)


class GmailConnector:
    """One configured Gmail label/query boundary (Section 11.3: "Query
    must include label, participants/domain, subject term, or explicit
    combination"). `access_token` is minted fresh by the caller from a
    stored refresh token before construction — nothing here persists or
    refreshes credentials (mirrors `DriveConnector`'s division of
    responsibility). `since_date` is the already-computed, already-
    overlapped `after:` watermark (`YYYY/MM/DD`, Gmail's own search
    granularity) — see `project_context.services.sync.sync_gmail_project`
    for where the 48-hour overlap is computed; `None` means "no lower
    bound" (a first sync)."""

    def __init__(
        self,
        *,
        access_token: str,
        label: str | None = None,
        query: str | None = None,
        since_date: str | None = None,
        http_transport: HttpTransport | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if not (label and label.strip()) and not (query and query.strip()):
            raise ConnectorConfigError("a Gmail label, query, or both are required")
        self._access_token = access_token
        self._label = label.strip() if label else None
        self._query = query.strip() if query else None
        self._since_date = since_date
        self._transport = http_transport or RequestsHttpTransport()
        self._page_size = max(1, min(page_size, _GMAIL_MAX_PAGE_SIZE))
        self._sleep = sleep
        self._rand = rand

    # --- Connector protocol -------------------------------------------

    def validate_config(self) -> ConnectorHealth:
        try:
            self._list_messages_page(query=self._effective_query(), page_token=None, max_results=1)
        except ConnectorAuthError as exc:
            return ConnectorHealth(status=ConnectorHealthStatus.AUTH_ERROR, detail=exc.safe_message)
        except ConnectorPermissionError as exc:
            return ConnectorHealth(
                status=ConnectorHealthStatus.PERMISSION_ERROR, detail=exc.safe_message
            )
        except ConnectorConfigError as exc:
            return ConnectorHealth(
                status=ConnectorHealthStatus.CONFIG_ERROR, detail=exc.safe_message
            )
        except ConnectorError as exc:
            return ConnectorHealth(status=ConnectorHealthStatus.ERROR, detail=exc.safe_message)
        return ConnectorHealth(status=ConnectorHealthStatus.OK)

    def preview(self, boundary: dict[str, Any], limit: int = 20) -> list[ArtifactMetadata]:
        """A dry run over `boundary["label"]`/`boundary["query"]`
        (which may not yet be saved) rather than this connector's own
        configured boundary — lets the UI show matched metadata before
        the user commits (FR-002), including why each message matches
        (Prompt 11: "why the boundary matches")."""
        label = boundary.get("label")
        query = boundary.get("query")
        if not (label and str(label).strip()) and not (query and str(query).strip()):
            raise ConnectorConfigError("boundary must include a label, query, or both")
        effective_query = _build_query(
            label=str(label).strip() if label else None,
            query=str(query).strip() if query else None,
            since_date=None,
        )
        page = self._list_messages_page(query=effective_query, page_token=None, max_results=limit)
        results = [self._fetch_metadata(ref["id"]) for ref in page.get("messages", [])[:limit]]
        return results

    def discover(self, checkpoint: dict[str, Any] | None) -> DiscoveryPage:
        """One `messages.list` page, then one `messages.get(format=
        metadata)` per returned ID (Section 11.3: "list ... then
        messages.get for returned IDs"). A message that has become
        inaccessible or vanished between `list` and `get` (404/403) is
        skipped rather than failing the whole page (Section 11.3:
        "inaccessible message retained as metadata" — approximated here
        by simply not surfacing it as a discoverable artifact rather
        than modeling a separate "inaccessible" placeholder row; see
        the Prompt 11 report for why)."""
        page_token = checkpoint.get("page_token") if checkpoint else None
        page = self._list_messages_page(
            query=self._effective_query(), page_token=page_token, max_results=self._page_size
        )
        artifacts: list[ArtifactMetadata] = []
        for ref in page.get("messages", []):
            try:
                artifacts.append(self._fetch_metadata(ref["id"]))
            except (ConnectorNotFoundError, ConnectorPermissionError):
                continue
        next_token = page.get("nextPageToken")
        next_checkpoint = {"page_token": next_token} if next_token else None
        return DiscoveryPage(artifacts=tuple(artifacts), next_checkpoint=next_checkpoint)

    def fetch(self, artifact: ArtifactMetadata) -> RawArtifact:
        message = self._get_message(artifact.external_id, fmt="full")
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        try:
            body = extract_plain_text_body(payload)
        except EmailDecodeError as exc:
            raise ConnectorParseError(str(exc)) from exc

        full_text, _body_start = build_normalized_email_text(
            subject=get_header(headers, "Subject"),
            from_header=get_header(headers, "From"),
            to_header=get_header(headers, "To"),
            cc_header=get_header(headers, "Cc"),
            date_header=get_header(headers, "Date"),
            message_id=artifact.external_id,
            thread_id=message.get("threadId", ""),
            body_text=body.text,
        )
        return RawArtifact(
            metadata=artifact,
            data=full_text.encode("utf-8"),
            mime_type="text/plain",
            filename=f"gmail-{artifact.external_id}.txt",
        )

    # --- internals ---------------------------------------------------

    def _effective_query(self) -> str:
        return _build_query(label=self._label, query=self._query, since_date=self._since_date)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _request(self, url: str, *, params: dict[str, Any]):
        response = request_with_retry(
            self._transport,
            "GET",
            url,
            params=params,
            headers=self._headers(),
            sleep=self._sleep,
            rand=self._rand,
        )
        if response.status_code == 400:
            raise ConnectorConfigError("Gmail rejected this label/query as malformed search syntax")
        return response

    def _list_messages_page(
        self, *, query: str, page_token: str | None, max_results: int
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query,
            "maxResults": max(1, min(max_results, _GMAIL_MAX_PAGE_SIZE)),
        }
        if page_token:
            params["pageToken"] = page_token
        response = self._request(GMAIL_MESSAGES_LIST_URL, params=params)
        return response.json()

    def _get_message(self, message_id: str, *, fmt: str) -> dict[str, Any]:
        params: dict[str, Any] = {"format": fmt}
        if fmt == "metadata":
            params["metadataHeaders"] = list(_METADATA_HEADERS)
        response = self._request(f"{GMAIL_MESSAGES_LIST_URL}/{message_id}", params=params)
        return response.json()

    def _fetch_metadata(self, message_id: str) -> ArtifactMetadata:
        message = self._get_message(message_id, fmt="metadata")
        headers = message.get("payload", {}).get("headers", [])
        subject = get_header(headers, "Subject") or "(no subject)"
        from_header = get_header(headers, "From")
        occurred_at = parse_rfc2822_date(get_header(headers, "Date"))
        return ArtifactMetadata(
            external_id=message["id"],
            title=subject,
            artifact_type=ArtifactType.EMAIL,
            source_type=EvidenceSourceType.EMAIL,
            mime_type="text/plain",
            # Messages are immutable once received — a constant marker
            # tied to the message's own ID means "unchanged" is true on
            # every future sync that rediscovers it inside the 48-hour
            # overlap window, so re-listing it never re-imports it
            # (Prompt 11: "A repeated sync with no new message must
            # create no content/observation/proposal duplicates").
            version_marker=f"gmail:{message['id']}",
            author=from_header,
            occurred_at=occurred_at,
            external_url=f"https://mail.google.com/mail/u/0/#all/{message['id']}",
            extra={"thread_id": message.get("threadId", "")},
        )
