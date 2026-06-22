"""rfq_attachments.py — collect and encode datasheet files for RFQ email attachments.

Resolves MaterialCardDatasheet rows for the basket's material cards, fetches bytes
via datasheet_library.fetch_datasheet_bytes under a Semaphore to bound concurrency,
base64-encodes each file, enforces a ~3 MB combined cap by dropping the LARGEST
attachments first, and returns (trimmed attachment list, per-datasheet status list).

Called by: app.routers.sightings.sightings_send_inquiry (via collect_rfq_attachments)
           app.email_service.send_batch_rfq (consumes the RfqAttachment list)
Depends on: app.models.intelligence.MaterialCardDatasheet,
            app.services.datasheet_library.fetch_datasheet_bytes
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import RfqAttachmentStatus
from ..models.intelligence import MaterialCardDatasheet
from .datasheet_library import fetch_datasheet_bytes

# ~3 MB combined limit — Microsoft Graph simple sendMail attachment cap.
_COMBINED_CAP_BYTES = 3 * 1024 * 1024

# Max concurrent Graph fetches per RFQ send.
_FETCH_CONCURRENCY = 3


@dataclass(frozen=True)
class RfqAttachment:
    """A single file attachment ready for the Graph sendMail API.

    Fields map directly to the Graph ``#microsoft.graph.fileAttachment`` properties.
    """

    name: str
    content_type: str
    content_bytes_b64: str  # base64-encoded file bytes


async def collect_rfq_attachments(
    db: Session,
    material_card_ids: list[int],
    selected_ids: list[int],
) -> tuple[list[RfqAttachment], list[dict]]:
    """Resolve, fetch, encode, and cap-enforce datasheets for an RFQ batch.

    Args:
        db: SQLAlchemy session (sync).
        material_card_ids: All material_card_id values from the basket's requirements
            (used to scope which datasheets are accessible).
        selected_ids: The subset of MaterialCardDatasheet.id values the buyer opted in.

    Returns:
        A tuple ``(attachments, statuses)`` where:
        - ``attachments`` is a list of :class:`RfqAttachment` ready to inject into the
          Graph payload (post-cap trimming, largest-first drop).
        - ``statuses`` is one dict per selected datasheet with keys
          ``datasheet_id``, ``file_name``, and ``status`` (one of
          ``attached`` | ``missing`` | ``oversized`` | ``fetch_error``).
    """
    if not selected_ids:
        return [], []

    # Resolve the selected datasheet rows, constrained to the basket's cards.
    rows: list[MaterialCardDatasheet] = (
        db.query(MaterialCardDatasheet)
        .filter(
            MaterialCardDatasheet.id.in_(selected_ids),
            MaterialCardDatasheet.material_card_id.in_(material_card_ids) if material_card_ids else False,
        )
        .all()
    )

    if not rows:
        return [], []

    row_map = {r.id: r for r in rows}

    # Fetch bytes concurrently, bounded by semaphore.
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch_one(ds: MaterialCardDatasheet) -> tuple[int, bytes | None, str]:
        """Return (datasheet_id, bytes_or_none, status_hint)."""
        if not (ds.library_drive_id and ds.library_item_id):
            return ds.id, None, RfqAttachmentStatus.MISSING
        async with sem:
            try:
                data = await fetch_datasheet_bytes(ds.library_drive_id, ds.library_item_id)
            except Exception:
                logger.warning(
                    "datasheet fetch error id={} item={}",
                    ds.id,
                    ds.library_item_id,
                    exc_info=True,
                )
                return ds.id, None, RfqAttachmentStatus.FETCH_ERROR
        if data is None:
            return ds.id, None, RfqAttachmentStatus.MISSING
        return ds.id, data, RfqAttachmentStatus.ATTACHED

    fetch_results = await asyncio.gather(*(_fetch_one(row_map[sid]) for sid in selected_ids if sid in row_map))

    # Build candidates, filter out errors, sort by size descending for cap drop.
    candidates: list[tuple[int, bytes, str]] = []  # (ds_id, bytes, file_name)
    error_statuses: list[dict] = []

    for ds_id, data, hint in fetch_results:
        ds = row_map.get(ds_id)
        if ds is None:
            continue
        if data is None:
            error_statuses.append({"datasheet_id": ds_id, "file_name": ds.file_name, "status": hint})
        else:
            candidates.append((ds_id, data, ds.file_name))

    # Enforce combined cap: drop LARGEST first until within budget.
    candidates.sort(key=lambda x: len(x[1]), reverse=True)
    total_bytes = sum(len(d) for _, d, _ in candidates)
    oversized_ids: set[int] = set()

    while total_bytes > _COMBINED_CAP_BYTES and candidates:
        dropped_id, dropped_data, dropped_name = candidates.pop(0)
        total_bytes -= len(dropped_data)
        oversized_ids.add(dropped_id)
        logger.info(
            "datasheet attachment dropped (oversized) id={} name={} size={}",
            dropped_id,
            dropped_name,
            len(dropped_data),
        )

    # Build attachment objects.
    attachments: list[RfqAttachment] = []
    for ds_id, data, _fname in candidates:
        ds = row_map[ds_id]
        attachments.append(
            RfqAttachment(
                name=ds.file_name,
                content_type=ds.content_type or "application/octet-stream",
                content_bytes_b64=base64.b64encode(data).decode(),
            )
        )

    # Build status list in selected_ids order.
    statuses: list[dict] = list(error_statuses)
    for ds_id, _, fname in candidates:
        statuses.append({"datasheet_id": ds_id, "file_name": fname, "status": RfqAttachmentStatus.ATTACHED})
    for ds_id in oversized_ids:
        ds = row_map[ds_id]
        statuses.append({"datasheet_id": ds_id, "file_name": ds.file_name, "status": RfqAttachmentStatus.OVERSIZED})

    return attachments, statuses
