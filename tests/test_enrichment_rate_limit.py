import asyncio

from app.connectors.errors import ConnectorQuotaError, ConnectorRateLimitError
from app.services.authoritative_enrichment_service import fetch_authoritative


def _rl_conn(name="element14"):
    class _C:
        source_name = name
        calls = 0

        async def search(self, pn):
            type(self).calls += 1
            raise ConnectorRateLimitError("element14 rate limited (QPS)")

    return _C()


def test_rate_limit_cools_down_not_disabled():
    conn = _rl_conn()
    disabled: set[str] = set()
    cooldown: dict[str, float] = {}
    # First MPN: rate-limited -> cooldown set, NOT permanently disabled
    asyncio.run(fetch_authoritative("A", "a", [conn], disabled, cooldown))
    assert "element14" not in disabled
    assert "element14" in cooldown
    calls_after_first = type(conn).calls
    # Second MPN immediately: still in cooldown -> skipped (no new call)
    asyncio.run(fetch_authoritative("B", "b", [conn], disabled, cooldown))
    assert type(conn).calls == calls_after_first


def test_quota_still_disables():
    class _Q:
        source_name = "oemsecrets"

        async def search(self, pn):
            raise ConnectorQuotaError("out of api calls")

    disabled: set[str] = set()
    asyncio.run(fetch_authoritative("A", "a", [_Q()], disabled, {}))
    assert "oemsecrets" in disabled
