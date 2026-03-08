"""Shared enrichment utilities — batch processing, credential checks, contact dedup.

Provides reusable helpers for enrichment pipelines to avoid duplicating
batch loop / semaphore / progress / credential / dedup logic.

Called by: enrichment.py, customer_enrichment_batch.py, customer_enrichment_service.py,
           deep_enrichment_service.py
Depends on: credential_service, asyncio, loguru
"""

import asyncio
from typing import Any, Callable, Coroutine

from loguru import logger

from app.services.credential_service import get_credential_cached


async def run_enrichment_batch(
    entities: list,
    process_fn: Callable[[Any], Coroutine],
    *,
    batch_size: int = 50,
    concurrency: int = 5,
    label: str = "enrichment",
) -> dict:
    """Shared batch processing helper with semaphore + progress logging.

    Processes ``entities`` through ``process_fn`` with bounded concurrency.
    Logs progress every ``batch_size`` items.

    Args:
        entities: items to process
        process_fn: async callable that takes one entity, returns a result dict or None
        batch_size: how often to log progress (default 50)
        concurrency: max parallel tasks via semaphore (default 5)
        label: human-readable label for log messages

    Returns:
        {total, processed, errors: [str]}
    """
    sem = asyncio.Semaphore(concurrency)
    total = len(entities)
    processed = 0
    errors: list[str] = []

    async def _run_one(entity):
        nonlocal processed
        async with sem:
            try:
                await process_fn(entity)
                processed += 1
            except Exception as e:
                processed += 1
                errors.append(f"{label}: {str(e)[:200]}")
                logger.warning("%s batch error: %s", label, e)

    for i, entity in enumerate(entities):
        await _run_one(entity)
        if (i + 1) % batch_size == 0:
            logger.info(
                "%s progress: %d/%d processed (%d errors)",
                label,
                i + 1,
                total,
                len(errors),
            )

    logger.info(
        "%s complete: %d/%d processed, %d errors",
        label,
        processed,
        total,
        len(errors),
    )
    return {"total": total, "processed": processed, "errors": errors}


def check_enrichment_credentials(source_names: list[str]) -> dict[str, bool]:
    """Check which enrichment sources have valid credentials configured.

    Args:
        source_names: list of source names to check (e.g. ["apollo", "hunter", "lusha"])

    Returns:
        dict mapping source name to bool (True = credentials present)
    """
    # Map source names to their required credential keys
    _CRED_MAP: dict[str, list[tuple[str, str]]] = {
        "apollo": [("apollo", "APOLLO_API_KEY")],
        "hunter": [("hunter", "HUNTER_API_KEY")],
        "lusha": [("lusha", "LUSHA_API_KEY")],
        "nexar": [("nexar", "NEXAR_CLIENT_ID"), ("nexar", "NEXAR_CLIENT_SECRET")],
        "digikey": [("digikey", "DIGIKEY_CLIENT_ID"), ("digikey", "DIGIKEY_CLIENT_SECRET")],
        "mouser": [("mouser", "MOUSER_API_KEY")],
        "element14": [("element14", "ELEMENT14_API_KEY")],
        "oemsecrets": [("oemsecrets", "OEMSECRETS_API_KEY")],
        "brokerbin": [("brokerbin", "BROKERBIN_API_KEY")],
        "clearbit": [("clearbit", "CLEARBIT_API_KEY")],
        "explorium": [("explorium", "EXPLORIUM_API_KEY")],
        "anthropic": [("anthropic_ai", "ANTHROPIC_API_KEY")],
    }

    result = {}
    for name in source_names:
        cred_keys = _CRED_MAP.get(name, [])
        if not cred_keys:
            result[name] = False
            continue
        result[name] = all(bool(get_credential_cached(src, env_var)) for src, env_var in cred_keys)
    return result


def deduplicate_contacts(contacts: list[dict], key: str = "email") -> list[dict]:
    """Deduplicate contacts by a key field, keeping the first (higher-priority) entry.

    Args:
        contacts: list of contact dicts
        key: field name to deduplicate on (default "email")

    Returns:
        deduplicated list preserving original order
    """
    seen: set[str] = set()
    result: list[dict] = []
    for contact in contacts:
        val = (contact.get(key) or "").lower().strip()
        if not val or val in seen:
            continue
        seen.add(val)
        result.append(contact)
    return result
