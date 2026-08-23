"""Content-first evidence file-kind detection.

Section 15 requires that upload validation not "trust extension alone."
Byte signatures are checked first; the filename extension is used only
to disambiguate plain-text formats (TXT vs. Markdown look byte-identical)
and to catch a mismatch — a `.pdf` file whose bytes aren't a PDF is
rejected outright rather than guessed at.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath


class EvidenceKind(StrEnum):
    TXT = "txt"
    MARKDOWN = "markdown"
    DOCX = "docx"
    PDF = "pdf"
    VTT = "vtt"


#: Canonical MIME type recorded for each kind — derived from the
#: detected kind, never trusted verbatim from the browser/caller.
MIME_TYPES: dict[EvidenceKind, str] = {
    EvidenceKind.TXT: "text/plain",
    EvidenceKind.MARKDOWN: "text/markdown",
    EvidenceKind.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    EvidenceKind.PDF: "application/pdf",
    EvidenceKind.VTT: "text/vtt",
}


class UnsupportedEvidenceError(ValueError):
    """Raised when a file's content and/or extension cannot be matched to
    a supported evidence kind, including a content/extension mismatch."""


#: A real .docx is a zip archive containing this internal member; a
#: lightweight, dependency-free way to tell "DOCX" from "some other zip"
#: without fully unzipping.
_DOCX_INTERNAL_MARKER = b"word/document.xml"


def _looks_like_docx(data: bytes) -> bool:
    return _DOCX_INTERNAL_MARKER in data


def _require_decodable_text(data: bytes) -> None:
    try:
        data.decode("utf-8")
        return
    except UnicodeDecodeError:
        pass
    try:
        data.decode("cp1252")
    except UnicodeDecodeError as exc:
        raise UnsupportedEvidenceError(
            "file content could not be decoded as text (tried UTF-8 and cp1252)"
        ) from exc


def detect_kind(*, filename: str, data: bytes) -> EvidenceKind:
    """Detect a supported evidence kind from content first, extension
    second. Raises `UnsupportedEvidenceError` on an unsupported type or
    a content/extension mismatch."""
    ext = PurePosixPath(filename).suffix.lower().lstrip(".")

    if data.startswith(b"%PDF-"):
        if ext not in ("", "pdf"):
            raise UnsupportedEvidenceError(
                f"file content is a PDF but the filename extension is .{ext}"
            )
        return EvidenceKind.PDF

    if data[:4] == b"PK\x03\x04":
        if _looks_like_docx(data):
            if ext not in ("", "docx"):
                raise UnsupportedEvidenceError(
                    f"file content is a DOCX but the filename extension is .{ext}"
                )
            return EvidenceKind.DOCX
        raise UnsupportedEvidenceError("file is a zip archive but not a supported DOCX")

    stripped = data.lstrip(b"\xef\xbb\xbf")  # tolerate a UTF-8 BOM before the WEBVTT marker
    if stripped[:6] == b"WEBVTT":
        if ext not in ("", "vtt"):
            raise UnsupportedEvidenceError(
                f"file content is a WebVTT transcript but the filename extension is .{ext}"
            )
        return EvidenceKind.VTT

    if ext in ("md", "markdown"):
        _require_decodable_text(data)
        return EvidenceKind.MARKDOWN
    if ext in ("txt", ""):
        _require_decodable_text(data)
        return EvidenceKind.TXT
    if ext == "pdf":
        raise UnsupportedEvidenceError("filename claims PDF but content is not a valid PDF")
    if ext == "docx":
        raise UnsupportedEvidenceError("filename claims DOCX but content is not a valid DOCX/zip")
    if ext == "vtt":
        raise UnsupportedEvidenceError("filename claims VTT but content does not start with WEBVTT")
    raise UnsupportedEvidenceError(f"unsupported file type: {filename!r}")
