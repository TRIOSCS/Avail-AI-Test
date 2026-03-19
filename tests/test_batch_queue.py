"""Tests for batch queue lifecycle helper."""

from app.services.batch_queue import BatchQueue


def test_enqueue_adds_item():
    bq = BatchQueue(prefix="test")
    bq.enqueue("item_1", {"prompt": "test", "schema": {}})
    assert bq.pending_count() == 1


def test_build_batch_returns_requests():
    bq = BatchQueue(prefix="test")
    bq.enqueue("item_1", {"prompt": "test prompt", "schema": {"type": "object"}})
    requests = bq.build_batch()
    assert len(requests) == 1
    assert requests[0]["custom_id"] == "test:item_1"
    assert requests[0]["prompt"] == "test prompt"
    assert requests[0]["schema"] == {"type": "object"}
    assert requests[0]["model_tier"] == "fast"
    assert requests[0]["max_tokens"] == 1024


def test_empty_queue_returns_empty_batch():
    bq = BatchQueue(prefix="test")
    assert bq.build_batch() == []


def test_build_batch_clears_pending():
    bq = BatchQueue(prefix="test")
    bq.enqueue("item_1", {"prompt": "test", "schema": {}})
    bq.build_batch()
    assert bq.pending_count() == 0


def test_multiple_items():
    bq = BatchQueue(prefix="enrich")
    bq.enqueue("mat_1", {"prompt": "p1", "schema": {}, "model_tier": "smart", "max_tokens": 2048})
    bq.enqueue("mat_2", {"prompt": "p2", "schema": {}, "system": "You are an expert."})
    requests = bq.build_batch()
    assert len(requests) == 2
    ids = {r["custom_id"] for r in requests}
    assert "enrich:mat_1" in ids
    assert "enrich:mat_2" in ids
    # Check custom params passed through
    smart_req = next(r for r in requests if r["custom_id"] == "enrich:mat_1")
    assert smart_req["model_tier"] == "smart"
    assert smart_req["max_tokens"] == 2048
    sys_req = next(r for r in requests if r["custom_id"] == "enrich:mat_2")
    assert sys_req["system"] == "You are an expert."


def test_enqueue_overwrites_same_id():
    bq = BatchQueue(prefix="test")
    bq.enqueue("item_1", {"prompt": "old", "schema": {}})
    bq.enqueue("item_1", {"prompt": "new", "schema": {}})
    assert bq.pending_count() == 1
    requests = bq.build_batch()
    assert requests[0]["prompt"] == "new"
