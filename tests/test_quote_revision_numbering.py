"""test_quote_revision_numbering.py — the unified quote revision convention (oq-04).

Owner-chosen 2026-08-17: the ORIGINAL quote keeps its base number forever; each
revision carries an explicit trail suffix — Q-0142 → Q-0142-R1 → Q-0142-R2. The two
previously-diverging writers (HTMX revise suffixing the NEW quote but compounding
-R2-R3 on re-revision; quote-builder/JSON-reopen renaming the OLD quote) now share
crm_service.quote_base_number / revision_quote_number.

Called by: pytest
Depends on: app.services.crm_service, conftest (client, db_session fixtures)
"""

from app.services.crm_service import quote_base_number, revision_quote_number


class TestHelpers:
    def test_base_number_strips_single_suffix(self):
        assert quote_base_number("Q-2026-0142-R2") == "Q-2026-0142"

    def test_base_number_flattens_legacy_compounded(self):
        assert quote_base_number("Q-2026-0142-R2-R3") == "Q-2026-0142"

    def test_base_number_passthrough(self):
        assert quote_base_number("Q-2026-0142") == "Q-2026-0142"
        assert quote_base_number(None) == ""

    def test_revision_suffixes(self):
        assert revision_quote_number("Q-2026-0142", 1) == "Q-2026-0142"  # original
        assert revision_quote_number("Q-2026-0142", 2) == "Q-2026-0142-R1"
        assert revision_quote_number("Q-2026-0142", 3) == "Q-2026-0142-R2"

    def test_no_compounding_round_trip(self):
        """Re-revising a revision must not stack suffixes: R1's next number is R2."""
        current = "Q-2026-0142-R1"
        assert revision_quote_number(quote_base_number(current), 3) == "Q-2026-0142-R2"
