"""test_quote_preview_email.py — Preview renders the exact send-path email (Phase-3 B2).

Verifies: /v2/partials/quotes/{id}/preview no longer hand-rolls its own markup — it
renders _build_quote_email_html (the SAME builder send_quote_email uses) inside an
iframe-srcdoc shell with a To/Subject header strip, so what the salesperson previews is
byte-for-byte what goes out. Also covers the no-resolvable-recipient case (200 + a note,
never a 500).

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_quote, test_requisition,
test_user), app.routers.htmx.quotes.preview_quote, app.services.quote_send.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Quote, Requisition, User
from app.services.quote_send import build_quote_subject


class TestPreviewRendersSendPathEmail:
    def test_preview_contains_email_builder_markup_not_old_fields(self, client: TestClient, test_quote: Quote):
        """The preview body IS _build_quote_email_html's output (a marker only it
        emits), and the old hand-rolled preview's "Status" field label is gone."""
        resp = client.post(
            f"/v2/partials/quotes/{test_quote.id}/preview",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Unique to _build_quote_email_html's greeting body — never emitted by anything else.
        assert "Thank you for your interest. Please find our quotation detailed below." in resp.text
        # The old preview.html rendered a labeled "Status" field — that markup is deleted.
        assert ">Status<" not in resp.text

    def test_preview_shows_resolved_recipient(self, client: TestClient, test_quote: Quote):
        """The header strip shows the recipient the send path would actually use
        (site.contact_name / contact_email — same resolution as send_quote_email)."""
        resp = client.post(
            f"/v2/partials/quotes/{test_quote.id}/preview",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "jane@acme-electronics.com" in resp.text
        assert "Jane Doe" in resp.text
        assert build_quote_subject(test_quote) in resp.text

    def test_preview_no_recipient_resolved_is_200_not_500(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_user: User,
    ):
        """A quote with no customer site (so no contact email to resolve) must still
        preview at 200 with a note — mirrors crm/quotes.py:358's no-recipient handling,
        never a 500."""
        quote = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=None,
            quote_number="TEST-Q-NOSITE-001",
            status="draft",
            line_items=[],
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(quote)
        db_session.commit()
        db_session.refresh(quote)

        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/preview",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "no recipient resolved" in resp.text.lower()
