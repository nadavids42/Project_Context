"""Centralized, time-sortable identifier creation.

Every application-owned entity ID is a ULID (Crockford base32, 26
characters, lexicographically sortable by creation time) rendered as
plain text — never a raw SQLite `rowid`. All ID generation goes through
this module so the format can be audited or changed in one place. See
docs/Project_Context_Product_Plan_v1.md Section 9 ("Modeling rules").
"""

from __future__ import annotations

from ulid import ULID


def new_id() -> str:
    """Return a new, time-sortable, globally unique text identifier."""
    return str(ULID())
