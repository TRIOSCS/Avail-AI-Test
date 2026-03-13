"""Tests to close remaining coverage gaps.

Covers:
1. Companies — substring duplicate check
2. Tagging AI — module import coverage

Called by: pytest
Depends on: conftest fixtures, app modules
"""

from datetime import datetime, timezone

from app.models import Company

# ══════════════════════════════════════════════════════════════════════
#  1. COMPANIES — substring duplicate check
# ══════════════════════════════════════════════════════════════════════


class TestCompanySubstringMatch:
    def test_company_duplicate_substring(self, client, db_session):
        """Cover line 371: substring match in check-duplicate."""
        co = Company(name="Microchip Technology", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()

        resp = client.get("/api/companies/check-duplicate", params={"name": "Microchip"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("matches", [])) > 0


# ══════════════════════════════════════════════════════════════════════
#  2. TAGGING_AI — module import coverage
# ══════════════════════════════════════════════════════════════════════


class TestTaggingAiImport:
    def test_module_imports(self):
        """Cover module-level imports and constants."""
        from app.services.tagging_ai import _CLASSIFY_PROMPT, _SYSTEM

        assert "classify" in _CLASSIFY_PROMPT.lower()
        assert len(_SYSTEM) > 0
