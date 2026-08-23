"""Briefs page (Section 6; FR-024; Prompt 9): generate a Current Project
Brief, inspect generation status and developer diagnostics, browse
stored brief history, copy, and download Markdown.

All generation/validation logic lives in `project_context.services.
briefs`; this module only collects input, calls it, and renders the
result (callbacks stay thin, matching every other page in this app).
"""

from __future__ import annotations

import sqlite3

import streamlit as st

from project_context.config import AppConfig, load_config
from project_context.db import brief_repository
from project_context.domain.briefs import (
    BriefStatus,
    BriefType,
    ClaimValidationStatus,
    GeneratedBrief,
)
from project_context.services import briefs as briefs_service
from project_context.services import extraction as extraction_service
from project_context.services.projects import ProjectNotFoundError
from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project

_FLASH_MESSAGE_KEY = "briefs_flash_message"
_SELECTED_BRIEF_KEY = "briefs_selected_brief_id"

_STATUS_RENDERER = {
    BriefStatus.VALID: st.success,
    BriefStatus.FAILED: st.error,
    BriefStatus.SUPERSEDED: st.info,
    BriefStatus.GENERATING: st.info,
}


def _set_flash(kind: str, text: str) -> None:
    st.session_state[_FLASH_MESSAGE_KEY] = (kind, text)


def _render_flash() -> None:
    flash = st.session_state.pop(_FLASH_MESSAGE_KEY, None)
    if flash is None:
        return
    kind, text = flash
    getattr(st, kind)(text)


def render() -> None:
    st.title("Briefs")
    config = load_config()
    with project_context_connection() as conn:
        project = require_selected_project(conn)
        if project is None:
            return

        _render_flash()
        _render_generate_section(conn, project.id, config)
        st.divider()
        _render_history(conn, project.id)


def _render_generate_section(conn: sqlite3.Connection, project_id: str, config: AppConfig) -> None:
    st.subheader("Generate Current Project Brief")
    st.caption(
        "Composed only from accepted ledger state (Section 5.8) — never a fresh search "
        "over raw evidence."
    )
    if not st.button("Generate Current Project Brief", key="generate-brief"):
        return

    provider = extraction_service.build_default_provider()
    if provider is None:
        st.error(
            "No LLM provider is configured. Set the OPENAI_API_KEY environment variable "
            "to enable brief generation."
        )
        return

    with st.spinner("Generating brief…"):
        try:
            result = briefs_service.generate_current_project_brief(
                conn, project_id, provider=provider, model=config.openai_model
            )
        except ProjectNotFoundError as exc:
            st.error(str(exc))
            return

    st.session_state[_SELECTED_BRIEF_KEY] = result.brief.id
    if result.brief.status is BriefStatus.VALID:
        _set_flash("success", "Brief generated.")
    else:
        error_text = result.brief.safe_error or "unknown error"
        _set_flash("error", f"Brief generation failed: {error_text}")
    st.rerun()


def _render_history(conn: sqlite3.Connection, project_id: str) -> None:
    st.subheader("Brief history")
    briefs = brief_repository.list_briefs_for_project(
        conn, project_id, brief_type=BriefType.CURRENT_PROJECT
    )
    if not briefs:
        st.info("No briefs generated yet.", icon="🧭")
        return

    selected_id = st.session_state.get(_SELECTED_BRIEF_KEY)
    options = {f"{b.created_at} · {b.status.value}": b.id for b in briefs}
    labels = list(options.keys())
    default_index = 0
    for index, brief in enumerate(briefs):
        if brief.id == selected_id:
            default_index = index
            break
    chosen_label = st.selectbox(
        "Select a stored brief", options=labels, index=default_index, key="brief-history-select"
    )
    chosen_id = options[chosen_label]
    st.session_state[_SELECTED_BRIEF_KEY] = chosen_id
    _render_selected_brief(conn, project_id, chosen_id)


def _render_developer_detail(
    conn: sqlite3.Connection, project_id: str, brief: GeneratedBrief
) -> None:
    with st.expander("Developer detail (tokens, cost, versions)"):
        st.markdown(
            f"**Model:** {brief.model_id or 'Not stated'} · "
            f"**Prompt version:** {brief.prompt_version or 'Not stated'} · "
            f"**Schema version:** {brief.schema_version or 'Not stated'}"
        )
        cost = brief.estimated_cost_usd or 0.0
        st.markdown(
            f"**Tokens:** {brief.input_tokens or 0} in / {brief.output_tokens or 0} out · "
            f"**Estimated cost:** ${cost:.4f} · **Latency:** {brief.latency_ms or 0} ms"
        )
        st.markdown(f"**Cutoff:** {brief.cutoff_at}")

        claims = brief_repository.list_claims_for_brief(conn, project_id, brief.id)
        omitted = [c for c in claims if c.validation_status is not ClaimValidationStatus.VALID]
        if omitted:
            st.warning(
                f"{len(omitted)} claim(s) generated but omitted from the rendered brief "
                "(unsupported or an invalid reference)."
            )
            for claim in omitted:
                st.caption(f"[{claim.section}] {claim.validation_status.value}: {claim.claim_text}")
        else:
            st.caption("Every generated claim was validated and included.")


def _render_selected_brief(conn: sqlite3.Connection, project_id: str, brief_id: str) -> None:
    brief = brief_repository.get_brief(conn, project_id, brief_id)
    if brief is None:
        st.warning("That brief no longer exists.")
        return

    renderer = _STATUS_RENDERER.get(brief.status, st.info)
    renderer(f"Status: {brief.status.value}")
    if brief.safe_error:
        st.caption(f"Error: {brief.safe_error}")

    _render_developer_detail(conn, project_id, brief)

    if not brief.markdown:
        st.caption("No Markdown available for this brief.")
        return

    st.markdown(brief.markdown)
    st.caption("Copy from the code block below (hover for the copy icon), or download as Markdown.")
    st.code(brief.markdown, language="markdown")
    st.download_button(
        "Download Markdown",
        data=brief.markdown,
        file_name=f"current-project-brief-{brief.created_at[:10]}.md",
        mime="text/markdown",
        key=f"download-{brief_id}",
    )
