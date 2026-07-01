"""Regression tests for the AI-search obsolescence trigger MPN lookup.

Called by: pytest
Depends on: app.search_service._any_pn_obsolete, MaterialCard, normalize_mpn_key

The smart-AI trigger checks whether any of a requirement's PNs is an obsolete
MaterialCard. ``pns`` arrive in DISPLAY form (uppercase, dashes preserved) from
get_all_pns, while MaterialCard.normalized_mpn stores the canonical KEY form
(normalize_mpn_key: lowercase, non-alphanumerics stripped). A raw display-form
``filter_by(normalized_mpn=pn)`` never matches, so is_obsolete was permanently
False and the trigger never fired. These tests lock in the key-form lookup.
"""

from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.search_service import _any_pn_obsolete
from app.utils.normalization import normalize_mpn_key


def _mk_card(db: Session, display_mpn: str, lifecycle_status: str) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=normalize_mpn_key(display_mpn),
        display_mpn=display_mpn,
        lifecycle_status=lifecycle_status,
    )
    db.add(card)
    db.flush()
    return card


class TestAnyPnObsolete:
    def test_obsolete_card_found_from_display_form_pn(self, db_session: Session):
        # Card stored in canonical key form ("xc7a35t-1ftg256c"); the PN that
        # reaches the trigger is the display form ("XC7A35T-1FTG256C").
        _mk_card(db_session, "XC7A35T-1FTG256C", "obsolete")
        db_session.commit()

        assert _any_pn_obsolete(db_session, ["XC7A35T-1FTG256C"]) is True

    def test_active_card_is_not_obsolete(self, db_session: Session):
        _mk_card(db_session, "STM32F407VGT6", "active")
        db_session.commit()

        assert _any_pn_obsolete(db_session, ["STM32F407VGT6"]) is False

    def test_mixed_pns_returns_true_if_any_obsolete(self, db_session: Session):
        _mk_card(db_session, "LM358-DR", "active")
        _mk_card(db_session, "EP2C5T144C8N", "obsolete")
        db_session.commit()

        assert _any_pn_obsolete(db_session, ["LM358-DR", "EP2C5T144C8N"]) is True

    def test_no_matching_card_returns_false(self, db_session: Session):
        assert _any_pn_obsolete(db_session, ["NOSUCHPART-123"]) is False

    def test_empty_or_unkeyable_pns_returns_false(self, db_session: Session):
        # normalize_mpn_key strips these to empty, so there are no keys to match.
        assert _any_pn_obsolete(db_session, []) is False
        assert _any_pn_obsolete(db_session, ["--", "  "]) is False
