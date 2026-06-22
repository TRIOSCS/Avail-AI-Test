from app.services import firmo_tiers as ft


def test_per_field_authority_overrides_base():
    # SAM wins legal_name; Explorium wins ticker; Lusha wins phone.
    assert ft.firmo_tier("legal_name", "sam_gov") > ft.firmo_tier("legal_name", "explorium")
    assert ft.firmo_tier("ticker", "explorium") > ft.firmo_tier("ticker", "clay")
    assert ft.contact_tier("phone", "lusha") > ft.contact_tier("phone", "apollo")


def test_unknown_source_is_tier_zero():
    assert ft.firmo_tier("industry", "totally_unknown") == 0


def test_blend_company_highest_tier_wins_per_field():
    results = [
        {"source": "apollo", "industry": "Wholesale", "legal_name": "Arrow Inc"},
        {"source": "explorium", "industry": "Electronics Distribution", "ticker": "ARW"},
    ]
    blended = ft.blend_company(results)
    assert blended["industry"] == "Electronics Distribution"  # explorium > apollo
    assert blended["legal_name"] == "Arrow Inc"  # only apollo had it
    assert blended["ticker"] == "ARW"
    assert set(blended["source"].split("+")) == {"apollo", "explorium"}
    assert blended["_provenance"]["industry"]["source"] == "explorium"


def test_blend_company_skips_empty_values():
    results = [{"source": "ai", "industry": None, "website": ""}]
    assert ft.blend_company(results) == {}


def test_blend_contacts_dedups_and_prefers_verified_email():
    results = [
        {"source": "apollo", "full_name": "Jane Doe", "email": "j@x.com", "verified": False, "title": "Buyer"},
        {"source": "lusha", "full_name": "Jane Doe", "email": "j@x.com", "verified": True, "phone": "+1"},
    ]
    out = ft.blend_contacts(results)
    assert len(out) == 1
    assert out[0]["verified"] is True
    assert out[0]["phone"] == "+1"
    assert out[0]["title"] == "Buyer"
