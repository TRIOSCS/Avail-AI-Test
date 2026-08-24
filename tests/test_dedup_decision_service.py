"""tests/test_dedup_decision_service.py — unit tests for persisted dedup decisions.

Covers the pure service layer: canonicalization of keeper-first UI tokens to
(min,max), idempotent dismissal upserts, un-dismiss removal, the one-query
dismissed-set loader, and router-level candidate post-filtering.

Called by: pytest. Depends on: conftest fixtures, app.models.DedupDecision.
"""

import pytest

from app.models import DedupDecision


class TestCanonicalPair:
    def test_orders_min_max_both_directions(self):
        from app.services.dedup_decision_service import canonical_pair

        assert canonical_pair(7, 3) == (3, 7)
        assert canonical_pair(3, 7) == (3, 7)


class TestRecordDismissals:
    def test_canonicalizes_and_persists(self, db_session, admin_user):
        from app.services.dedup_decision_service import record_dismissals

        added = record_dismissals(db_session, "vendor", [(9, 4)], admin_user.id)
        db_session.commit()
        assert added == 1
        row = db_session.query(DedupDecision).one()
        assert (row.entity_type, row.id_a, row.id_b) == ("vendor", 4, 9)
        assert row.decided_by_id == admin_user.id

    def test_idempotent_across_token_orders(self, db_session, admin_user):
        from app.services.dedup_decision_service import record_dismissals

        record_dismissals(db_session, "company", [(4, 9)], admin_user.id)
        db_session.commit()
        added = record_dismissals(db_session, "company", [(9, 4), (4, 9)], admin_user.id)
        db_session.commit()
        assert added == 0
        assert db_session.query(DedupDecision).count() == 1

    def test_self_pair_ignored(self, db_session, admin_user):
        from app.services.dedup_decision_service import record_dismissals

        assert record_dismissals(db_session, "vendor", [(5, 5)], admin_user.id) == 0

    def test_unknown_entity_type_raises(self, db_session, admin_user):
        from app.services.dedup_decision_service import record_dismissals

        with pytest.raises(ValueError):
            record_dismissals(db_session, "nuke", [(1, 2)], admin_user.id)


class TestLoadAndFilter:
    def test_load_groups_by_entity_type_one_query(self, db_session):
        from app.services.dedup_decision_service import load_dismissed_pairs

        db_session.add_all(
            [
                DedupDecision(entity_type="vendor", id_a=1, id_b=2),
                DedupDecision(entity_type="company", id_a=1, id_b=2),
            ]
        )
        db_session.commit()
        d = load_dismissed_pairs(db_session)
        assert d["vendor"] == {(1, 2)}
        assert d["company"] == {(1, 2)}
        assert d["contact"] == set()

    def test_filter_drops_dismissed_regardless_of_pair_order(self):
        from app.services.dedup_decision_service import filter_dismissed_pairs

        pairs = [
            {"vendor_a": {"id": 9}, "vendor_b": {"id": 4}, "score": 95},
            {"vendor_a": {"id": 1}, "vendor_b": {"id": 2}, "score": 95},
        ]
        kept = filter_dismissed_pairs(pairs, {(4, 9)}, "vendor")
        assert kept == [pairs[1]]

    def test_filter_noop_when_nothing_dismissed(self):
        from app.services.dedup_decision_service import filter_dismissed_pairs

        pairs = [{"company_a": {"id": 1}, "company_b": {"id": 2}, "score": 90}]
        assert filter_dismissed_pairs(pairs, set(), "company") == pairs


class TestRemoveDismissal:
    def test_removes_canonical_row_given_reversed_ids(self, db_session):
        from app.services.dedup_decision_service import remove_dismissal

        db_session.add(DedupDecision(entity_type="contact", id_a=4, id_b=9))
        db_session.commit()
        assert remove_dismissal(db_session, "contact", 9, 4) == 1
        db_session.commit()
        assert db_session.query(DedupDecision).count() == 0

    def test_missing_row_raises(self, db_session):
        from app.services.dedup_decision_service import remove_dismissal

        with pytest.raises(ValueError):
            remove_dismissal(db_session, "contact", 1, 2)
