"""Guard: scripts/seed_test_data.py refuses to run without an explicit opt-in flag.

Mirrors tests/test_seed_resell_demo_guard.py — a mistaken `python
scripts/seed_test_data.py` against the prod database must NOT inject synthetic test
data. The refusal is checked before any DB session is opened, so this test needs no
database.

Called by: pytest
Depends on: scripts.seed_test_data
"""

import pytest

from scripts import seed_test_data


def test_seed_test_data_refuses_without_optin(monkeypatch):
    monkeypatch.delenv("ALLOW_SAMPLE_DATA_SEED", raising=False)
    with pytest.raises(SystemExit) as exc:
        seed_test_data.main()
    assert exc.value.code == 2


def test_seed_test_data_refuses_with_falsey_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_SAMPLE_DATA_SEED", "false")
    with pytest.raises(SystemExit) as exc:
        seed_test_data.main()
    assert exc.value.code == 2
