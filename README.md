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
scope caveat; and **read-only Calendar matching** (Prompt 12) — one
configured set of deterministic assignment rules per project (explicit
event ID > project-name term > client domain/participant > include/
exclude term or regex), a bounded 180-day-back/90-day-forward scan
window re-scanned on every manual sync, cancelled/no-longer-matching
events marked unavailable without erasing imported evidence, and only
an event's own description text ever entering extraction — metadata
(title, attendees, timing) alone can never produce a decision,
commitment, or risk — see
"[Calendar setup](#calendar-setup-optional)" below, including its
sensitive-scope caveat; **user-triggered Fathom API-key polling**
(Prompt 13) — `GET /meetings` with cursor pagination, a last-created
watermark with a 48-hour overlap, meeting transcripts as primary
extraction evidence with Fathom's own summary/action items stored as
secondary, citable evidence that is never itself sent to extraction,
and deterministic project-assignment rules (manual recording ID >
recorded-by/team > client domain > participant > meeting-URL/bounded-
time-window, the last of which always lands in Unassigned Evidence for
manual review) — see "[Fathom setup](#fathom-setup-optional)" below;
and **Zoom-to-Drive compatibility** (Prompt 13) — this application
never calls a Zoom API; it reads whatever an existing Zoom-to-Drive
recording workflow has already deposited in a configured Drive folder
through the same Drive connector everything else uses, with advisory-
only (never assignment-driving) filename hints — see
"[Zoom-to-Drive compatibility](#zoom-to-drive-compatibility)" below;
and the **Meeting Preparation Brief** (Prompt 14) — the second and
final generated output, on the Briefs page alongside the Current
Project Brief. Select an imported Calendar/Fathom meeting or enter one
manually (Calendar stays fully optional); the previous relevant meeting
and its cutoff are determined from accepted meeting evidence and shown
before generation, with an explicit override; participants are
resolved by exact email, then a known alias, then left explicitly
unresolved — never guessed. The deterministic fact set (purpose,
accepted changes since the cutoff, outstanding commitments grouped by
owner/unassigned, decisions required, risks/blockers, unanswered
questions) can be previewed and individually included/excluded before
one Stage C composition call adds a `Suggested Discussion Topics`
section — labeled suggestions only, never asserted as fact — through
the same claim-validation, evidence-citation, and Markdown-export path
the Current Project Brief uses. Finally, the **three-project
evaluation harness** (Section 13) runs the ledger system and a
recent-context baseline against three frozen synthetic corpora, scores
precision/recall/evidence-correctness/leakage/review-burden/cost, and
writes a go/iterate/stop report — see
`python scripts/run_evaluation.py --help` and
`src/project_context/evaluation/README.md`.

This is a **stabilized first-version prototype checkpoint**, not a
finished or commercial product — see
[`docs/RELEASE_NOTES.md`](docs/RELEASE_NOTES.md) for exactly what's
enabled/disabled/excluded, current test counts, and remaining known
issues, and [`docs/ADR.md`](docs/ADR.md) for the twelve key
architecture decisions behind this design.

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
  to Drive/Gmail/Calendar/Fathom, and this application never calls a
  Zoom API at all) and disabled by default. Google Drive (Prompt 10),
  Gmail (Prompt 11), Calendar (Prompt 12), and Fathom (Prompt 13) are
  implemented but require you to explicitly opt in — see
  "[Google Drive setup](#google-drive-setup-optional)",
  "[Gmail setup](#gmail-setup-optional)",
  "[Calendar setup](#calendar-setup-optional)", and
  "[Fathom setup](#fathom-setup-optional)" below, each including its
  own scope/authentication caveat.
- Do not expose the Streamlit port beyond `127.0.0.1`.
- **Parser limits — no OCR, no transcription.** A scanned/image-only
  PDF page is flagged `ocr_required` and its text is never guessed at;
  audio/video files are not transcribed by this application at all (a
  VTT/text transcript you already have, from Zoom, Fathom, or anywhere
  else, is what gets parsed and cited). DOCX headers/footers are not
  extracted. See "[Zoom-to-Drive
  compatibility](#zoom-to-drive-compatibility)" for one further, tested
  parser limitation.
- **No external writes, ever.** Every connector (Drive, Gmail,
  Calendar, Fathom) is read-only end to end — this application has no
  code path that creates, edits, or deletes anything in any of those
  providers, and does not call a Zoom API at all.
- **Deletion is explicit and two-tiered.** **Archive** (Projects page)
  is reversible and destroys nothing. **Delete** (same page) is
  irreversible: it previews exact row/file counts, requires typing the
  project's exact name to confirm, and then actually removes that
  project's evidence, ledger history, reviews, briefs, sync history,
  and connector credentials — see "[Delete a
  project](#delete-a-project)" below.
- **Backups are local-file copies, not encryption.** `scripts/backup.py`
  copies your database and evidence to a destination *you* choose,
  which is expected to already be encrypted storage — see "[Backup and
  restore](#backup-and-restore)" below for exactly what it does and
  does not guarantee.

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

## Calendar setup (optional)

Calendar matching is fully implemented but **disabled by default**,
and is the **second connector to cut** if you are short on time —
Drive is the priority. The manual-ingestion path remains the required
fallback for every Calendar scenario.

> **Scope caveat — read before enabling.** This integration requests
> `https://www.googleapis.com/auth/calendar.events.readonly` — the
> narrower of Google's two Calendar read scopes (Section 16: "Use
> event-read-only Calendar scope"), and never Calendar write access.
> Google classifies reading calendar events as a **sensitive** (not
> restricted) scope — a materially lower commercialization bar than
> Drive/Gmail's restricted scopes, but a public launch still needs
> Google's sensitive-scope verification. See Sections 11.4 and 16 of
> the product plan before promising Calendar access to anyone other
> than yourself.

1. Reuse the same Google Cloud project and OAuth consent screen as
   Drive/Gmail (see the Drive setup section above), or set one up
   following those steps if you have not already.
2. **Enable the Google Calendar API** for that project (APIs & Services
   → Library → "Google Calendar API" → Enable).
3. **Set the feature flag** (in your real, gitignored `.env`):

   ```bash
   PROJECT_CONTEXT_FEATURE_CALENDAR_ENABLED=true
   ```

   The same OAuth client ID/secret used for Drive/Gmail apply here too
   — Calendar requests its own scope through its own consent flow (a
   separate "Connect Calendar" click and its own stored refresh token,
   same independent-credential design as Gmail).
4. **Restart the app**, open a project's Sources & Settings page, and
   click **Connect Calendar**.
5. **Configure at least one deterministic assignment rule**, evaluated
   in this fixed priority order:
   1. Explicitly included event IDs (highest priority).
   2. Project name terms — matches an event whose title/description
      contains one of these (prefilled with the project's own name).
   3. Client domain and/or explicit participant emails — matches an
      event whose organizer/attendee email domain or address matches.
   4. Include terms/regex (lowest priority).

   Exclude terms/regex override every tier above, deterministically —
   an event matching both an include tier and an exclude rule is
   always excluded, never guessed at or sent to an LLM for a tiebreak.
   Click **Preview** to dry-run recent matched *and* a sample of
   unmatched events with their exact match/exclusion reasons, then
   **Save rules**. A scan window (default 180 days back, 90 days
   forward; configurable within validated bounds) is also set here.
6. Click **Sync Project**. This re-scans the full bounded window on
   every sync (no incremental sync token in this version — Section
   11.4 allows deferring that until a bounded rescan proves
   inadequate), deduplicates by event ID/`updated` timestamp, and
   feeds matched events through the same parser/extraction/
   reconciliation/review pipeline manual uploads and Drive/Gmail use.

**Calendar metadata alone never becomes a decision, commitment, or
risk.** Title, organizer, attendees, timing, and match reason are
always stored as evidence (so a Meeting Preparation Brief can later
select the right meeting and show its context), but only an event's
own `description` text — verbatim, never a synthesized sentence about
who attended or when — is ever handed to extraction. A matched event
with no description produces zero extraction chunks: nothing is
invented from the mere fact that a meeting exists.

Cancelled events, and events that stop matching a project's rules on a
later sync (an edited title, a changed rule set), are marked
unavailable — never silently deleted from the ledger; their previously
imported evidence stays queryable.

To disconnect at any time, click **Disconnect** — this deletes the
stored refresh token immediately and disables the source; it does not
delete evidence already imported.

**Not implemented, deliberately excluded from this version:** event
creation/modification, attendee invites, free/busy scheduling,
webhooks, background sync, and incremental sync tokens (`nextSyncToken`).

## Fathom setup (optional)

Fathom API-key polling is fully implemented but **disabled by
default**. Unlike Drive/Gmail/Calendar, it needs no Google OAuth client
at all — it uses one user-generated Fathom API key per install, sent as
`X-Api-Key` through the exact same credential service every other
connector uses (Section 16: "user-generated API key ... through the
existing credential service"). The manual-ingestion path remains the
required fallback if you skip this.

> **Scope caveat — read before enabling.** This is the private,
> API-key-authenticated shape of Fathom's API — the one Fathom itself
> recommends for personal/internal automation. It is **not** Fathom
> OAuth, has **no webhook**, and never downloads recording media; it
> only calls `GET /meetings` with `include_transcript=true` (a
> documented Fathom "heavy request"). See Section 11.5 of the product
> plan before enabling this for anyone other than yourself.

1. Generate an API key at fathom.video → Settings → API keys.
2. **Set the feature flag** (in your real, gitignored `.env`):

   ```bash
   PROJECT_CONTEXT_FEATURE_FATHOM_ENABLED=true
   ```
3. **Restart the app**, open a project's Sources & Settings page, paste
   the key into **Fathom API key**, and click **Connect Fathom**. The
   key is stored exactly like a Drive/Gmail/Calendar refresh token —
   OS keyring, or an encrypted local file with its key kept separately
   — and is never echoed back after saving.
4. **Configure at least one deterministic assignment rule**, evaluated
   in this fixed priority order:
   1. Explicitly included recording IDs (highest priority).
   2. Recorded-by/team emails — any meeting recorded by one of these
      belongs to this project.
   3. Client email domain — matches a calendar invitee or transcript
      speaker at this domain.
   4. Explicit participant emails.
   5. A configured recurring meeting URL, or a bounded time window
      (lowest priority).

   Unlike every tier above it, **tier 5 is never auto-assigned** — a
   meeting-URL or time-window match alone carries no participant/domain
   corroboration, so it always lands in **Unassigned Evidence** for
   manual confirmation instead (Section 11.5: "Ambiguity goes to
   unassigned/manual review"). Click **Preview** to dry-run recent
   matched *and* a sample of unmatched meetings with their exact match
   reasons — a preview flags a would-be-unassigned match explicitly —
   then **Save rules**.
5. Click **Sync Project**. This is always **user-triggered**, never a
   background poll: it lists meetings created since your last
   successful sync minus a 48-hour overlap, deduplicates by Fathom's
   own stable `recording_id`, and feeds matched meetings through the
   same parser/extraction/reconciliation/review pipeline every other
   source uses.

**Meeting transcripts are the primary evidence sent to extraction.**
Fathom's own generated summary and action items are stored and fully
visible/citable in the evidence viewer, but are never themselves sent
to extraction — so a Fathom-produced action item can never become an
automatically accepted ledger commitment on its own; a person has to
read it and create/confirm the ledger item themselves. A meeting whose
transcript has not finished Fathom's own post-processing yet still
imports its metadata as evidence immediately; the next sync's overlap
rescan picks up the transcript once it becomes available (there is no
webhook, so this application never assumes it will be told).

To disconnect at any time, click **Disconnect** — this deletes the
stored API key immediately and disables the source; it does not delete
evidence already imported.

**Not implemented, deliberately excluded from this version:** Fathom
OAuth, Fathom webhooks, recording-media/audio download, and background
polling.

## Zoom-to-Drive compatibility

This application has **no native Zoom integration and calls no Zoom
API** — see Section 11.6/11.7 of the product plan for why (avoiding a
second OAuth app, host/admin-permission questions, recording-download
security, and webhook operations). Instead, it is compatible with
whatever your **existing** Zoom-to-Drive workflow already produces —
Zoom's own cloud-recording pipeline exporting a transcript and/or an AI
Companion meeting summary that lands in a Google Drive folder you have
already configured as a project's Drive source (see
"[Google Drive setup](#google-drive-setup-optional)" above). **This is
a prerequisite you must already have set up outside this
application** — nothing here creates, configures, or verifies that
Zoom-to-Drive workflow.

**Supported file types**, discovered and parsed exactly like any other
Drive file (no Zoom-specific code path):

- **VTT transcripts** (`.vtt`) — Zoom's own cloud-recording transcript
  export, parsed by the same VTT parser every other transcript source
  uses.
- **Chat exports** (`.txt`) and **meeting-summary documents**
  (`.txt`/`.docx`, e.g. a Zoom AI Companion summary) — parsed by the
  same TXT/DOCX parsers manual uploads and Drive already use.

**Filename hints are advisory only, never a project decision.** The
evidence viewer shows a small caption (e.g. "Looks like a Zoom
cloud-recording transcript") when a stored file's original filename
matches one of Zoom's own documented default export patterns
(`GMT<timestamp>_...`, `....transcript.vtt`, `....chat.txt`) or looks
like a meeting-summary document. This is display text only — which
project a file belongs to is always decided by which project's
configured Drive folder the file was found in, exactly as it is for
every other Drive file; a Zoom-shaped filename in the wrong project's
folder is still that project's evidence, and an oddly-named file inside
the right folder is still correctly assigned.

**Known limitation, discovered while building this compatibility
coverage:** Zoom's actual VTT transcript export does not use WebVTT
`<v Speaker>` voice tags — it puts the speaker's name as a plain-text
`"Speaker Name: "` prefix inside each cue's own text instead. The VTT
parser only recognizes `<v>` tags for its "merge adjacent cues from the
same speaker" behavior (Section 8), so a Zoom-exported transcript
imports completely (every word from every speaker is preserved,
verbatim, and fully evidence-linkable) but without per-speaker turn
boundaries — every cue in one Zoom VTT file currently merges into a
single block. See `tests/fixtures/zoom_fixtures.py` and
`tests/unit/test_zoom_drive_compatibility.py` for the fixture that
surfaces this; it is a reasonable candidate for a small, dedicated
follow-up (recognizing the plain-text `"Name: "` prefix as a fallback
speaker marker) rather than something this change makes silently.

## Delete a project

The Projects page has two destructive-looking but very different
actions, side by side:

- **Archive** — reversible. Hides the project from the active list;
  every row, file, and credential is retained; **Restore** undoes it.
- **Delete** — irreversible. Click **Delete**, and the confirmation
  dialog shows real, freshly computed counts (evidence artifacts,
  content versions and stored files, observations, ledger items/
  versions, reviews, corrections, generated briefs, sync runs) before
  asking you to type the project's exact name to proceed. Confirming
  then:
  1. disconnects and deletes any connector credential this project's
     sources hold (separately from, and in addition to, what
     **Disconnect** on Sources & Settings already does per-source);
  2. deletes every row this project owns — evidence, chunks, FTS
     entries, observations, ledger items/versions/evidence links,
     proposed mutations, reviews, corrections, stakeholder links,
     generated briefs/claims, sync history, and its own audit trail;
  3. deletes the on-disk content-addressed evidence file for each
     piece of content, **but only if no other project's evidence still
     references those exact bytes** (this storage layer dedupes
     identical content across projects; deleting one project never
     deletes another project's evidence out from under it).

  Canonical identity records (`people`) are shared across projects by
  design (Section 9) and are never deleted by a project purge, even if
  this was the only project referencing a given person.

  This never happens automatically, never happens without the exact
  typed confirmation, and — unlike disconnecting a single connector on
  Sources & Settings — is not scoped to one source; it is the whole
  project. See `src/project_context/services/project_deletion.py`.

## Backup and restore

```bash
python scripts/backup.py backup --dest /path/to/your/encrypted/destination
python scripts/backup.py verify --backup-dir /path/to/.../project-context-backup-<timestamp>
python scripts/backup.py restore --backup-dir /path/to/.../project-context-backup-<timestamp> \
    --target-data-dir /path/to/a/scratch/or/replacement/data/dir
```

`backup` checkpoints the database's WAL and copies it via SQLite's own
online backup API (safe even if the app is currently running, unlike a
raw file copy), plus every content-addressed evidence file a row still
references, into a fresh timestamped directory — it never overwrites
an earlier backup. `--dest` is **expected to already be encrypted
storage** (an encrypted external drive, or somewhere under your
already-full-disk-encrypted home directory per Section 16) — this
script does not encrypt anything itself and cannot verify that a given
path actually is encrypted.

`restore` refuses to overwrite an existing database at
`--target-data-dir` unless you also pass `--force`; **never point
`--target-data-dir` at your real, in-use `data/` directory** — restore
into a scratch directory and inspect it first. `tests/unit/
test_backup_restore.py` includes a full backup -> restore smoke test,
run automatically by `pytest`, entirely against temporary directories.

**Known limitation:** this backs up your local files; it does not back
up anything held only in the OS keyring. If your credentials live in
the keyring (the default, when available) rather than the
encrypted-file fallback, reconnecting Drive/Gmail/Calendar/Fathom after
a restore onto a different machine/profile will be needed regardless —
the ledger, evidence, and briefs restore completely either way.

## Secure-delete maintenance (optional)

```bash
python scripts/secure_delete_maintenance.py
```

A separate, manual, opt-in maintenance step — never run automatically,
including not after **Delete a project** above. Sets `PRAGMA
secure_delete=ON` and runs `VACUUM` against the configured database,
which reduces (but does not certify-erase) the chance previously
deleted rows' bytes are still readable in the plain SQLite file.
**Read `src/project_context/db/maintenance.py`'s module docstring
before running this** — it documents exactly what this can and cannot
guarantee (an existing backup/copy is untouched; a WAL/SHM sidecar is
checkpointed first but a concurrent writer could still leave
something; SSD wear-leveling and copy-on-write filesystem snapshots are
entirely outside SQLite's — and this script's — control). Stop the
running application first: this needs exclusive access to the database
file and roughly its own size again in free disk space.

## Test

```bash
pytest
```

Automated tests cover unit/domain/repository/service/UI layers,
connector contracts against recorded fixtures (never live credentials),
prompt regression against frozen expected outputs, the three-project
golden evaluation corpora, and the `tests/security/` cross-project
sentinel/log-scan suite above. What automated tests cannot cover — a
real OAuth consent screen, a real Fathom API key, visual review of a
live sync — is
[`docs/MANUAL_ACCEPTANCE_CHECKLIST.md`](docs/MANUAL_ACCEPTANCE_CHECKLIST.md),
meant to be run on a clean Ubuntu profile before calling a build ready
for dogfooding.

## Lint and format

```bash
ruff check .
ruff format .
```

## Security scanning

```bash
ruff check .                       # includes no additional security rules; see below
pytest tests/unit/test_secret_scan.py       # real-shaped secret patterns in tracked files
pytest tests/security/                       # cross-project sentinel leakage + log-content scan
uv export --no-deps -o requirements.txt && pip-audit -r requirements.txt   # or: pip-audit --local
```

`tests/unit/test_secret_scan.py` scans every tracked/would-be-tracked
text file for real-shaped OpenAI/Google/AWS/GitHub secret patterns and
confirms `.gitignore` actually covers this app's local credential
artifacts. `tests/security/` is the consolidated cross-project sentinel
suite (repositories, FTS, reconciliation, evidence-viewer authorization,
brief fact building, claim validation, exports, model-request
construction) plus a runtime log-content scan (evidence quotes/source
bodies/a synthetic sentinel never appear in a log record); narrower,
scattered isolation/redaction tests also exist throughout `tests/unit/`.

**Dependency vulnerability scanning** uses [`pip-audit`](https://pypi.org/project/pip-audit/)
(not a project dependency — install it ad hoc) against this project's
*locked* dependency set specifically, not whatever else happens to be
installed in your interpreter — `pip-audit -r <(uv export --no-deps)`,
or sync `uv.lock` into a clean venv first and run `pip-audit --local`
there. As of this checkpoint: **`pytest` 8.4.2 has a disclosed
advisory (PYSEC-2026-1845) with a fix in 9.0.3**, outside this
project's current `pytest>=8.0,<9` pin — left as a known, tracked P2
issue rather than silently upgraded, since pytest is a dev-only tool
(never runs against real data or in the shipped application) and a
major-version bump deserves its own dedicated verification pass rather
than a same-session drive-by upgrade. `cryptography` was found on an
older pinned range with several disclosed advisories during this
checkpoint and has already been bumped (`>=46.0.6,<51`) and re-locked —
see [`docs/RELEASE_NOTES.md`](docs/RELEASE_NOTES.md) for the full,
dated finding.

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

Per-user secrets (the Drive/Gmail/Calendar refresh tokens and the
Fathom API key) are never read from `.env` — see
"[Google Drive setup](#google-drive-setup-optional)"
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
│   ├── backup.py               # local backup/restore (online backup API + referenced-evidence copy + manifest)
│   ├── spans.py                # character-span validation (FR-008)
│   ├── chunking.py             # deterministic paragraph/page/turn-boundary chunking
│   ├── credentials/            # OS-keyring-first, encrypted-file-fallback secret storage + connect/refresh/mask/disconnect service
│   ├── db/                    # connection (incl. DatabaseBusyError), migrations, health, maintenance (secure-delete/VACUUM), project_deletion_repository, and one repository per table/domain (projects, audit, sources, evidence, people, observations, ledger, evidence links, proposed mutations, reviews, corrections, briefs, sync), FTS5
│   ├── domain/                # projects, audit, sources, evidence, people, observations, ledger, review, briefs, meeting_prep, sync, email_normalization, calendar_matching, fathom_matching, zoom_hints (enums, models)
│   ├── services/               # projects, project_deletion, evidence, extraction, observations, ledger, reconciliation, review, briefs, brief_shared, meeting_prep, sync orchestration, drive_ingestion, gmail_ingestion, calendar_ingestion, fathom_ingestion, google_connect
│   ├── connectors/             # protocol/errors/http (shared), drive, gmail, calendar, fathom (all implemented), google_oauth
│   ├── parsers/                # txt, md, docx, pdf, vtt + content-first kind detection (no OCR, no transcription)
│   ├── llm/                    # LLMProvider protocol, OpenAI adapter, retry, structured-extraction + brief-composition schemas, prompt loading
│   ├── retrieval/               # deterministic Current Project Brief and Meeting Preparation Brief fact builders (brief_facts.py shared per-fact helpers)
│   ├── evaluation/              # three-project ledger-vs-baseline evaluation harness (Section 13)
│   └── ui/                     # Streamlit pages: projects (incl. Delete), overview, activity/review, ledger, evidence, briefs (Current Project + Meeting Preparation), sources & settings
├── migrations/                # numbered SQLite migrations (see migrations/README.md)
├── prompts/                   # versioned LLM prompt templates (extraction_v1.md, brief_current_v1.md, brief_meeting_prep_v1.md)
├── tests/                     # unit, security (cross-project sentinel suite + log scan), evaluation, fixtures, golden projects
├── scripts/                   # build_benchmark_corpus, run_evaluation, backup, secure_delete_maintenance — see scripts/README.md
├── docs/                      # product plan, ADR.md, RELEASE_NOTES.md, MANUAL_ACCEPTANCE_CHECKLIST.md
└── data/                      # local SQLite DB + evidence store + credentials — gitignored, created at runtime
```

See Section 8 of the product plan for the full architecture rationale.
