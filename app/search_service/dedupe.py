"""Search package — sighting deduplication (display merge, aggressive per-vendor
grouping for the search tab, and the streaming incremental dedup).

W4.5a split of app/search_service.py — pure structural move (see cache.py header).
"""

from ..vendor_utils import normalize_vendor_name


def _deduplicate_sightings(sighting_dicts: list[dict]) -> list[dict]:
    """Deduplicate and merge sighting results for cleaner display.

    Rules:
    - Exclude sightings with qty_available=0 (confirmed zero stock)
    - Keep sightings with qty_available=None (unknown stock — part exists)
    - Same vendor + MPN + price → merge (sum quantities, keep best row)
    - Same vendor + MPN + different price → keep separate lines
    - Historical / material-history rows pass through untouched
    """
    kept: list[dict] = []
    groups: dict[tuple, list[dict]] = {}

    for d in sighting_dicts:
        # Pass through historical rows untouched
        if d.get("is_historical") or d.get("is_material_history"):
            kept.append(d)
            continue

        # Filter out rows with confirmed zero stock; keep None (unknown qty)
        qty = d.get("qty_available")
        if qty is not None and qty == 0:
            continue

        # Group key: vendor + mpn + price
        vendor = (d.get("vendor_name") or "").strip().lower()
        mpn = (d.get("mpn_matched") or "").strip().lower()
        price = d.get("unit_price")
        price_key = round(float(price), 4) if price is not None else None
        key = (vendor, mpn, price_key)
        groups.setdefault(key, []).append(d)

    # Merge each group
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue

        # Pick the row with highest score as the "best"
        group.sort(key=lambda x: (x.get("confidence_pct") or 0, x.get("score") or 0), reverse=True)
        best = dict(group[0])

        # Sum quantities across all rows in group; stay None if all unknown
        known_qtys = [g["qty_available"] for g in group if g.get("qty_available") is not None]
        best["qty_available"] = sum(known_qtys) if known_qtys else None

        # Keep best confidence
        best["confidence"] = max((g.get("confidence") or 0) for g in group)

        # Keep lowest MOQ (most favorable to buyer)
        moqs = [g["moq"] for g in group if g.get("moq")]
        if moqs:
            best["moq"] = min(moqs)

        # Collect merged source types (e.g. "nexar + digikey")
        sources = sorted({g.get("source_type", "") for g in group})
        if len(sources) > 1:
            best["merged_sources"] = sources

        best["merged_count"] = len(group)
        kept.append(best)

    return kept


def _deduplicate_sightings_aggressive(sighting_dicts: list[dict]) -> list[dict]:
    """Aggressive dedup: one entry per vendor+MPN. All price variants become sub_offers.

    Used by the search tab (not requisition search which uses _deduplicate_sightings).

    Called by: stream_search_mpn, search_run (search tab only)
    Depends on: vendor_utils.normalize_vendor_name
    """
    groups: dict[tuple, list[dict]] = {}

    for d in sighting_dicts:
        qty = d.get("qty_available")
        if qty is not None and qty == 0:
            continue

        vendor = normalize_vendor_name((d.get("vendor_name") or "").strip())
        mpn = (d.get("mpn_matched") or "").strip().lower()
        key = (vendor, mpn)
        groups.setdefault(key, []).append(d)

    results = []
    for group in groups.values():
        group.sort(key=lambda x: (x.get("score", 0), x.get("confidence", 0)), reverse=True)
        best = dict(group[0])

        # Sum quantities
        known_qtys = [g["qty_available"] for g in group if g.get("qty_available") is not None]
        best["qty_available"] = sum(known_qtys) if known_qtys else None

        # Best confidence
        best["confidence"] = max((g.get("confidence") or 0) for g in group)

        # Lowest MOQ
        moqs = [g["moq"] for g in group if g.get("moq")]
        if moqs:
            best["moq"] = min(moqs)

        # Collect sources
        best["sources_found"] = {g.get("source_type", "") for g in group}
        best["sources_found"].discard("")

        # Sub-offers (everything except the best)
        best["sub_offers"] = group[1:] if len(group) > 1 else []
        best["offer_count"] = len(group)

        results.append(best)

    results.sort(key=lambda x: (x.get("score", 0), x.get("confidence", 0)), reverse=True)
    return results


def _incremental_dedup(incoming: list[dict], existing: list[dict]) -> tuple[list[dict], list[dict]]:
    """Dedup incoming results against already-sent cards.

    Returns (new_cards, updated_cards) where:
    - new_cards: vendors not yet seen — append to DOM
    - updated_cards: vendors already sent — OOB swap to update card

    Mutates existing list in-place (adds new entries, updates existing ones).

    Called by: _run_streaming_search
    Depends on: vendor_utils.normalize_vendor_name
    """
    existing_map: dict[tuple, dict] = {}
    for card in existing:
        vendor = normalize_vendor_name((card.get("vendor_name") or "").strip())
        mpn = (card.get("mpn_matched") or "").strip().lower()
        existing_map[(vendor, mpn)] = card

    new_cards = []
    updated_cards = []

    for item in incoming:
        qty = item.get("qty_available")
        if qty is not None and qty == 0:
            continue

        vendor = normalize_vendor_name((item.get("vendor_name") or "").strip())
        mpn = (item.get("mpn_matched") or "").strip().lower()
        key = (vendor, mpn)

        if key in existing_map:
            card = existing_map[key]
            card.setdefault("sub_offers", []).append(item)
            card["offer_count"] = card.get("offer_count", 1) + 1
            card.setdefault("sources_found", set()).add(item.get("source_type", ""))

            # Update best offer if incoming is better
            if item.get("score", 0) > card.get("score", 0):
                old_best = {k: v for k, v in card.items() if k not in ("sub_offers", "offer_count", "sources_found")}
                card["sub_offers"].append(old_best)
                card["sub_offers"].remove(item)
                for k, v in item.items():
                    if k not in ("sub_offers", "offer_count", "sources_found"):
                        card[k] = v

            # Re-sum quantities
            all_offers = [card] + card.get("sub_offers", [])
            known_qtys = [o["qty_available"] for o in all_offers if o.get("qty_available") is not None]
            card["qty_available"] = sum(known_qtys) if known_qtys else None

            updated_cards.append(card)
        else:
            new_card = dict(item)
            new_card["sub_offers"] = []
            new_card["offer_count"] = 1
            new_card["sources_found"] = {item.get("source_type", "")}
            new_card["sources_found"].discard("")
            existing.append(new_card)
            existing_map[key] = new_card
            new_cards.append(new_card)

    return new_cards, updated_cards
