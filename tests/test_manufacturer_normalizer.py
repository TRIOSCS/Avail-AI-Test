"""tests/test_manufacturer_normalizer.py — brand/maker normalization + gating constants.

Covers: app/services/manufacturer_normalizer.py — normalize_brand_name (alias hit →
canonical, miss → verbatim strip, composite makers verbatim), OEM_BRANDS membership,
and the two trailing-token regex gates (accept EXACTLY the literal lists, reject
anything else). Under TESTING=1 the per-process cache reloads per call, so each test's
isolated SQLite DB is honored.

Called by: pytest
Depends on: conftest.py (db_session), app.models.Manufacturer
"""

import json

from sqlalchemy.orm import Session

from app.models import Manufacturer
from app.services.manufacturer_normalizer import (
    MAKER_TRAILING_RE,
    OEM_BRANDS,
    OEM_TRAILING_RE,
    normalize_brand_name,
)


def _seed(db: Session, *rows: tuple[str, list[str]]) -> None:
    for canonical, aliases in rows:
        db.add(Manufacturer(canonical_name=canonical, aliases=aliases))
    db.flush()


# --- normalize_brand_name -----------------------------------------------------


def test_alias_hit_returns_canonical(db_session: Session):
    _seed(db_session, ("Hewlett Packard Enterprise", ["HPE", "HP"]))
    assert normalize_brand_name(db_session, "HP") == "Hewlett Packard Enterprise"
    assert normalize_brand_name(db_session, "hpe") == "Hewlett Packard Enterprise"


def test_alias_hit_is_case_insensitive(db_session: Session):
    _seed(db_session, ("Seagate Technology", ["Seagate"]))
    assert normalize_brand_name(db_session, "SEAGATE") == "Seagate Technology"
    assert normalize_brand_name(db_session, "seagate technology") == "Seagate Technology"


def test_canonical_name_maps_to_itself(db_session: Session):
    _seed(db_session, ("IBM", []))
    assert normalize_brand_name(db_session, "ibm") == "IBM"


def test_miss_returns_verbatim_stripped(db_session: Session):
    _seed(db_session, ("IBM", []))
    # Inventing a canonicalization for an unknown name would be a guess.
    assert normalize_brand_name(db_session, "  Frobozz Magic Drives  ") == "Frobozz Magic Drives"


def test_composite_maker_hitachi_ibm_stays_verbatim(db_session: Session):
    # `Hitachi/IBM` from fru_links is its own truthful facet value — never split, even
    # with both component names seeded.
    _seed(db_session, ("Hitachi", []), ("IBM", []))
    assert normalize_brand_name(db_session, "Hitachi/IBM") == "Hitachi/IBM"


def test_canonical_wins_alias_collision(db_session: Session):
    # "Toshiba" is BOTH an alias of "Toshiba Electronic Devices" and its own canonical
    # row (dual-brand seed) — the canonical must win the lookup-map collision.
    _seed(db_session, ("Toshiba Electronic Devices", ["Toshiba"]), ("Toshiba", []))
    assert normalize_brand_name(db_session, "toshiba") == "Toshiba"


def test_no_session_returns_verbatim_strip():
    assert normalize_brand_name(None, " IBM ") == "IBM"


def test_aliases_stored_as_json_text_still_resolve(db_session: Session):
    # The startup seed writes aliases via json.dumps into a JSON column — the ORM round-
    # trips it as a list; guard the loader against a row seeded with an empty list too.
    db_session.add(Manufacturer(canonical_name="Western Digital", aliases=json.loads('["WD"]')))
    db_session.flush()
    assert normalize_brand_name(db_session, "wd") == "Western Digital"


# --- gating constants ----------------------------------------------------------


def test_oem_brands_is_the_literal_label_set():
    assert OEM_BRANDS == {"ibm", "dell", "hp", "hpe", "hewlett packard enterprise", "lenovo"}


def test_oem_trailing_re_accepts_exactly_the_literal_list():
    for token in ("IBM", "Dell", "HP", "HPE", "Lenovo", "ibm", "LENOVO"):
        m = OEM_TRAILING_RE.search(f'HDD, 300GB, 2.5" SED, 15K RPM, {token}')
        assert m is not None, token
        assert m.group(1).lower() == token.lower()


def test_oem_trailing_re_rejects_non_oem_and_non_trailing():
    assert OEM_TRAILING_RE.search("HDD, 300GB, Foobar") is None
    assert OEM_TRAILING_RE.search("HDD, 300GB, Seagate") is None  # maker, not OEM label
    assert OEM_TRAILING_RE.search("IBM, 300GB HDD") is None  # not trailing
    assert OEM_TRAILING_RE.search("HDD 300GB IBM") is None  # no comma delimiter


def test_maker_trailing_re_accepts_exactly_the_literal_list():
    for token in ("Seagate", "Kingston", "Samsung", "SEAGATE"):
        m = MAKER_TRAILING_RE.search(f"HDD, 4TB 7.2K, {token}")
        assert m is not None, token
        assert m.group(1).lower() == token.lower()


def test_maker_trailing_re_rejects_everything_else():
    assert MAKER_TRAILING_RE.search("HDD, 4TB, Foobar") is None
    assert MAKER_TRAILING_RE.search("HDD, 4TB, IBM") is None  # OEM label, not maker
    assert MAKER_TRAILING_RE.search("Seagate, 4TB HDD") is None  # not trailing


# --- Per-process memoization (the NON-TESTING path) -----------------------------
# These tests drop TESTING from the environment (monkeypatch auto-restores) and reset
# the module cache, so they exercise the real memoization branch the worker/CLI runs.


def test_empty_alias_map_is_never_memoized(db_session: Session, monkeypatch):
    # Pre-seed race: the enrichment worker's first decode can run before the app
    # container's _seed_manufacturers lands. An EMPTY load must be a cache MISS — not
    # frozen for the process lifetime (which would split the facet into "Kingston" and
    # "Kingston Technology" until a restart).
    import app.services.manufacturer_normalizer as mod

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(mod, "_canonical_by_lower", None)

    assert normalize_brand_name(db_session, "Kingston") == "Kingston"  # table empty → verbatim
    assert mod._canonical_by_lower is None  # the empty result was NOT memoized

    _seed(db_session, ("Kingston Technology", ["Kingston"]))
    # Self-healed: the next call reloads and sees the seeds.
    assert normalize_brand_name(db_session, "Kingston") == "Kingston Technology"
    assert mod._canonical_by_lower is not None


def test_populated_alias_map_is_memoized(db_session: Session, monkeypatch):
    # Steady state: a NON-EMPTY map is cached forever (table is seed-only; restart
    # refreshes) — rows added after the first load are invisible until then.
    import app.services.manufacturer_normalizer as mod

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(mod, "_canonical_by_lower", None)

    _seed(db_session, ("Kingston Technology", ["Kingston"]))
    assert normalize_brand_name(db_session, "Kingston") == "Kingston Technology"
    assert mod._canonical_by_lower is not None

    _seed(db_session, ("Seagate Technology", ["Seagate"]))
    # Memoized: the post-load row is NOT visible (documented restart-refreshes contract).
    assert normalize_brand_name(db_session, "Seagate") == "Seagate"
