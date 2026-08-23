"""Evidence page (Section 6): manual text entry, file upload, an
evidence list, and an evidence viewer with a validated span highlight.

See FR-005 through FR-009. Business logic — validation, ingestion,
parsing, chunking — lives entirely in `project_context.services.
evidence`; this module only collects form input and renders results.
"""

from __future__ import annotations

import html
import sqlite3
from datetime import date, datetime, time

import streamlit as st

from project_context.config import AppConfig, load_config
from project_context.domain.evidence import (
    AUTHOR_MAX_LENGTH,
    EXTERNAL_URL_MAX_LENGTH,
    FAILED_PARSE_STATUSES,
    TITLE_MAX_LENGTH,
    EvidenceSourceType,
    ManualFileUploadInput,
    ManualTextInput,
)
from project_context.services import evidence as evidence_service
from project_context.spans import InvalidSpanError, validate_span
from project_context.ui.db import project_context_connection
from project_context.ui.project_scope import require_selected_project

_SELECTED_ARTIFACT_KEY = "evidence_selected_artifact_id"
_SUPPORTED_FILE_EXTENSIONS = ["txt", "md", "markdown", "docx", "pdf", "vtt"]

_SOURCE_TYPE_LABELS = {
    EvidenceSourceType.MEETING_NOTES: "Meeting notes",
    EvidenceSourceType.EMAIL: "Email",
    EvidenceSourceType.DOCUMENT: "Document",
    EvidenceSourceType.CHAT_TRANSCRIPT: "Chat transcript",
    EvidenceSourceType.CALL_RECORDING: "Call recording",
    EvidenceSourceType.OTHER: "Other",
}
_LABEL_TO_SOURCE_TYPE = {label: kind for kind, label in _SOURCE_TYPE_LABELS.items()}

_PARSE_STATUS_LABELS = {
    "pending": "Pending",
    "parsed": "Parsed",
    "empty": "Empty",
    "ocr_required": "OCR required (scanned document)",
    "unsupported": "Unsupported file",
    "failed": "Failed to parse",
}

_AVAILABILITY_LABELS = {
    "available": "Available",
    "deleted_external": "Deleted externally",
    "inaccessible": "Inaccessible",
    "unassigned": "Unassigned",
    "rejected": "Rejected",
}


_FLASH_MESSAGE_KEY = "evidence_flash_message"


def _set_flash(kind: str, text: str) -> None:
    """Stash a message to show on the *next* run. A message set just
    before `st.rerun()` in the same run would never actually reach the
    user — the rerun interrupts rendering immediately."""
    st.session_state[_FLASH_MESSAGE_KEY] = (kind, text)


def _render_flash() -> None:
    flash = st.session_state.pop(_FLASH_MESSAGE_KEY, None)
    if flash is None:
        return
    kind, text = flash
    getattr(st, kind)(text)


def render() -> None:
    st.title("Evidence")
    config = load_config()
    with project_context_connection() as conn:
        project = require_selected_project(conn)
        if project is None:
            return

        _render_flash()

        text_tab, file_tab = st.tabs(["Add manual text", "Upload file"])
        with text_tab:
            _render_manual_text_form(conn, project.id, config)
        with file_tab:
            _render_file_upload_form(conn, project.id, config)

        st.divider()
        _render_evidence_list(conn, project.id)
        st.divider()
        _render_viewer(conn, project.id)


def _render_manual_text_form(conn: sqlite3.Connection, project_id: str, config: AppConfig) -> None:
    with st.form(key="manual-text-form"):
        title = st.text_input("Title*", max_chars=TITLE_MAX_LENGTH, key="text-title")
        source_type_label = st.selectbox(
            "Source type*", options=list(_SOURCE_TYPE_LABELS.values()), key="text-source-type"
        )
        date_col, time_col = st.columns(2)
        occurred_date = date_col.date_input("Occurred date*", value=date.today(), key="text-date")
        occurred_time = time_col.time_input("Occurred time*", value=time(9, 0), key="text-time")
        author = st.text_input("Author (optional)", max_chars=AUTHOR_MAX_LENGTH, key="text-author")
        external_url = st.text_input(
            "External URL (optional)", max_chars=EXTERNAL_URL_MAX_LENGTH, key="text-url"
        )
        text = st.text_area("Text*", height=240, key="text-body")
        submitted = st.form_submit_button("Add text evidence")

    if not submitted:
        return

    try:
        data = ManualTextInput(
            title=title,
            source_type=_LABEL_TO_SOURCE_TYPE[source_type_label],
            occurred_at=datetime.combine(occurred_date, occurred_time),
            author=author or None,
            external_url=external_url or None,
            text=text,
        )
        result = evidence_service.submit_manual_text(
            conn,
            project_id,
            data,
            evidence_dir=config.evidence_dir,
            chunk_target_chars=config.chunk_target_chars,
            chunk_overlap_ratio=config.chunk_overlap_ratio,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    if result.created_new_version:
        _set_flash("success", f"Saved '{data.title}' as a new evidence version.")
    else:
        _set_flash("info", f"'{data.title}' matches an existing version — no change recorded.")
    st.rerun()


def _render_file_upload_form(conn: sqlite3.Connection, project_id: str, config: AppConfig) -> None:
    max_mb = config.max_upload_bytes / (1024 * 1024)
    with st.form(key="file-upload-form"):
        uploaded = st.file_uploader(
            "File*",
            type=_SUPPORTED_FILE_EXTENSIONS,
            help=f"TXT, Markdown, DOCX, PDF, or VTT. Max size: {max_mb:.0f} MB.",
            key="file-uploader",
        )
        title = st.text_input(
            "Title (defaults to filename)", max_chars=TITLE_MAX_LENGTH, key="file-title"
        )
        source_type_label = st.selectbox(
            "Source type*", options=list(_SOURCE_TYPE_LABELS.values()), key="file-source-type"
        )
        date_col, time_col = st.columns(2)
        occurred_date = date_col.date_input("Occurred date*", value=date.today(), key="file-date")
        occurred_time = time_col.time_input("Occurred time*", value=time(9, 0), key="file-time")
        author = st.text_input("Author (optional)", max_chars=AUTHOR_MAX_LENGTH, key="file-author")
        external_url = st.text_input(
            "External URL (optional)", max_chars=EXTERNAL_URL_MAX_LENGTH, key="file-url"
        )
        submitted = st.form_submit_button("Upload file evidence")

    if not submitted:
        return
    if uploaded is None:
        st.error("Choose a file to upload.")
        return

    try:
        data = ManualFileUploadInput(
            title=title or uploaded.name,
            source_type=_LABEL_TO_SOURCE_TYPE[source_type_label],
            occurred_at=datetime.combine(occurred_date, occurred_time),
            author=author or None,
            external_url=external_url or None,
            filename=uploaded.name,
            declared_mime_type=uploaded.type,
            data=uploaded.getvalue(),
        )
        result = evidence_service.submit_file_upload(
            conn,
            project_id,
            data,
            evidence_dir=config.evidence_dir,
            max_upload_bytes=config.max_upload_bytes,
            chunk_target_chars=config.chunk_target_chars,
            chunk_overlap_ratio=config.chunk_overlap_ratio,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    if result.content.parse_status in FAILED_PARSE_STATUSES:
        label = _PARSE_STATUS_LABELS.get(
            result.content.parse_status.value, result.content.parse_status.value
        )
        _set_flash(
            "warning", f"Uploaded, but parsing reported: {label}. See the Evidence list below."
        )
    elif result.created_new_version:
        _set_flash("success", f"Uploaded '{data.title}' as a new evidence version.")
    else:
        _set_flash("info", f"'{data.title}' matches an existing version — no change recorded.")
    st.rerun()


def _render_evidence_list(conn: sqlite3.Connection, project_id: str) -> None:
    st.subheader("Evidence")
    items = evidence_service.list_evidence(conn, project_id)
    if not items:
        st.info("No evidence yet. Add manual text or upload a file above.", icon="🧭")
        return

    column_widths = [3, 2, 2, 1, 2, 2, 1]
    header_cols = st.columns(column_widths)
    for column, label in zip(
        header_cols,
        ["Title", "Source type", "Occurred", "Ver.", "Parser status", "Availability", ""],
        strict=True,
    ):
        column.markdown(f"**{label}**")

    for item in items:
        cols = st.columns(column_widths)
        cols[0].write(item.artifact.title or "Untitled")
        cols[1].write(
            _SOURCE_TYPE_LABELS.get(item.artifact.source_type, "Not stated")
            if item.artifact.source_type
            else "Not stated"
        )
        cols[2].write(item.artifact.occurred_at or "Not stated")
        cols[3].write(str(item.version_count))
        if item.current_parse_status is None:
            cols[4].write("Not stated")
        else:
            label = _PARSE_STATUS_LABELS.get(
                item.current_parse_status.value, item.current_parse_status.value
            )
            if item.current_parse_status in FAILED_PARSE_STATUSES:
                cols[4].markdown(f":red[{label}]")
            else:
                cols[4].write(label)
        cols[5].write(
            _AVAILABILITY_LABELS.get(
                item.artifact.availability.value, item.artifact.availability.value
            )
        )
        if cols[6].button("View", key=f"view-{item.artifact.id}"):
            st.session_state[_SELECTED_ARTIFACT_KEY] = item.artifact.id
            st.rerun()


def _render_viewer(conn: sqlite3.Connection, project_id: str) -> None:
    artifact_id = st.session_state.get(_SELECTED_ARTIFACT_KEY)
    if not artifact_id:
        return

    detail = evidence_service.get_evidence_detail(conn, project_id, artifact_id)
    if detail is None:
        st.warning("That evidence item no longer exists in this project.")
        return

    st.subheader(f"Viewing: {detail.artifact.title or 'Untitled'}")
    meta_col, content_col = st.columns([1, 2])
    with meta_col:
        source_type = detail.artifact.source_type
        source_type_text = (
            _SOURCE_TYPE_LABELS.get(source_type, "Not stated") if source_type else "Not stated"
        )
        availability_value = detail.artifact.availability.value
        availability_text = _AVAILABILITY_LABELS.get(availability_value, availability_value)
        st.markdown(f"**Source type:** {source_type_text}")
        st.markdown(f"**Occurred:** {detail.artifact.occurred_at or 'Not stated'}")
        st.markdown(f"**Author:** {detail.artifact.author or 'Not stated'}")
        st.markdown(f"**External URL:** {detail.artifact.external_url or 'Not stated'}")
        st.markdown(f"**Availability:** {availability_text}")
        st.markdown(f"**Versions:** {detail.version_count}")

    with content_col:
        if detail.content is None:
            st.info("No content version recorded yet.")
        else:
            st.markdown(f"**Version:** {detail.content.version_key}")
            st.markdown(f"**MIME type:** {detail.content.mime_type or 'Not stated'}")
            if detail.content.original_filename:
                st.markdown(f"**Original filename:** {detail.content.original_filename}")
            status = detail.content.parse_status
            status_text = _PARSE_STATUS_LABELS.get(status.value, status.value)
            if status in FAILED_PARSE_STATUSES:
                st.error(f"Parser status: {status_text}")
                for warning in (detail.content.location_map or {}).get("warnings") or []:
                    st.caption(f"⚠️ {warning}")
            else:
                st.success(f"Parser status: {status_text}")
            st.caption(f"{len(detail.chunks)} chunk(s)")

    if detail.content is None or not detail.content.normalized_text:
        st.info("No text content to display.")
        return

    text = detail.content.normalized_text
    st.markdown("**Highlight a character span** (optional)")
    span_cols = st.columns(2)
    char_start = span_cols[0].number_input(
        "Start", min_value=0, max_value=len(text), value=0, key=f"span-start-{artifact_id}"
    )
    char_end = span_cols[1].number_input(
        "End", min_value=0, max_value=len(text), value=0, key=f"span-end-{artifact_id}"
    )

    st.markdown("**Normalized text**")
    if char_start or char_end:
        try:
            validate_span(text, int(char_start), int(char_end))
        except InvalidSpanError as exc:
            st.error(str(exc))
            st.text(text)
        else:
            _render_highlighted_text(text, int(char_start), int(char_end))
    else:
        st.text(text)


def _render_highlighted_text(text: str, char_start: int, char_end: int) -> None:
    before = html.escape(text[:char_start])
    middle = html.escape(text[char_start:char_end])
    after = html.escape(text[char_end:])
    markup = (
        "<div style='white-space: pre-wrap; font-family: monospace;'>"
        f"{before}<mark>{middle}</mark>{after}"
        "</div>"
    )
    st.markdown(markup, unsafe_allow_html=True)
