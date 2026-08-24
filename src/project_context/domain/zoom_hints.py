"""Conservative Zoom-filename recognition hints (Section 11.6; Prompt
13: "Add conservative filename/metadata hints, but do not assign solely
from a filename").

This module returns **advisory display text only** — a caption the
evidence viewer can show next to a file's name (`ui.pages.evidence`
already renders `original_filename`; this adds one optional line under
it). It has no return type that could be mistaken for a project
assignment, no database access, and nothing here is ever consulted by
`project_context.services.drive_ingestion` or any assignment path.
Project assignment for every file arriving through Drive remains
exactly what it has always been: whichever project's Drive source has
that file inside its own configured folder (Section 11.2/11.6:
"Dedicated project folder or intake queue; filename rules are hints,
not proof"). A file named to look exactly like a Zoom export that lands
in the wrong project's folder is still evidence for *that* project —
this module only ever adds a caption, never a decision.

Patterns recognized are Zoom's own documented default export names
(cloud-recording downloads and, separately, an AI Companion meeting-
summary export saved manually to Drive) — not exhaustive, and
deliberately conservative: an unrecognized filename returns `None`
rather than guessing.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

#: Zoom's own default cloud-recording filename prefix: `GMT` + a
#: `YYYYMMDD-HHMMSS` timestamp, e.g. `GMT20260601-140000_Recording...`.
_GMT_PREFIX_RE = re.compile(r"^GMT\d{8}-\d{6}_", re.IGNORECASE)

#: Zoom's default in-meeting-chat export suffix.
_CHAT_SUFFIX_RE = re.compile(r"\.chat\.txt$", re.IGNORECASE)

#: Zoom's default audio-transcript (closed caption) export suffix, and
#: the more generic "...Transcript.vtt" naming some Zoom AI Companion
#: exports use instead.
_TRANSCRIPT_SUFFIX_RE = re.compile(r"(\.transcript\.vtt|transcript\.vtt)$", re.IGNORECASE)

#: Zoom AI Companion's manually-saved meeting-summary export — no fixed
#: filename standard, so this only fires on the word "summary" itself,
#: combined with a document extension, to stay conservative.
_SUMMARY_NAME_RE = re.compile(r"summary", re.IGNORECASE)
_SUMMARY_EXTENSIONS = frozenset({"docx", "txt", "md", "pdf"})


def describe_filename_hint(filename: str) -> str | None:
    """One short, human-readable hint string, or `None` if `filename`
    matches none of the recognized Zoom export patterns. Never raises;
    never inspects file content — content-based kind detection remains
    `project_context.parsers.kinds.detect_kind`'s job, entirely
    independent of this function."""
    name = PurePosixPath(filename).name
    ext = PurePosixPath(name).suffix.lower().lstrip(".")

    if _GMT_PREFIX_RE.match(name) and ext == "vtt":
        return "Looks like a Zoom cloud-recording transcript (GMT-prefixed filename)."
    if _TRANSCRIPT_SUFFIX_RE.search(name):
        return "Looks like a Zoom-generated transcript export."
    if _CHAT_SUFFIX_RE.search(name):
        return "Looks like a Zoom in-meeting chat export."
    if _GMT_PREFIX_RE.match(name):
        return "Looks like a Zoom cloud-recording export (GMT-prefixed filename)."
    if _SUMMARY_NAME_RE.search(name) and ext in _SUMMARY_EXTENSIONS:
        return "Looks like a meeting-summary document (e.g. a Zoom AI Companion summary export)."
    return None
