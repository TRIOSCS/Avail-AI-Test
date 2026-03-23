# Mocking Reference

## Contents
- Where to Patch (Source Module Rule)
- Mocking Claude API Calls
- Mocking Microsoft Graph API
- Mocking Connector/External API Calls
- AsyncMock vs MagicMock
- WARNING: Patching at Import Site

## Where to Patch (Source Module Rule)

Always patch at the module where the name is **defined**, not where it is **imported**:

```python
# app/services/ai_service.py contains:
#   from app.utils.claude_client import claude_json

# BAD — patches the wrong reference
with patch("app.utils.claude_client.claude_json") as mock:
    ...

# GOOD — patches where ai_service sees it
with patch("app.services.ai_service.claude_json") as mock:
    ...
```

## Mocking Claude API Calls

`claude_json` and `claude_text` are the two entry points into Claude. Always mock at the service module:

```python
from unittest.mock import AsyncMock, patch
from app.services.ai_service import draft_rfq


async def test_draft_rfq_returns_email_body():
    with patch("app.services.ai_service.claude_text", new_callable=AsyncMock) as mock:
        mock.return_value = "Dear vendor,\n\nPlease quote LM317T x1000."
        result = await draft_rfq(mpn="LM317T", qty=1000)
    assert "LM317T" in result
    mock.assert_called_once()
```

For structured JSON responses:

```python
async def test_enrich_returns_parsed_contacts():
    mock_response = {
        "contacts": [{"full_name": "Jane Smith", "title": "VP Procurement", "email": "jane@acme.com"}]
    }
    with patch("app.services.ai_service.claude_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_response
        result = await enrich_contacts_websearch("Acme Electronics", db=None)
    assert result[0]["full_name"] == "Jane Smith"
```

## Mocking Microsoft Graph API

Graph API calls go through `app/utils/graph_client.py`. Mock at the service that uses it:

```python
from unittest.mock import AsyncMock, patch


async def test_send_rfq_calls_graph_once():
    with patch("app.email_service.graph_client.send_mail", new_callable=AsyncMock) as mock:
        mock.return_value = {"id": "msg-abc123"}
        await send_batch_rfq(requisition_id=1, vendor_emails=["vendor@arrow.com"])
    mock.assert_called_once()
    call_kwargs = mock.call_args.kwargs
    assert "[AVAIL-1]" in call_kwargs["subject"]
```

## Mocking Connector/External API Calls

Search connectors all inherit from a base class. Mock `search()` at the connector module:

```python
from unittest.mock import AsyncMock, patch


async def test_search_service_aggregates_results():
    mock_results = [{"mpn": "LM317T", "vendor": "Arrow", "price": 0.50, "qty": 1000}]
    with patch("app.connectors.brokerbin.BrokerBinConnector.search", new_callable=AsyncMock) as mock:
        mock.return_value = mock_results
        results = await search_requirement(mpn="LM317T", qty=1000)
    assert any(r["vendor"] == "Arrow" for r in results)
```

## AsyncMock vs MagicMock

Use `AsyncMock` for any function defined with `async def`. Using `MagicMock` for async functions causes `TypeError: object MagicMock can't be used in 'await' expression`:

```python
# BAD — MagicMock is not awaitable
with patch("app.services.ai_service.claude_json") as mock:
    mock.return_value = {"contacts": []}  # fails at await claude_json(...)

# GOOD
with patch("app.services.ai_service.claude_json", new_callable=AsyncMock) as mock:
    mock.return_value = {"contacts": []}
```

## Mocking the Cache Decorator

When testing endpoints that use `@cached_endpoint`, patch both `get_cached` and `set_cached` to control cache behaviour:

```python
from unittest.mock import patch


def test_cache_miss_calls_service():
    with (
        patch("app.cache.decorators.get_cached", return_value=None),
        patch("app.cache.decorators.set_cached") as mock_set,
    ):
        result = my_cached_function(x=42)
    assert result is not None
    mock_set.assert_called_once()
```

See the **redis** skill for cache invalidation patterns.

## WARNING: Patching at Import Site

**The Problem:**

```python
# BAD — this patches the original module, not the reference used by ai_service
with patch("app.utils.claude_client.claude_json") as mock:
    result = await draft_rfq(mpn="LM317T", qty=1000)
# mock.called == False — the patch had no effect!
```

**Why This Breaks:**
Python resolves names at import time. `app/services/ai_service.py` has already bound `claude_json` to its own namespace. Patching the source module changes the original, but not the already-bound reference in `ai_service`.

**The Fix:**
Always patch `"app.services.<module_using_it>.<name>"` — never `"app.utils.<source>.<name>"`.
