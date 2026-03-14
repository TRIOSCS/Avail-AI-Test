"""Knowledge service transactional tests.

Purpose: Validate transaction ownership behavior in knowledge_service.create_entry.
Description: Ensures commit=False does not force persistence across caller rollback.
Business rules enforced:
- create_entry(commit=False) must stay inside caller transaction boundary.
- create_entry(commit=True) persists immediately.
Called-by: pytest
Depends-on: app/services/knowledge_service.py, app/models/knowledge.py
"""

from app.models.knowledge import KnowledgeEntry
from app.services.knowledge_service import create_entry


def test_create_entry_commit_false_respects_caller_transaction(db_session, test_user):
    """commit=False should allow caller rollback to remove the row."""
    entry = create_entry(
        db_session,
        user_id=test_user.id,
        entry_type="fact",
        content="transient fact",
        source="manual",
        commit=False,
    )
    assert entry.id is not None
    db_session.rollback()
    assert db_session.query(KnowledgeEntry).filter(KnowledgeEntry.id == entry.id).first() is None


def test_create_entry_commit_true_persists(db_session, test_user):
    """commit=True should persist row immediately."""
    entry = create_entry(
        db_session,
        user_id=test_user.id,
        entry_type="fact",
        content="durable fact",
        source="manual",
    )
    assert entry.id is not None
    assert db_session.query(KnowledgeEntry).filter(KnowledgeEntry.id == entry.id).first() is not None
