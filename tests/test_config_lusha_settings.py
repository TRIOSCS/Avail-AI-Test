"""SP1 config: Lusha enrichment settings exist with documented defaults.

Verifies the three new Settings fields and that no lusha_api_key field was added
(the key flows through get_credential_cached, matching Explorium).
"""

import os

os.environ["TESTING"] = "1"

from app.config import settings


def test_lusha_settings_defaults():
    assert settings.lusha_enrichment_enabled is False
    assert settings.lusha_cooldown_minutes == 15
    assert settings.prospect_enrich_contacts_per_account == 5


def test_no_lusha_api_key_field():
    assert not hasattr(settings, "lusha_api_key")
