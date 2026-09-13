"""Guard: the offers status CHECK constraint matches the OfferStatus enum.

Background: migration 048 created ``chk_offer_status`` allowing
``active,expired,won,lost,pending_review,rejected`` — it OMITS the valid
``approved`` and ``sold`` states and carries a phantom ``lost`` that is not in the
enum. Real write paths set offers to APPROVED/SOLD (offer approval, mark-sold), so a
fresh-DB rebuild / backup-restore would re-create the broken constraint and reject
those writes (live works only because the constraint was manually dropped).

Migration 124 fixes the drift: it drops the drifted ``chk_offer_status`` and ensures
``ck_offers_status`` enforces EXACTLY the ``OfferStatus`` enum. This test reads that
migration's text and keeps the constraint and the enum in lock-step so the drift can
never silently return.

Called by: pytest. Depends on: app.constants.OfferStatus + the migration 124 file
text only (no DB).
"""

from __future__ import annotations

import pathlib

from app.constants import OfferStatus

# Migration 212 SUPERSEDES 124's constraint (drop + recreate DERIVED from the enum),
# so the effective fresh-DB shape is 212's. Importing its module-level derivation and
# comparing to the enum catches anyone re-hardcoding the list in a future edit.
_MIG = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions" / "212_worker_singleton_checks.py"


def _ck_offers_status_set() -> set[str]:
    """Return the value set of the ck_offers_status CHECK defined in upgrade()."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("mig212", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {v.strip().strip("'") for v in mod._OFFER_STATUSES.split(",")}


def test_every_offer_status_enum_value_is_permitted():
    allowed = _ck_offers_status_set()
    enum_vals = {s.value for s in OfferStatus}
    missing = enum_vals - allowed
    assert not missing, (
        f"OfferStatus value(s) {sorted(missing)} are not permitted by ck_offers_status "
        "in migration 212 — the constraint would reject valid offer writes on a fresh DB."
    )


def test_constraint_has_no_phantom_values():
    allowed = _ck_offers_status_set()
    enum_vals = {s.value for s in OfferStatus}
    extra = allowed - enum_vals
    assert not extra, (
        f"ck_offers_status permits non-enum value(s) {sorted(extra)} (e.g. the phantom "
        "'lost') — the constraint must match OfferStatus exactly."
    )


def test_downgrade_leaves_earlier_migrations_constraints_in_place():
    """downgrade() must not blanket-drop the six ``_ENSURE`` constraints: each is
    already owned by an earlier migration (023 -> ck_nc_worker_status_singleton,
    031 -> ck_ics_worker_status_singleton, 8c22bd2f6837 -> the other four) — 212 only
    self-heals them when the squash left them missing. Unconditionally dropping them
    on downgrade would remove pre-212 state on whichever DB (fresh vs. pre-squash
    production) already had them from that earlier migration. Only the two
    enum-lagging constraints 212 unconditionally replaces every run
    (ck_buy_plans_status, ck_offers_status) may be dropped+restored here."""
    import ast

    tree = ast.parse(_MIG.read_text(encoding="utf-8"))
    downgrade_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "downgrade")
    dropped_names = {
        node.args[0].value
        for node in ast.walk(downgrade_fn)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "drop_constraint"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }

    ensure_names = {
        "ck_nc_worker_status_singleton",
        "ck_ics_worker_status_singleton",
        "ck_quotes_status",
        "ck_offers_parse_confidence_range",
        "ck_sightings_confidence_range",
        "ck_requirements_target_qty_nonneg",
    }
    assert not (dropped_names & ensure_names), (
        f"downgrade() drops {sorted(dropped_names & ensure_names)}, which are owned by "
        "earlier migrations (023/031/8c22bd2f6837), not by 212"
    )
    assert dropped_names == {"ck_buy_plans_status", "ck_offers_status"}


def test_drifted_chk_offer_status_is_dropped():
    # A 124-specific historical assertion — read THAT migration, not the effective 212.
    mig124 = _MIG.parent / "124_offer_status_constraint.py"
    text = mig124.read_text(encoding="utf-8")
    assert "DROP CONSTRAINT IF EXISTS chk_offer_status" in text, (
        "migration 124 must drop the drifted chk_offer_status constraint (idempotent, "
        "IF EXISTS — it is absent on live but present on a fresh rebuild)."
    )
