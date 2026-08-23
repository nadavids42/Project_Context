# Prompts

Versioned extraction and brief-generation prompt templates (Stage A
extraction, Stage B optional adjudication, Stage C brief composition —
see docs/Project_Context_Product_Plan_v1.md Section 12.3).

| File | Stage | Loaded by | Schema |
|---|---|---|---|
| `extraction_v1.md` | A — atomic observation extraction | `project_context.llm.prompts.load_extraction_system_prompt` | `project_context.llm.schemas.ExtractionBatch` |
| `brief_current_v1.md` | C — Current Project Brief composition | `project_context.llm.prompts.load_brief_system_prompt` | `project_context.llm.schemas.BriefComposition` |

Stage B (optional candidate adjudication) is not implemented — Section
12.3 keeps it disabled initially; deterministic reconciliation
(`project_context.domain.reconciliation`) owns matching instead.

A wording change that could shift model behavior belongs in a new
`_vN.md` file plus a bumped version constant (`PROMPT_VERSION` /
`BRIEF_PROMPT_VERSION`), never an edit in place — regression fixtures and
stored generation records stay pinned to what actually produced them
(Section 12.4).
