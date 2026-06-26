"""Locks the reworked Requisition pipeline enum.

Called by: pytest. Depends on: app.constants.
"""

from app.constants import RequisitionStatus


def test_pipeline_members_exact():
    vals = {e.value for e in RequisitionStatus}
    assert vals == {"draft", "open", "rfqs_sent", "offers", "quoted", "won", "lost", "hotlist", "cancelled"}


def test_archived_and_sourcing_removed():
    assert not hasattr(RequisitionStatus, "ARCHIVED")
    assert not hasattr(RequisitionStatus, "SOURCING")
    assert not hasattr(RequisitionStatus, "ACTIVE")


def test_terminal_and_open_pipeline_sets():
    assert RequisitionStatus.TERMINAL == frozenset({"won", "lost", "cancelled"})
    assert RequisitionStatus.OPEN_PIPELINE == frozenset({"open", "rfqs_sent", "offers", "quoted"})
    assert RequisitionStatus.MONITOR == frozenset({"hotlist"})
