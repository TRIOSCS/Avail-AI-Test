# tests/test_offer_qualification.py
# What: pure-function tests for the offer qualification service.
# Called by: pytest. Depends on: app.services.offer_qualification (no DB for these).
import pytest

from app.services.offer_qualification import (
    apply_qualification,
    compose_note,
    compute_status,
    meter,
    normalize_offer_condition,
    request_template,
    validate_essentials,
)


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
