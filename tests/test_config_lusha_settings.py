"""SP1 config: Lusha enrichment settings exist with documented defaults.

Verifies the three new Settings fields and that no lusha_api_key field was added
(the key flows through get_credential_cached, matching Explorium).
"""

import os

os.environ["TESTING"] = "1"

from app.config import settings


def test_lusha_settings_defaults(monkeypatch):
    """Lusha SP1 config CODE defaults, independent of any ambient prod ``.env``.

    A fresh Settings is built with no env file and the relevant vars cleared so the
    assertions verify the in-code defaults even when pytest runs from a checkout that
    carries a prod ``.env`` (e.g. ``LUSHA_ENRICHMENT_ENABLED=true``).
    """
    from app.config import Settings

    for key in (
        "LUSHA_ENRICHMENT_ENABLED",
        "LUSHA_COOLDOWN_MINUTES",
        "PROSPECT_ENRICH_CONTACTS_PER_ACCOUNT",
    ):
        monkeypatch.delenv(key, raising=False)

    s = Settings(_env_file=None)
    assert s.lusha_enrichment_enabled is False
    assert s.lusha_cooldown_minutes == 15
    assert s.prospect_enrich_contacts_per_account == 5


def test_no_lusha_api_key_field():
    assert not hasattr(settings, "lusha_api_key")
