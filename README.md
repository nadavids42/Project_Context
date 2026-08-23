# Project Context

Project Context is a local, single-user, evidence-backed project
intelligence prototype. It turns explicitly bounded emails, calendar
events, meeting transcripts, documents, and manual notes into a
persistent, reviewable project ledger — decisions, commitments,
milestones, risks, blockers, open questions, and stakeholders — where
every claim is traceable to source evidence.

See [`docs/Project_Context_Product_Plan_v1.md`](docs/Project_Context_Product_Plan_v1.md)
for the full product and architecture plan. **This repository is at the
bootstrap stage**: configuration, redacted logging, and the application
shell only. No project data, database schema, ledger, connectors, or LLM
calls are implemented yet.

## ⚠️ Privacy and data policy

This is a local, single-user prototype — not a secured, multi-tenant
product.

- **Use only synthetic, personal, public, or explicitly authorized data.**
  Never use employer or customer data unless its owner has explicitly
  permitted it in writing.
- Source content is stored on this device. Once extraction is
  implemented, selected text will be sent to the configured LLM provider
  for processing — this build sends nothing anywhere.
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

Opens the local application shell with a privacy notice and a
configuration/health panel. No project functionality exists yet.

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
starting the application in an unknown state. Secrets (LLM API key,
Google OAuth tokens, Fathom API key) are never read from `.env` — see the
product plan, Section 16, for the credential-storage design.

## Repository structure

```text
project-context/
├── app.py                    # Streamlit entry point
├── src/project_context/
│   ├── config.py              # typed configuration loading
│   ├── observability.py       # redacted structured logging
│   ├── db/                    # connection, migrations, repositories, FTS (not yet implemented)
│   ├── domain/                # enums, models, transition rules (not yet implemented)
│   ├── services/               # sync, extraction, reconciliation, review, briefs (not yet implemented)
│   ├── connectors/             # manual, drive, gmail, calendar, fathom (not yet implemented)
│   ├── parsers/                # txt, md, docx, pdf, vtt (not yet implemented)
│   ├── llm/                    # provider protocol, OpenAI adapter, prompts (not yet implemented)
│   ├── retrieval/               # SQL/FTS queries (not yet implemented)
│   └── ui/                     # Streamlit pages/components (not yet implemented)
├── migrations/                # numbered SQLite migrations (not yet implemented)
├── prompts/                   # versioned LLM prompt templates (not yet implemented)
├── tests/                     # unit, integration, fixtures, golden projects, prompt regression
├── scripts/                   # seed/evaluate/export/purge utilities (not yet implemented)
└── data/                      # local SQLite DB + evidence store — gitignored, created at runtime
```

See Section 8 of the product plan for the full architecture rationale.
