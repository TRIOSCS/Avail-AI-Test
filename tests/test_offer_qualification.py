# tests/test_offer_qualification.py
# What: pure-function tests for the offer qualification service.
# Called by: pytest. Depends on: app.services.offer_qualification (no DB for these).
import pytest

from app.constants import OfferCondition
from app.services.offer_qualification import (
    apply_qualification,
    compose_note,
    compute_status,
    essentials_data,
    meter,
    normalize_offer_condition,
    request_template,
    validate_essentials,
)


class TestOfferConditionEnumSites:
    """P2.5: validate_essentials (line ~133), compose_note (line ~196), and _items_for
    (line ~235) compare against OfferCondition.NEW rather than a raw "new" literal.

    A caller passing the enum member or the raw string must behave identically (StrEnum
    equality).
    """

    def test_validate_essentials_accepts_enum_member(self):
        assert validate_essentials(OfferCondition.NEW, {"manufacturer": ""})
        assert validate_essentials(OfferCondition.NEW, {"manufacturer": "TI"}) == []

    def test_compose_note_accepts_enum_member(self):
        assert compose_note(OfferCondition.NEW, {}) == "New — parts are in the original manufacturer's packaging."

    def test_items_for_via_meter_accepts_enum_member(self):
        data = {"manufacturer": "TI", "packaging": "Trays", "date_code": "2501"}
        assert meter(OfferCondition.NEW, data, has_images=False) == (3, 3)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("used", "pulls"),
        ("Used", "pulls"),
        ("pulled", "pulls"),
        ("refurbished", "refurb"),
        ("recertified", "refurb"),
        ("new", "new"),
        ("new_no_pkg", "new_no_pkg"),
        ("new no pkg", "new_no_pkg"),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_normalize_offer_condition(raw, expected):
    assert normalize_offer_condition(raw) == expected


def test_note_new():
    assert compose_note("new", {}) == "New — parts are in the original manufacturer's packaging."


def test_note_new_no_pkg():
    assert (
        compose_note("new_no_pkg", {"packaging": "Trays"})
        == "New, no original manufacturer packaging. Packaged in Trays."
    )


def test_note_pulls_full():
    note = compose_note("pulls", {"packaging": "Trays", "usage": "systems", "part_condition": "Light wear"})
    assert note == "Pulls — packaged in Trays, pulled from systems. Condition: Light wear."


def test_note_refurb_third_party_with_cert():
    note = compose_note(
        "refurb", {"refurbished_by": "third_party", "refurb_process": "Reballed BGA", "cert_doc": "yes"}
    )
    assert note == "Refurbished by a third party. Process: Reballed BGA. Certifying document on file."


def test_validate_blocks_bulk_for_pulls():
    errs = validate_essentials("pulls", {"packaging": "bulk", "usage": "boards"})
    assert errs and any("bulk" in e.lower() for e in errs)


def test_validate_pulls_requires_usage():
    errs = validate_essentials("pulls", {"packaging": "Trays"})
    assert any("usage" in e.lower() for e in errs)


def test_validate_new_no_essential_blocks_missing_manufacturer():
    assert validate_essentials("new", {"manufacturer": ""})
    assert validate_essentials("new", {"manufacturer": "TI"}) == []


def test_validate_refurb_requires_by_and_process():
    assert validate_essentials("refurb", {"refurbished_by": "supplier"})  # missing process
    assert validate_essentials("refurb", {"refurbished_by": "supplier", "refurb_process": "Cleaned"}) == []


def test_unset_condition_allowed_and_unset_status():
    assert validate_essentials(None, {}) == []
    assert compute_status(None, {}, has_images=False) == "unset"


def test_status_and_meter_pulls():
    data = {"packaging": "Trays", "usage": "systems", "part_condition": "Clean"}
    assert meter("pulls", data, has_images=True) == (4, 4)
    assert compute_status("pulls", data, has_images=True) == "complete"
    assert compute_status("pulls", data, has_images=False) == "essentials"  # images missing


def test_refurb_meter_excludes_cert_for_supplier():
    data = {"refurbished_by": "supplier", "refurb_process": "Cleaned"}
    assert meter("refurb", data, has_images=False) == (2, 3)  # by, process, images(0); no cert item


def test_request_template():
    assert "{mpn}" not in request_template("images", "LM317T")
    with pytest.raises(ValueError):
        request_template("nope", "X")


def test_apply_qualification_incomplete_on_missing_essential():
    # The canonical builder never blocks: a missing essential yields status
    # "incomplete" (the gate lives in the buyer handlers), and a note is still composed.
    class _O:
        condition = "pulls"
        packaging = "Trays"
        manufacturer = None
        date_code = None
        qualification = {}  # no usage
        attachments = []
        qualification_note = None
        qualification_status = None

    o = _O()
    apply_qualification(o)
    assert o.qualification_status == "incomplete"
    assert o.qualification_note == "Pulls — packaged in Trays."


def test_apply_qualification_sets_note_and_status():
    class _O:
        condition = "new"
        packaging = None
        manufacturer = "TI"
        date_code = None
        qualification = {}
        attachments = []
        qualification_note = None
        qualification_status = None

    o = _O()
    apply_qualification(o)
    assert o.qualification_note.startswith("New — parts are in the original")
    assert o.qualification_status in ("essentials", "complete")


# ── FIX A: broad-synonym fallback in normalize_offer_condition ────────────────


def test_normalize_offer_condition_factory_new():
    assert normalize_offer_condition("Factory New") == "new"


def test_normalize_offer_condition_brand_new():
    assert normalize_offer_condition("Brand New") == "new"


def test_normalize_offer_condition_used_synonym_to_pulls():
    # "surplus" maps to "used" via the broad normalizer → "pulls" in offer vocab
    assert normalize_offer_condition("surplus") == "pulls"


def test_normalize_offer_condition_junk_still_none():
    assert normalize_offer_condition("garbage") is None


# ── FIX B: compute_status with unrecognized condition ────────────────────────


def test_compute_status_unrecognized_condition_is_unset():
    assert compute_status("garbage", {}, has_images=False) == "unset"


@pytest.mark.parametrize("cond", ["new", "new_no_pkg", "pulls", "refurb"])
def test_compute_status_valid_conditions_not_unset(cond):
    # Valid conditions must not be short-circuited to "unset" — they may be
    # incomplete/essentials/complete depending on data, but never "unset".
    status = compute_status(cond, {}, has_images=False)
    assert status != "unset"


# ── FIX C: essentials_data helper ────────────────────────────────────────────


def test_essentials_data_all_defaults_are_empty_strings():
    d = essentials_data()
    expected_keys = {
        "manufacturer",
        "packaging",
        "date_code",
        "usage",
        "refurbished_by",
        "refurb_process",
        "cert_doc",
        "part_condition",
    }
    assert set(d.keys()) == expected_keys
    assert all(v == "" for v in d.values())


def test_essentials_data_passes_through_values():
    d = essentials_data(manufacturer="TI", packaging="Trays", usage="boards")
    assert d["manufacturer"] == "TI"
    assert d["packaging"] == "Trays"
    assert d["usage"] == "boards"
    assert d["date_code"] == ""


def test_essentials_data_none_coerced_to_empty_string():
    d = essentials_data(manufacturer=None, usage=None)
    assert d["manufacturer"] == ""
    assert d["usage"] == ""


def test_essentials_data_compatible_with_validate_essentials():
    # essentials_data output must be accepted by validate_essentials without error
    errs = validate_essentials("new", essentials_data(manufacturer="TI"))
    assert errs == []
