"""Tests for the GET /api/requisitions status filter.

Covers comma-separated and single-value ?status= filtering on the
requisitions API list. (The part comms-tab task tests that used to live
here died with the split-panel workspace endpoints — spec §5.1.)

Called by: pytest
Depends on: routers/requisitions/core.py, conftest fixtures
"""


class TestRequisitionStatusFilter:
    def _get_requisitions(self, resp):
        """Extract requisition list from response, handling various formats."""
        import json

        data = resp.json()
        # Handle double-encoded JSON (cached_endpoint may return a string)
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, list):
            return data
        return data.get("requisitions", data.get("items", []))

    def test_comma_separated_status_filter(self, client, db_session, test_user):
        """GET /api/requisitions?status=won,lost returns only matching statuses."""
        from app.models import Requisition

        r1 = Requisition(name="Won Req", status="won", created_by=test_user.id)
        r2 = Requisition(name="Lost Req", status="lost", created_by=test_user.id)
        r3 = Requisition(name="Active Req", status="open", created_by=test_user.id)
        db_session.add_all([r1, r2, r3])
        db_session.commit()

        resp = client.get("/api/requisitions?status=won,lost")
        assert resp.status_code == 200
        items = self._get_requisitions(resp)
        names = [r["name"] for r in items]
        assert "Won Req" in names
        assert "Lost Req" in names
        assert "Active Req" not in names

    def test_single_status_still_works(self, client, db_session, test_user):
        """GET /api/requisitions?status=draft still works with a single value."""
        from app.models import Requisition

        r = Requisition(name="Draft Filter Req", status="draft", created_by=test_user.id)
        db_session.add(r)
        db_session.commit()

        resp = client.get("/api/requisitions?status=draft")
        assert resp.status_code == 200
        items = self._get_requisitions(resp)
        names = [r["name"] for r in items]
        assert "Draft Filter Req" in names
