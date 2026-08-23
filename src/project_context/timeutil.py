"""Shared UTC timestamp helper.

Every stored timestamp is UTC ISO 8601 text, produced only through this
module so formatting stays consistent everywhere it is written. See
docs/Project_Context_Product_Plan_v1.md Section 9 ("Modeling rules"):
"Store all timestamps in UTC ISO 8601; retain original timezone/text
where interpretation matters" (the latter is a per-field concern for
callers dealing with source-provided dates, e.g. `date_text` fields —
not something this helper needs to know about).
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a `Z` suffix."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
