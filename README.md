# Project Context

Project Context is a local, single-user, evidence-backed project
intelligence prototype. It turns explicitly bounded emails, calendar
events, meeting transcripts, documents, and manual notes into a
persistent, reviewable project ledger — decisions, commitments,
milestones, risks, blockers, open questions, and stakeholders — where
every claim is traceable to source evidence.

See [`docs/Project_Context_Product_Plan_v1.md`](docs/Project_Context_Product_Plan_v1.md)
for the full product and architecture plan. **Implemented so far:**
configuration, redacted logging, the SQLite schema foundation
(projects/sources/evidence/sync tables), project lifecycle
(create/edit/archive/restore, FR-001), and the full navigation skeleton
— Projects, Project Overview, Activity & Review, Ledger Views, Evidence,
Briefs, and Sources & Settings. The Evidence page is fully built:
manual text entry and file upload (TXT, Markdown, DOCX, PDF, VTT),
content-addressed immutable storage, deterministic parsing and
chunking, FTS5 search, and an evidence viewer with span highlighting
(FR-005 through FR-009). The provider-neutral LLM boundary (Section 12)
is also built: a typed `LLMProvider` protocol, one OpenAI adapter with
centralized retry and one schema-repair attempt, versioned Pydantic
structured-extraction schemas (Section 9), and a manually-triggered
"Extract observations" action on the Evidence page — every candidate
observation is deterministically validated against its cited source
chunk before it is shown as accepted (Section 12.4/12.8). Extraction
output is a proposal only: it is never persisted and never mutates
project state. Every other project-scoped page still shows an honest
"not built in this step" state. The ledger, reconciliation, review,
briefs, and connectors are not implemented yet.

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
- External connectors (Drive, Gmail, Calendar, Fathom) are designed to be
  read-only and are disabled by default; none are implemented yet.
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
pages (most of which are still "not built in this step"). Application
configuration/health is on the Sources & Settings page.

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
starting the application in an unknown state. Secrets (Google OAuth
tokens, Fathom API key) are never read from `.env` — see the product
plan, Section 16, for the credential-storage design. The one documented
exception is the OpenAI API key: the OS-keyring/encrypted-secrets
subsystem Section 16 describes isn't built yet, so extraction reads the
standard `OPENAI_API_KEY` environment variable directly (see
[`.env.example`](.env.example)); without it, "Extract observations"
shows an actionable error instead of failing silently.

## Repository structure

```text
project-context/
├── app.py                    # Streamlit entry point
├── src/project_context/
│   ├── config.py              # typed configuration loading
│   ├── observability.py       # redacted structured logging
│   ├── evidence_store.py      # content-addressed SHA-256 evidence storage
│   ├── spans.py                # character-span validation (FR-008)
│   ├── chunking.py             # deterministic paragraph/page/turn-boundary chunking
│   ├── db/                    # connection, migrations, health, projects/audit/sources/evidence repositories, FTS5
│   ├── domain/                # projects, audit, sources, evidence (enums, models); ledger/transition rules not yet implemented
│   ├── services/               # projects (lifecycle), evidence (manual ingestion), extraction (Section 12); sync, reconciliation, review, briefs not yet implemented
│   ├── connectors/             # manual, drive, gmail, calendar, fathom (not yet implemented)
│   ├── parsers/                # txt, md, docx, pdf, vtt + content-first kind detection
│   ├── llm/                    # LLMProvider protocol, OpenAI adapter, retry, structured-extraction schemas, prompt loading
│   ├── retrieval/               # SQL/FTS queries (not yet implemented)
│   └── ui/                     # Streamlit pages, navigation, project lifecycle + evidence forms + extraction action
├── migrations/                # numbered SQLite migrations (projects/evidence/sync, audit, evidence fields, FTS5)
├── prompts/                   # versioned LLM prompt templates (extraction_v1.md)
├── tests/                     # unit, integration, fixtures, golden projects, prompt regression
├── scripts/                   # seed/evaluate/export/purge utilities (not yet implemented)
└── data/                      # local SQLite DB + evidence store — gitignored, created at runtime
```

See Section 8 of the product plan for the full architecture rationale.
