"""Guard: the resell demo seeder refuses to run without an explicit opt-in flag.

Mirrors the AVSAMPLE seed guard — a mistaken `python -m app.management.seed_resell_demo`
against the prod database must NOT inject synthetic demo data. The refusal is checked
before any DB session is opened, so this test needs no database.

Called by: pytest
Depends on: app.management.seed_resell_demo
"""

import pytest

from app.management import seed_resell_demo


def test_resell_seed_refuses_without_optin(monkeypatch):
    monkeypatch.delenv("ALLOW_SAMPLE_DATA_SEED", raising=False)
    with pytest.raises(SystemExit) as exc:
        seed_resell_demo.main([])
    assert exc.value.code == 2


def test_resell_seed_refuses_with_falsey_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_SAMPLE_DATA_SEED", "false")
    with pytest.raises(SystemExit) as exc:
        seed_resell_demo.main([])
    assert exc.value.code == 2
