"""batch_queue.py — Lifecycle helper for Claude Batch API submissions.

Manages the pending -> submitted -> completed cycle for batch AI processing.
Wraps claude_batch_submit/claude_batch_results with queue management.

Called by: scheduler jobs, enrichment services
Depends on: app.utils.claude_client
"""

from loguru import logger


class BatchQueue:
    """In-memory batch queue for collecting items before batch submission.

    Usage:
        bq = BatchQueue(prefix="material_enrich")
        bq.enqueue("mat_123", {"prompt": "...", "schema": {...}})
        bq.enqueue("mat_456", {"prompt": "...", "schema": {...}})
        requests = bq.build_batch()
        # Submit via claude_batch_submit(requests)
    """

    def __init__(self, prefix: str):
        self.prefix = prefix
        self._pending: dict[str, dict] = {}

    def enqueue(self, item_id: str, request: dict) -> None:
        """Add an item to the pending queue."""
        self._pending[item_id] = request

    def pending_count(self) -> int:
        """Return number of items waiting for batch submission."""
        return len(self._pending)

    def build_batch(self) -> list[dict]:
        """Build batch request list from pending items.

        Returns list of dicts ready for claude_batch_submit(). Clears the pending queue.
        """
        if not self._pending:
            return []

        requests = []
        for item_id, req in self._pending.items():
            requests.append(
                {
                    "custom_id": f"{self.prefix}:{item_id}",
                    "prompt": req["prompt"],
                    "schema": req["schema"],
                    "system": req.get("system", ""),
                    "model_tier": req.get("model_tier", "fast"),
                    "max_tokens": req.get("max_tokens", 1024),
                }
            )

        self._pending.clear()
        logger.info("Built batch of %d items for prefix '%s'", len(requests), self.prefix)
        return requests
