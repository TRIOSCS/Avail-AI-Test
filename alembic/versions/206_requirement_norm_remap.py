"""Requirement normalization remap: condition lowercase + normalized_mpn recompute + packaging vocab (data-only).

What (DML only, no schema change — condition is a plain String(50), normalized_mpn a
plain String(255), no CHECK constraints, so no DDL is needed). Row-by-row in Python
(68 rows on the live DB) so the remap is dialect-neutral and uses the EXACT same
transformation the app's pipeline now applies:

  1. requirements.condition -> LOWER(TRIM(condition)) where it differs.
     Remap verified on the live copy (availai-simp-db-1, 2026-08-05): 'New' x28
     -> 'new' (joins 'new' x8 already lowercase; 'used' x1 and NULL x18
     untouched). General rule: pure case-fold — vocabulary re-mapping (e.g.
     'Refurbished' -> 'refurb') is NOT attempted here. NOTE: prod/live DBs do
     NOT have chk_req_condition/chk_req_packaging (verified absent on the live
     copy — only fresh migration-built DBs carry them, NOT VALID), so this
     UPDATE cannot trip a constraint there; on fresh DBs the constraint means
     no case-drifted rows can exist, making pass 1 a no-op.
  2. requirements.normalized_mpn -> canonical key form of primary_mpn
     (lowercase, ALL non-alphanumeric stripped — utils.normalization.normalize_mpn_key),
     where it differs. Remap verified on the live copy (2026-08-05): 28 of the
     50 rows with a primary_mpn held a display-form value (e.g. primary_mpn
     'MAX232CPE+' stored as 'MAX232CPE', expected 'max232cpe') — the
     quick-source display-as-normalized_mpn bug class, which broke
     part-history / material-card joins that assume the key form. Rows with
     NULL primary_mpn (resell excess-mirror virtual requirements) are left
     untouched.
  3. requirements.packaging -> normalize_packaging (frozen copy below) clamped to
     the requirements vocabulary ('reel','tube','tray','bulk','cut_tape' —
     chk_req_packaging, migration 048); unmapped/out-of-vocab -> NULL, matching
     the pipeline's _req_packaging. Remap verified on the live copy
     (2026-08-05): 'tape & reel' x1 -> 'reel', 'yes' x1 -> NULL, NULL x55
     untouched. Without this pass the pre-pipeline display-form rows would sit
     outside the vocabulary every fresh-built DB enforces on new writes.

Why (simplification spec §9, W3): ONE requirement-creation pipeline
(services/requirement_service.py) now writes canonical values on every path; this
remap brings the pre-pipeline rows in line so derived-requisition-status and the
matcher read clean data. Ships in the same PR as the pipeline + the quick-source
key-form idempotency lookup fix (which relies on this remap for pre-existing rows).

Downgrade: documented no-op. Both remaps are many-to-one (original casing /
display-form spellings not recoverable) — mirrors 093/100/189/193/204.

Called by: alembic (upgrade/downgrade).
Depends on: requirements (condition, normalized_mpn, primary_mpn columns).

Revision ID: 206_requirement_norm_remap
Revises: 205_task_status_two_state
Create Date: 2026-08-05
"""

from __future__ import annotations

import re

from sqlalchemy import text

from alembic import op

revision = "206_requirement_norm_remap"
down_revision = "205_task_status_two_state"
branch_labels = None
depends_on = None

# Frozen copy of utils.normalization.normalize_mpn_key's stripping rule so the
# migration is self-contained (app code may drift after this ships).
_NONALNUM_RE = re.compile(r"[^a-z0-9]")


def _mpn_key(raw: str) -> str:
    return _NONALNUM_RE.sub("", raw.strip().lower())


# Frozen copy of utils.normalization._PACKAGING_MAP (substring match, insertion
# order = longest/most-specific first) clamped to the requirements vocabulary,
# mirroring app/services/requirement_service._req_packaging at freeze time.
_PACKAGING_MAP = {
    "tape and reel": "reel",
    "tape & reel": "reel",
    "cut tape": "cut_tape",
    "t&r": "reel",
    "tray": "tray",  # before "tr" — "tr" is a substring of "tray"
    "reel": "reel",
    "tube": "tube",
    "bulk": "bulk",
    "bag": "bag",
    "box": "box",
    "loose": "bulk",
    "each": "each",
    "ea/": "each",
    "ea ": "each",
    "piece": "each",
    "pcs": "each",
    "strip": "strip",
    "ct": "cut_tape",
    "dip": "tube",
    "smd": "reel",
    "tr": "reel",  # "T/R" shorthand — after "tray"
}
_REQ_PACKAGING_VOCAB = frozenset({"reel", "tube", "tray", "bulk", "cut_tape"})


def _req_packaging(raw: str) -> str | None:
    s = raw.strip().lower()
    for pattern, normalized in _PACKAGING_MAP.items():
        if pattern in s:
            return normalized if normalized in _REQ_PACKAGING_VOCAB else None
    return None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        text("SELECT id, primary_mpn, condition, normalized_mpn, packaging FROM requirements")
    ).fetchall()
    for row_id, primary_mpn, condition, normalized_mpn, packaging in rows:
        updates = {}
        if condition is not None:
            lowered = condition.strip().lower()
            if lowered != condition:
                updates["condition"] = lowered or None
        if primary_mpn is not None and primary_mpn.strip():
            expected = _mpn_key(primary_mpn)
            if normalized_mpn != expected:
                updates["normalized_mpn"] = expected
        if packaging is not None:
            canon = _req_packaging(packaging)
            if canon != packaging:
                updates["packaging"] = canon
        if updates:
            sets = ", ".join(f"{col} = :{col}" for col in updates)
            conn.execute(
                text(f"UPDATE requirements SET {sets} WHERE id = :row_id"),
                {**updates, "row_id": row_id},
            )


def downgrade() -> None:
    # Many-to-one remap: the original condition casing, the display-form
    # normalized_mpn spellings, and the raw packaging spellings are not
    # recoverable — documented no-op.
    pass
