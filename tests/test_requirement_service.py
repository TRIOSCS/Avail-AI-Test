# tests/test_requirement_service.py — unit tests for the ONE requirement-creation
# pipeline (W3): vocabulary normalization (condition, packaging clamp), canonical
# normalized_mpn key form, blank-MPN skips, and explicit-None target_qty handling.

from app.models.crm import Company, CustomerSite
from app.models.sourcing import Requisition
from app.services.requirement_service import create_requirements


def _mk_req(db_session, name="REQ-SVC-TEST"):
    req = Requisition(name=name, customer_name="Acme")
    db_session.add(req)
    db_session.flush()
    return req


def test_normalizes_mpn_condition_and_packaging(db_session, test_user):
    req = _mk_req(db_session)
    result = create_requirements(
        db_session,
        req,
        [
            {
                "primary_mpn": "max232cpe+",
                "condition": "New",
                "packaging": "Tape & Reel",
                "target_qty": 10,
            }
        ],
    )
    assert len(result.created) == 1 and not result.skipped
    r = result.created[0]
    assert r.primary_mpn == "MAX232CPE+"  # display form
    assert r.normalized_mpn == "max232cpe"  # key form: lowercase, non-alnum stripped
    assert r.condition == "new"  # chk_req_condition vocab
    assert r.packaging == "reel"  # chk_req_packaging vocab


def test_packaging_clamped_to_requirements_vocab(db_session, test_user):
    """normalize_packaging's sightings/offers-only outputs ('bag','box','each') and
    unmapped spellings must store as NULL — chk_req_packaging (048) would reject them on
    any fresh migration-built DB."""
    req = _mk_req(db_session)
    result = create_requirements(
        db_session,
        req,
        [
            {"primary_mpn": "P1", "packaging": "bag"},  # mapped, out-of-vocab
            {"primary_mpn": "P2", "packaging": "yes"},  # unmapped
            {"primary_mpn": "P3", "packaging": "Tube"},  # in-vocab after case-fold
        ],
    )
    by_mpn = {r.primary_mpn: r for r in result.created}
    assert by_mpn["P1"].packaging is None
    assert by_mpn["P2"].packaging is None
    assert by_mpn["P3"].packaging == "tube"


def test_blank_mpn_skipped_never_fatal(db_session, test_user):
    req = _mk_req(db_session)
    result = create_requirements(
        db_session,
        req,
        [{"primary_mpn": "  "}, {"primary_mpn": "LM317T"}],
    )
    assert len(result.created) == 1
    assert result.created[0].primary_mpn == "LM317T"
    assert result.skipped == [{"index": 0, "error": "primary_mpn is required"}]


def test_dup_detection_same_part_same_site(db_session, test_user):
    """W3 acceptance: UI dup detection — the same part on another requisition
    for the same customer site (30-day window) is reported, informationally."""
    company = Company(name="Dup Co")
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    req_a = Requisition(name="REQ-A", customer_name="Dup Co", customer_site_id=site.id)
    db_session.add(req_a)
    db_session.flush()
    first = create_requirements(db_session, req_a, [{"primary_mpn": "LM317T"}])
    assert first.duplicates == []  # nothing to collide with yet

    req_b = Requisition(name="REQ-B", customer_name="Dup Co", customer_site_id=site.id)
    db_session.add(req_b)
    db_session.flush()
    second = create_requirements(db_session, req_b, [{"primary_mpn": "LM317T"}])
    assert second.duplicates == [{"mpn": "LM317T", "req_id": req_a.id, "req_name": "REQ-A"}]


def test_target_qty_absent_vs_explicit_none(db_session, test_user):
    """Absent qty leaves the model default (1); an explicit None (search-picker add with
    no quantity) stores NULL."""
    req = _mk_req(db_session)
    result = create_requirements(
        db_session,
        req,
        [
            {"primary_mpn": "QTY-DEFAULT"},
            {"primary_mpn": "QTY-NULL", "target_qty": None},
        ],
    )
    by_mpn = {r.primary_mpn: r for r in result.created}
    assert by_mpn["QTY-DEFAULT"].target_qty == 1
    assert by_mpn["QTY-NULL"].target_qty is None
