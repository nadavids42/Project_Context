# Project Context

Project Context is a local, single-user, evidence-backed project
intelligence prototype. It turns explicitly bounded emails, calendar
events, meeting transcripts, documents, and manual notes into a
persistent, reviewable project ledger — decisions, commitments,
milestones, risks, blockers, open questions, and stakeholders — where
every claim is traceable to source evidence.

See [`docs/Project_Context_Product_Plan_v1.md`](docs/Project_Context_Product_Plan_v1.md)
for the full product and architecture plan. **Implemented so far:**
the full manual-ingestion vertical slice, end to end — create a
project; ingest manual text or an uploaded file (TXT, Markdown, DOCX,
PDF, VTT) or a synced Google Drive file; extract atomic, evidence-cited
observations with a versioned LLM prompt/schema; deterministically
reconcile them into reviewable proposals (create/update/complete/
cancel/supersede/conflict); review and accept/edit/reject them into a
versioned, append-only project ledger; and generate a cited Current
Project Brief from accepted ledger state only. Every step is
project-isolated, evidence-linked, and covered by repository/service/
UI tests. Also implemented: **read-only Google Drive sync** (Prompt
10) — a local OAuth desktop flow, OS-keyring-backed (with an encrypted-
file fallback) credential storage, one configured Drive folder per
project, recursive folder enumeration with change detection, Google
Docs export, and manual "Sync Project" orchestration feeding the same
extraction/reconciliation/review path as manual uploads — see
"[Google Drive setup](#google-drive-setup-optional)" below; and
**read-only Gmail sync** (Prompt 11) — one configured Gmail label
and/or search query per project, list-then-get message sync with a
48-hour-overlap watermark and external-message-ID dedup, normalized
plain-text/HTML-fallback body extraction with attachments excluded and
quoted-history/signatures trimmed only from what gets sent to
extraction (the complete message is always kept as evidence), feeding
the same extraction/reconciliation/review path — see
"[Gmail setup](#gmail-setup-optional)" below, including its restricted-
scope caveat. Calendar and Fathom connectors, the Meeting Preparation
Brief, and the evaluation harness are not implemented yet.

## ⚠️ Privacy and data policy

This is a local, single-user prototype — not a secured, multi-tenant
product.

- **Use only synthetic, personal, public, or explicitly authorized data.**
  Never use employer or customer data unless its owner has explicitly
  permitted it in writing.
- Source content is stored on this device. Extraction is opt-in and
  manually triggered per evidence item: only when you click "Extract
  observations" is that one chunk's text sent to the configured LLM
  provider (OpenAI, stateless, `store: false`); nothing is sent
  automatically or in the background. Extraction is disabled until you
  set `OPENAI_API_KEY` in your environment.
- External connectors are strictly read-only (no code path ever writes
  to Drive/Gmail/Calendar/Fathom) and disabled by default. Google Drive
  (Prompt 10) and Gmail (Prompt 11) are implemented but require you to
  explicitly opt in — see "[Google Drive setup](#google-drive-setup-optional)"
  and "[Gmail setup](#gmail-setup-optional)" below, both including a
  restricted-scope caveat. Calendar and Fathom are not implemented yet.
- Do not expose the Streamlit port beyond `127.0.0.1`.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or `pip`

## Install

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"
```

Or with plain `pip`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Copy the environment template (all variables are optional; safe local
defaults apply if you skip this):

```bash
cp .env.example .env
```

## Run

```bash
streamlit run app.py
```

Opens the Projects page. From there: create a project, open it to see
Project Overview, and use the sidebar to reach the other project-scoped
pages. Application configuration/health is on the Sources & Settings
page.

## Google Drive setup (optional)

Drive sync is fully implemented but **disabled by default**. Enabling
it is entirely optional — the manual-ingestion path (paste text or
upload a file) works with no setup at all and is the required fallback
for every Drive scenario.

> **Scope caveat — read before enabling.** This integration requests
> `https://www.googleapis.com/auth/drive.readonly`, which Google
> classifies as a **restricted** scope. That is the right choice for a
> private, local, single-user prototype you run yourself against your
> own test-mode OAuth client — it is **not appropriate for a casual
> public launch**: Google requires restricted-scope app verification,
> and storing/transmitting restricted-scope data through a third-party
> server can require an annual third-party security assessment. See
> Sections 11.2 and 16 of the product plan before promising Drive
> access to anyone other than yourself.

1. **Create a Google Cloud project** (or reuse one) at
   [console.cloud.google.com](https://console.cloud.google.com/).
2. **Enable the Google Drive API** for that project (APIs & Services →
   Library → "Google Drive API" → Enable).
3. **Configure the OAuth consent screen** (APIs & Services → OAuth
   consent screen). Choose **External** and leave the app in **Testing**
   mode — this keeps it unverified and capped to the test users you
   explicitly add next, which is the correct, honest state for a
   private prototype (do not attempt to publish/verify it).
4. **Add yourself as a test user** on that consent screen. Only test
   users can complete the OAuth flow while the app is unverified.
5. **Create an OAuth client ID** (APIs & Services → Credentials →
   Create Credentials → OAuth client ID) with application type
   **Desktop app**. Copy the generated Client ID and Client Secret.
6. **Set the two environment variables** (in your real, gitignored
   `.env`, not `.env.example`):

   ```bash
   PROJECT_CONTEXT_FEATURE_DRIVE_ENABLED=true
   PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID=your-client-id
   PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret
   ```

7. **Restart the app**, open a project's Sources & Settings page, and
   click **Connect Google Drive**. Your browser opens Google's consent
   screen; approving it completes a local desktop OAuth flow
   (`localhost` callback) and stores the resulting refresh token in
   your OS keyring (or, only if the keyring is unavailable, an
   explicit Fernet-encrypted local file with `0600` permissions and its
   key kept in a separate file — see
   [`src/project_context/credentials/store.py`](src/project_context/credentials/store.py)).
   No token is ever written to SQLite or logged.
8. **Configure exactly one Drive folder** for the project: paste the
   folder's ID (the segment after `/folders/` in its Drive URL) into
   the Drive folder ID field, click **Preview** to dry-run what would be
   matched, then **Save folder**.
9. Click **Sync Project**. This recursively enumerates the folder,
   downloads new/changed supported files, exports Google Docs to plain
   text, and feeds everything through the same parser/extraction/
   reconciliation pipeline manual uploads use — nothing here invents a
   different, Drive-specific fact-extraction path. Files trashed or
   removed from the folder are marked unavailable on the next full
   sync, never silently deleted from your ledger.

To disconnect at any time, click **Disconnect** — this deletes the
stored refresh token immediately and disables the source; it does not
delete evidence already imported (Section 16: external deletion never
erases previously imported evidence automatically).

**Known limitation, observed against the real Drive API:** `files.export`
only works for native Google Workspace types, and each type supports a
fixed set of export MIME types. This connector exports Google Docs to
plain text; Sheets, Slides, Forms, and Drawings are recognized but
deliberately skipped rather than guessed at — export them yourself to a
supported format and upload the result through the manual path if you
need one of those in a project's evidence.

## Gmail setup (optional)

Gmail sync is fully implemented but **disabled by default**, and is
the **first connector to cut** if you are short on time or on Google
verification budget — the manual-ingestion path remains the required
fallback for every Gmail scenario.

> **Scope caveat — read before enabling.** This integration requests
> `https://www.googleapis.com/auth/gmail.readonly`, which Google
> classifies as a **restricted** scope. That is the right choice for a
> private, local, single-user prototype you run yourself against your
> own test-mode OAuth client — it is **not appropriate for a casual
> public launch** and is, per the product plan, "the single largest
> commercialization obstacle" this application has: Google requires
> restricted-scope app verification, and storing/transmitting
> restricted-scope data through a third-party server can require an
> annual third-party security assessment. This connector never requests
> modify, compose, send, settings, or full-mailbox write permissions,
> and the application never exposes general mailbox search — only the
> one label/query boundary you configure per project. See Sections 11.3
> and 16 of the product plan before promising Gmail access to anyone
> other than yourself.

1. Reuse the same Google Cloud project and OAuth consent screen as
   Drive (see steps 1–4 above), or set one up following those steps if
   you have not already.
2. **Enable the Gmail API** for that project (APIs & Services →
   Library → "Gmail API" → Enable).
3. **Set the feature flag** (in your real, gitignored `.env`):

   ```bash
   PROJECT_CONTEXT_FEATURE_GMAIL_ENABLED=true
   ```

   The same `PROJECT_CONTEXT_GOOGLE_OAUTH_CLIENT_ID`/`_CLIENT_SECRET`
   used for Drive apply here too — Gmail requests its own scope through
   its own consent flow (a separate "Connect Gmail" click and its own
   stored refresh token; see
   [`src/project_context/services/google_connect.py`](src/project_context/services/google_connect.py)
   for why this prototype keeps Drive's and Gmail's credentials
   separate rather than pooling one token across both).
4. **Restart the app**, open a project's Sources & Settings page, and
   click **Connect Gmail**. Approving Google's consent screen stores
   the resulting refresh token exactly like Drive's (OS keyring first,
   encrypted-file fallback second — never SQLite, never logged).
5. **Configure a label, a query, or both** for the project: a Gmail
   label name (matched with the `label:` search operator) and/or a
   Gmail search query (e.g. `from:client@example.com subject:"Project
   Alpha"`) — click **Preview** to dry-run recent matching message
   metadata and see why each one matches, then **Save boundary**.
6. Click **Sync Project**. This lists matching message IDs
   (`users.messages.list`), fetches each one's full content
   (`users.messages.get`), and normalizes headers, participants,
   sent/received time, subject, thread/message IDs, and a plain-text
   body (falling back to a safe HTML-to-text conversion only when no
   plain-text part exists) into the same parser/extraction/
   reconciliation/review pipeline manual uploads and Drive use.
   Attachments are excluded entirely in this version.

**Incremental sync, evidence, and quoted history.** Each sync uses a
stored last-success watermark with a 48-hour overlap folded into the
query (Gmail's `after:` operator only supports day granularity) and
dedupes by Gmail message ID — a repeated sync with no new mail creates
no new content, observations, or proposals. Every imported message's
**complete** normalized body (including any quoted reply history) is
stored as that message's immutable evidence, exactly as fetched.
Separately, quoted-history and signature blocks are conservatively
trimmed from what gets *chunked for extraction only* — so replying
back and forth on a long thread does not re-extract the same older
commitments on every new message — while the full original text always
remains visible, unmodified, in the evidence viewer.

To disconnect at any time, click **Disconnect** — this deletes the
stored refresh token immediately and disables the source; it does not
delete evidence already imported.

**Not implemented, deliberately excluded from this version:** the
Gmail History API, push notifications/background sync, attachments,
and anything that reads/writes Gmail labels.

## Test

```bash
pytest
```

## Lint and format

```bash
ruff check .
ruff format .
```

## Configuration

All configuration is read from environment variables (or `.env`),
prefixed `PROJECT_CONTEXT_`, with safe local defaults — see
[`.env.example`](.env.example) for the full list and
[`src/project_context/config.py`](src/project_context/config.py) for
validation rules. Invalid values (unknown log level, malformed path,
conflicting paths, etc.) fail fast with a clear error rather than
starting the application in an unknown state, and every connector
feature flag defaults to disabled, so leaving all Google/Fathom
variables unset is a fully supported, fully functional configuration
(manual ingestion never depends on any of them).

Per-user secrets (the Drive refresh token, later a Fathom API key) are
never read from `.env` — see "[Google Drive setup](#google-drive-setup-optional)"
above and the product plan, Section 16, for the credential-storage
design (OS keyring first, an explicit encrypted-file fallback second,
never plaintext). The Google OAuth **client** ID/secret are configuration,
not a per-user secret, and are read from `.env` like everything else —
see the setup section above for why. The one remaining documented
exception is the OpenAI API key: the credential-store subsystem covers
Drive but extraction still reads the standard `OPENAI_API_KEY`
environment variable directly (see [`.env.example`](.env.example));
without it, "Extract observations" and Drive sync's extraction step
both show an actionable error instead of failing silently.

## Repository structure

```text
project-context/
├── app.py                    # Streamlit entry point
├── src/project_context/
│   ├── config.py              # typed configuration loading (incl. Google OAuth client, feature flags)
│   ├── observability.py       # redacted structured logging
│   ├── evidence_store.py      # content-addressed SHA-256 evidence storage
│   ├── spans.py                # character-span validation (FR-008)
│   ├── chunking.py             # deterministic paragraph/page/turn-boundary chunking
│   ├── credentials/            # OS-keyring-first, encrypted-file-fallback secret storage + connect/refresh/mask/disconnect service
│   ├── db/                    # connection, migrations, health, and one repository per table/domain (projects, audit, sources, evidence, people, observations, ledger, evidence links, proposed mutations, reviews, corrections, briefs, sync), FTS5
│   ├── domain/                # projects, audit, sources, evidence, people, observations, ledger, review, briefs, sync, email_normalization (enums, models)
│   ├── services/               # projects, evidence, extraction, observations, ledger, reconciliation, review, briefs, sync orchestration, drive_ingestion, gmail_ingestion, google_connect
│   ├── connectors/             # protocol/errors/http (shared), drive, gmail (both implemented), google_oauth; calendar/fathom not yet implemented
│   ├── parsers/                # txt, md, docx, pdf, vtt + content-first kind detection
│   ├── llm/                    # LLMProvider protocol, OpenAI adapter, retry, structured-extraction + brief-composition schemas, prompt loading
│   ├── retrieval/               # deterministic Current Project Brief fact builder
│   └── ui/                     # Streamlit pages: projects, overview, activity/review, ledger, evidence, briefs, sources & settings
├── migrations/                # numbered SQLite migrations (see migrations/README.md)
├── prompts/                   # versioned LLM prompt templates (extraction_v1.md, brief_current_v1.md)
├── tests/                     # unit, integration, fixtures, golden projects, prompt regression
├── scripts/                   # seed/evaluate/export/purge utilities (not yet implemented)
└── data/                      # local SQLite DB + evidence store + credentials — gitignored, created at runtime
```

See Section 8 of the product plan for the full architecture rationale.
