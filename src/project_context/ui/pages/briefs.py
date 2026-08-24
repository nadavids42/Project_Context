"""Briefs page (Section 6; FR-024/FR-025; Prompt 9, Prompt 14): generate
a Current Project Brief or a Meeting Preparation Brief, inspect
generation status and developer diagnostics, browse stored brief
history per type, copy, and download Markdown.

All generation/validation logic lives in `project_context.services.
briefs`/`project_context.services.meeting_prep`; this module only
collects input, calls it, and renders the result (callbacks stay thin,
matching every other page in this app).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time

import streamlit as st

from project_context.config import AppConfig, load_config
from project_context.db import brief_repository, sources_repository
from project_context.domain.briefs import (
    BriefStatus,
    BriefType,
    ClaimValidationStatus,
    GeneratedBrief,
)
from project_context.domain.meeting_prep import MeetingPrepBriefFacts
from project_context.domain.sources import SourceHealthStatus
from project_context.retrieval.meeting_prep import (
    MeetingArtifactNotFoundError,
    build_meeting_prep_facts,
    list_meeting_candidates,
)
from project_context.services import briefs as briefs_service
from project_context.services import extraction as extraction_service
from project_context.services import meeting_prep as meeting_prep_service
from project_context.services.projects import ProjectNotFoundError
from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project

_FLASH_MESSAGE_KEY = "briefs_flash_message"
_SELECTED_BRIEF_KEY = "briefs_selected_brief_id"

_MEETING_PREP_MODE_KEY = "meeting_prep_selection_mode"
_MEETING_PREP_PREVIEW_KEY = "meeting_prep_preview_facts"
_MEETING_PREP_SELECTED_BRIEF_KEY = "meeting_prep_selected_brief_id"

_STATUS_RENDERER = {
    BriefStatus.VALID: st.success,
    BriefStatus.FAILED: st.error,
    BriefStatus.SUPERSEDED: st.info,
    BriefStatus.GENERATING: st.info,
}

#: Sections offered for include/exclude — every fact-bearing section
#: except the always-deterministic `meeting_purpose` and the always-
#: empty `suggested_topics` (Section 5.9: "User may include/exclude
#: items before generation").
_EXCLUDABLE_SECTIONS = (
    "changes_since_previous",
    "outstanding_commitments",
    "decisions_required",
    "risks_and_blockers",
    "unanswered_questions",
)

#: A source this stale/unhealthy is worth flagging before generating a
#: brief from its evidence (Prompt 14: "Display stale sync/evidence
#: warning before generation when relevant").
_STALE_HEALTH_STATUSES = (SourceHealthStatus.DEGRADED, SourceHealthStatus.REAUTH_REQUIRED)


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
        _render_history(
            conn,
            project.id,
            brief_type=BriefType.CURRENT_PROJECT,
            heading="Current Project Brief history",
            selected_key=_SELECTED_BRIEF_KEY,
            filename_prefix="current-project-brief",
        )
        st.divider()
        _render_meeting_prep_section(conn, project.id, config)
        st.divider()
        _render_history(
            conn,
            project.id,
            brief_type=BriefType.MEETING_PREPARATION,
            heading="Meeting Preparation Brief history",
            selected_key=_MEETING_PREP_SELECTED_BRIEF_KEY,
            filename_prefix="meeting-prep-brief",
        )


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


# ---------------------------------------------------------------------------
# Meeting Preparation Brief (Prompt 14).
# ---------------------------------------------------------------------------


def _render_stale_source_warning(conn: sqlite3.Connection, project_id: str) -> None:
    sources = [
        s for s in sources_repository.list_sources_for_project(conn, project_id) if s.enabled
    ]
    if not sources:
        return
    stale = []
    for source in sources:
        if source.last_success_at is None:
            stale.append(f"{source.kind.value} ({source.display_name}): never synced successfully")
        elif source.health_status in _STALE_HEALTH_STATUSES:
            stale.append(
                f"{source.kind.value} ({source.display_name}): {source.health_status.value}"
            )
    if stale:
        st.warning(
            "Some sources may be stale or unhealthy — this brief may be missing recent "
            "evidence:\n" + "\n".join(f"- {line}" for line in stale)
        )


def _render_meeting_selection_form(conn: sqlite3.Connection, project_id: str) -> None:
    candidates = list_meeting_candidates(conn, project_id)
    mode_options = (
        ["Select an existing meeting", "Enter manually"] if candidates else ["Enter manually"]
    )
    mode = st.radio(
        "Meeting source", options=mode_options, key=_MEETING_PREP_MODE_KEY, horizontal=True
    )

    with st.form("meeting-prep-selection-form"):
        selected_artifact_id: str | None = None
        manual_title = manual_purpose = None
        manual_scheduled_at: str | None = None
        participants_text = ""

        if mode == "Select an existing meeting":
            options = {f"{c.title} — {c.occurred_at or 'unknown time'}": c.id for c in candidates}
            chosen_label = st.selectbox("Meeting", options=list(options.keys()))
            selected_artifact_id = options[chosen_label]
            manual_purpose = st.text_input(
                "Purpose override (optional)",
                help="Leave blank to leave the purpose unstated for this meeting.",
            )
        else:
            manual_title = st.text_input("Meeting title*")
            manual_purpose = st.text_input("Purpose (optional)")
            date_col, time_col = st.columns(2)
            scheduled_date = date_col.date_input("Scheduled date*", value=date.today())
            scheduled_time = time_col.time_input("Scheduled time*", value=time(9, 0))
            manual_scheduled_at = datetime.combine(scheduled_date, scheduled_time).isoformat() + "Z"

        participants_text = st.text_area(
            'Participants (one per line: "Name <email>", an email, or a name)',
            help="Resolved by exact email first, then a known alias, then left explicitly "
            "unresolved — never guessed.",
        )
        cutoff_override = st.text_input(
            "Cutoff override (optional, ISO 8601)",
            help="Leave blank to use the automatically determined previous-meeting cutoff, "
            "shown below after Preview.",
        )
        preview_clicked = st.form_submit_button("Preview meeting & facts")

    if not preview_clicked:
        return

    participant_lines = tuple(line for line in participants_text.splitlines() if line.strip())
    try:
        facts = build_meeting_prep_facts(
            conn,
            project_id,
            meeting_artifact_id=selected_artifact_id
            if mode == "Select an existing meeting"
            else None,
            manual_title=manual_title if mode != "Select an existing meeting" else None,
            manual_purpose=manual_purpose or None,
            manual_scheduled_at=manual_scheduled_at,
            participant_lines=participant_lines,
            cutoff_override=cutoff_override.strip() or None,
        )
    except MeetingArtifactNotFoundError as exc:
        st.error(str(exc))
        return
    except ValueError as exc:
        st.error(f"Could not build the meeting preview: {exc}")
        return

    st.session_state[_MEETING_PREP_PREVIEW_KEY] = facts
    st.rerun()


_RESOLUTION_BADGE = {"resolved": "✅", "ambiguous": "⚠️ ambiguous", "unknown": "❓ unresolved"}


def _render_meeting_preview(
    conn: sqlite3.Connection, project_id: str, facts: MeetingPrepBriefFacts
) -> None:
    meeting = facts.meeting
    st.markdown(f"**Meeting:** {meeting.title}")
    st.markdown(f"**Purpose:** {meeting.purpose or 'Not stated'}")
    st.markdown(f"**Scheduled:** {meeting.scheduled_at or 'Not stated'}")
    if meeting.participants:
        for participant in meeting.participants:
            badge = _RESOLUTION_BADGE.get(participant.resolution.outcome, "")
            st.caption(f"{participant.display_name} {badge}")
    else:
        st.caption("No participants entered.")
    if facts.previous_meeting_artifact_id:
        st.markdown(
            f"**Changes since:** {facts.cutoff_at} "
            f"([previous meeting](evidence?artifact_id={facts.previous_meeting_artifact_id}))"
        )
    else:
        st.markdown(
            f"**Changes since:** {facts.cutoff_at} (project start — no earlier meeting found)"
        )

    section_by_key = {s.section: s for s in facts.sections}
    excluded: set[str] = set()
    for key in _EXCLUDABLE_SECTIONS:
        section = section_by_key.get(key)
        if section is None or not section.facts:
            continue
        with st.expander(f"{section.heading} ({len(section.facts)})", expanded=True):
            for fact in section.facts:
                label = fact.title
                if fact.owner_name:
                    label += f" — {fact.owner_name}"
                if fact.due_date:
                    label += f" (due {fact.due_date})"
                included = st.checkbox(
                    label, value=True, key=f"meeting-prep-include-{fact.fact_id}"
                )
                if not included:
                    excluded.add(fact.fact_id)

    if st.button("Generate Meeting Preparation Brief", key="generate-meeting-prep-brief"):
        provider = extraction_service.build_default_provider()
        if provider is None:
            st.error(
                "No LLM provider is configured. Set the OPENAI_API_KEY environment variable "
                "to enable brief generation."
            )
            return
        config = load_config()
        with st.spinner("Generating meeting preparation brief…"):
            result = meeting_prep_service.generate_meeting_prep_brief(
                conn,
                project_id,
                facts=facts,
                excluded_fact_ids=frozenset(excluded),
                provider=provider,
                model=config.openai_model,
            )
        st.session_state[_MEETING_PREP_SELECTED_BRIEF_KEY] = result.brief.id
        st.session_state.pop(_MEETING_PREP_PREVIEW_KEY, None)
        if result.brief.status is BriefStatus.VALID:
            _set_flash("success", "Meeting preparation brief generated.")
        else:
            error_text = result.brief.safe_error or "unknown error"
            _set_flash("error", f"Brief generation failed: {error_text}")
        st.rerun()


def _render_meeting_prep_section(
    conn: sqlite3.Connection, project_id: str, config: AppConfig
) -> None:
    st.subheader("Generate Meeting Preparation Brief")
    st.caption(
        "Select an imported Calendar/Fathom meeting, or enter one manually — Calendar is "
        "never required (Section 5.9)."
    )
    _render_stale_source_warning(conn, project_id)
    _render_meeting_selection_form(conn, project_id)

    preview = st.session_state.get(_MEETING_PREP_PREVIEW_KEY)
    if preview is not None:
        _render_meeting_preview(conn, project_id, preview)


# ---------------------------------------------------------------------------
# Shared history/detail rendering (both brief types).
# ---------------------------------------------------------------------------


def _render_history(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    brief_type: BriefType,
    heading: str,
    selected_key: str,
    filename_prefix: str,
) -> None:
    st.subheader(heading)
    briefs = brief_repository.list_briefs_for_project(conn, project_id, brief_type=brief_type)
    if not briefs:
        st.info("No briefs generated yet.", icon="🧭")
        return

    selected_id = st.session_state.get(selected_key)
    options = {f"{b.created_at} · {b.status.value}": b.id for b in briefs}
    labels = list(options.keys())
    default_index = 0
    for index, brief in enumerate(briefs):
        if brief.id == selected_id:
            default_index = index
            break
    chosen_label = st.selectbox(
        "Select a stored brief", options=labels, index=default_index, key=f"{selected_key}-select"
    )
    chosen_id = options[chosen_label]
    st.session_state[selected_key] = chosen_id
    _render_selected_brief(conn, project_id, chosen_id, filename_prefix=filename_prefix)


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


def _render_selected_brief(
    conn: sqlite3.Connection, project_id: str, brief_id: str, *, filename_prefix: str
) -> None:
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
        file_name=f"{filename_prefix}-{brief.created_at[:10]}.md",
        mime="text/markdown",
        key=f"download-{brief_id}",
    )
