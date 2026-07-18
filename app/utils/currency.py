"""app/utils/currency.py — Approximate FX-to-USD conversion for search scoring only.

What it does: Provides a static, hand-maintained table of approximate exchange
rates (foreign currency -> USD) and a ``to_usd()`` helper that converts a price
in an arbitrary currency into an approximate USD value so vendor prices can be
COMPARED across currencies during sighting scoring (median-price baselines,
per-offer price-factor competitiveness).

This is explicitly SCORING-ONLY. The rates below are a static snapshot, not a
live feed — they drift from the real market over time and MUST NOT be used for
invoicing, accounting, PO totals, or any customer-facing price display. Those
paths must keep showing the vendor's original ``unit_price`` + ``currency``
unchanged; only the scoring math (median computation, price-factor comparison)
should run through ``to_usd()``.

Called by: app.search_service (quick_search_mpn, _save_sightings)
Depends on: nothing (pure, no I/O, no DB)
"""

from loguru import logger

# Approximate currency -> USD conversion rates (1 unit of currency = N USD).
# SCORING-ONLY static snapshot — update periodically from a market source when
# rates drift meaningfully; never treat as a live feed. Currencies observed
# from supplier connectors (BrokerBin, Nexar/Octopart, DigiKey, Mouser,
# Sourcengine, element14, OEMSecrets, eBay, AI web search) should have an
# entry here; anything missing falls through to the "assume USD" behavior in
# ``to_usd()``.
FX_TO_USD: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "JPY": 0.0064,
    "CNY": 0.14,
    "HKD": 0.13,
    "TWD": 0.031,
    "CAD": 0.73,
    "AUD": 0.66,
    "CHF": 1.12,
    "SGD": 0.74,
    "INR": 0.012,
    "KRW": 0.00072,
    "MXN": 0.051,
    "BRL": 0.17,
    "SEK": 0.095,
    "NOK": 0.091,
    "DKK": 0.145,
    "PLN": 0.25,
    "ILS": 0.27,
    "MYR": 0.21,
    "THB": 0.028,
    "PHP": 0.017,
    "VND": 0.00004,
    "ZAR": 0.055,
}


def to_usd(amount: float | None, currency: str | None) -> float | None:
    """Convert *amount* in *currency* to an approximate USD value for scoring.

    Behavior:
    - ``amount is None`` -> returns ``None`` unchanged (callers keep their
      existing None-guards; nothing to convert).
    - Missing/blank/unrecognized *currency* -> assumes the amount is already
      USD (matches behavior before FX conversion existed — most connectors
      report bare numbers with an implicit USD currency).
    - Recognized currency -> ``amount * FX_TO_USD[currency]``.

    Not a validator: does not raise on bad input, since scoring must never
    fail a search over a currency-conversion hiccup.
    """
    if amount is None:
        return None
    if not currency:
        return amount
    rate = FX_TO_USD.get(currency.strip().upper())
    if rate is None:
        logger.debug("currency.to_usd: unrecognized currency {!r}, assuming USD for scoring", currency)
        return amount
    return amount * rate
