"""AI-powered search pre-selection for faceted materials search.

What: Takes a natural language query, calls Claude Haiku to interpret it,
      and returns a suggested commodity category + sub-filter values.
Called by: htmx_views.py AI search endpoint
Depends on: claude_client, commodity_registry
"""

from loguru import logger

from app.services.commodity_registry import COMMODITY_SPEC_SEEDS, COMMODITY_TREE, get_all_commodities


def _build_commodity_summary() -> str:
    """Build a compact summary of available commodities and their spec fields.

    Kept concise to minimize token usage on every Haiku call.
    """
    lines = []
    all_commodities = get_all_commodities()
    for commodity in all_commodities:
        seeds = COMMODITY_SPEC_SEEDS.get(commodity)
        if seeds:
            spec_names = [s["display_name"] for s in seeds[:5]]
            lines.append(f"- {commodity}: {', '.join(spec_names)}")
        else:
            lines.append(f"- {commodity}")
    return "\n".join(lines)


def _build_enum_reference() -> str:
    """Build a compact reference of enum values for each commodity's spec fields.

    This helps the AI return exact matching enum values instead of guessing.
    """
    lines = []
    for commodity, seeds in COMMODITY_SPEC_SEEDS.items():
        for seed in seeds:
            if seed.get("data_type") == "enum" and seed.get("enum_values"):
                vals = ", ".join(seed["enum_values"][:10])
                lines.append(f"  {commodity}.{seed['spec_key']}: [{vals}]")
    return "\n".join(lines)


_SYSTEM_PROMPT = """\
You are an electronic component search assistant. Given a natural language query, \
determine the most relevant commodity category and filter values.

Available commodity categories (with filterable spec fields):
{commodity_summary}

Enum values reference:
{enum_reference}

Rules:
1. Return ONLY valid JSON, no markdown, no explanation.
2. commodity must be one of the exact category keys listed above, or empty string if unclear.
3. filters should map spec_key to value(s) that match the enum values or numeric ranges defined.
4. For enum filters, use an array of matching values.
5. For numeric filters, use {{spec_key}}_min and/or {{spec_key}}_max with numeric values.
6. Only include filters you are confident about from the query.
7. If the query is too vague, return commodity and empty filters."""

_USER_PROMPT = """\
Interpret this electronic component search query and suggest category + filters.

Query: "{query}"

Reply with ONLY a JSON object:
{{"commodity": "category_key", "filters": {{"spec_key": ["value1"], "other_key_min": 123}}, "summary": "short description of interpretation"}}"""


async def interpret_search_query(query: str) -> dict | None:
    """Interpret a natural language query using Claude Haiku.

    Args:
        query: Natural language search query (e.g. "DDR5 32GB ECC server memory")

    Returns:
        Dict with keys: commodity (str), filters (dict), summary (str)
        Returns None on any failure (API error, parse error, etc.)
    """
    if not query or len(query.strip().split()) < 3:
        return None

    try:
        from app.utils.claude_client import claude_json

        commodity_summary = _build_commodity_summary()
        enum_reference = _build_enum_reference()

        system_prompt = _SYSTEM_PROMPT.format(
            commodity_summary=commodity_summary,
            enum_reference=enum_reference,
        )
        user_prompt = _USER_PROMPT.format(query=query)

        result = await claude_json(
            prompt=user_prompt,
            system=system_prompt,
            model_tier="fast",
            max_tokens=300,
        )

        if result is None:
            logger.warning("AI search: no result from Claude")
            return None

        if not isinstance(result, dict):
            logger.warning("AI search: unexpected result type: {}", type(result))
            return None

        # Validate commodity is in our known list
        commodity = result.get("commodity", "")
        all_commodities = get_all_commodities()
        if commodity and commodity not in all_commodities:
            # Try lowercase match
            commodity_lower = commodity.lower().strip()
            if commodity_lower in all_commodities:
                result["commodity"] = commodity_lower
            else:
                logger.info("AI search: unknown commodity '{}', clearing", commodity)
                result["commodity"] = ""

        # Validate filters structure
        filters = result.get("filters", {})
        if not isinstance(filters, dict):
            result["filters"] = {}

        # Ensure summary exists
        if "summary" not in result:
            result["summary"] = f"{result.get('commodity', 'unknown')} search"

        logger.info(
            "AI search interpreted '{}' -> commodity={}, filters={}",
            query[:50],
            result.get("commodity"),
            list(result.get("filters", {}).keys()),
        )
        return result

    except Exception as e:
        logger.warning("AI search interpretation failed: {}", e)
        return None


def get_parent_for_commodity(commodity: str) -> str:
    """Find the parent group name for a commodity in the tree."""
    for group, subs in COMMODITY_TREE.items():
        if commodity in subs:
            return group
    return ""
