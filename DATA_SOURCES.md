# AVAIL Data Sources — Master Reference

**Total Sources: 27** (8 with connectors built, 19 pending setup)

---

## 🟢 LIVE — Connectors Built & Wired Into Search Pipeline

These have full Python connectors, are registered in the search service, and will fire automatically when env vars are set.

> **eBay is worker-backed (2026-09-05).** It is still on this list and still uses
> `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`, but its search no longer runs inside a
> user's request. Requirements are queued to `ebay_search_queue` and the
> `avail-ebay-worker` systemd unit polls the Browse API out of process
> (`app/services/ebay_worker`), writing Sightings asynchronously — the same shape as
> the ICS / NetComponents / The Broker Forum browser workers, but over HTTPS instead
> of a browser. `EbayConnector` remains live for the Settings → Connectors Test
> button, the health_monitor credential ping, and eBay title mining during material
> enrichment.

| # | Source | Type | Auth Method | Env Vars Needed | Signup URL | Notes |
|---|--------|------|-------------|-----------------|------------|-------|
| 1 | **Octopart (Nexar)** | Aggregator | OAuth2 client_credentials | `NEXAR_CLIENT_ID`, `NEXAR_CLIENT_SECRET` | nexar.com/api | GraphQL. 1000 queries/month free. Returns sellers, prices, authorized status. |
| 2 | **BrokerBin** | Broker | API key + username | `BROKERBIN_API_KEY`, `BROKERBIN_API_SECRET` | brokerbin.com | REST v2. Independent broker/distributor inventories. Contact sales for API. |
| 3 | **eBay** | Marketplace | OAuth2 client_credentials | `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | developer.ebay.com | Browse API, **worker-backed** (`avail-ebay-worker`). Surplus/used parts. Need production access approval. Searches all of eBay by default (no category filter); results pass a strict part-number-in-title filter, and "For parts or not working" + auction-only listings are dropped. Paced by `EBAY_DAILY_CALL_BUDGET` (default 4000 calls/UTC day) — see `.env.ebay-worker.example`. |
| 4 | **DigiKey** | Authorized | OAuth2 client_credentials | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` | developer.digikey.com | Product Search v4. Real-time pricing/inventory. Free tier. |
| 5 | **Mouser** | Authorized | API key | `MOUSER_API_KEY` | mouser.com/api-hub | Search API v2. Up to 50 results per query. Choose locale at signup. |
| 6 | **OEMSecrets** | Aggregator | API key | `OEMSECRETS_API_KEY` | oemsecrets.com/api | META-AGGREGATOR — one call gets 140+ distributors (DigiKey, Mouser, Arrow, Avnet, Farnell, RS, Future, TME). Highest ROI single source. |
| 7 | **Sourcengine** | Aggregator | Bearer token | `SOURCENGINE_API_KEY` | dev.sourcengine.com | B2B marketplace. MPN search across global supplier network. |
| 8 | **Email Intelligence (M365)** | Internal | Via Azure OAuth | `EMAIL_MINING_ENABLED=true` | (uses existing login) | Scans Outlook inbox for vendor offers, stock lists, contact info. Auto-enriches vendor cards. |

### Priority Setup Order:
1. **OEMSecrets** — single API covers 140+ distributors (may overlap with DigiKey/Mouser but catches Arrow, Avnet, Farnell, RS, Future, etc.)
2. **eBay** — easy OAuth, covers surplus/used market nobody else has (enter the keys in Settings → Connectors; the worker reads them DB-first)
3. **DigiKey** — direct authorized pricing
4. **Mouser** — direct authorized pricing
5. **Sourcengine** — B2B marketplace, different vendor pool
6. **Email Mining** — just flip the env var, uses existing Graph API auth

---

## 🟡 PENDING — Setup Required

### APIs (have or likely have developer programs):

| # | Source | Type | Est. Effort | Signup URL | Priority | Notes |
|---|--------|------|-------------|------------|----------|-------|
| 9 | **Arrow Electronics** | Authorized | Medium | developers.arrow.com | HIGH | #1 global distributor ($28B). OAuth2 API program. |
| 10 | **Newark / element14 / Farnell** | Authorized | Medium | partner.element14.com | HIGH | Part of Avnet. Single API covers 3 regional brands. |
| 11 | **RS Components** | Authorized | Medium | developerportal.rs-online.com | HIGH | 700K+ products. OAuth2 REST API. |
| 12 | **Future Electronics** | Authorized | Medium | futureelectronics.com | MEDIUM | Top 3 global distributor. Contact for API access. |
| 13 | **TME (Transfer Multisort)** | Authorized | Low | developers.tme.eu | MEDIUM | European distributor. Well-documented REST API. |
| 14 | **Rochester Electronics** | Authorized | Medium | rocelec.com | HIGH | EOL/obsolete specialist. Critical for legacy parts. |
| 15 | **Verical (Arrow)** | Marketplace | Low | verical.com | LOW | Arrow's marketplace. May be accessible via Arrow API. |
| 16 | **Heilind** | Authorized | Medium | heilind.com | LOW | Connectors, relays, sensors specialist. |
| 17 | **WIN SOURCE** | Broker | Medium | win-source.net | LOW | 1M+ SKUs. Strong China-sourced components. |
| 18 | **PartFuse** | Aggregator | Low | rapidapi.com | LOW | Unified API (DigiKey+Mouser+TME) on RapidAPI. Good backup. |
| 19 | **SiliconExpert / Z2Data** | Intelligence | Medium | siliconexpert.com/api | MEDIUM | Not a seller — lifecycle data, cross-refs, compliance. Enriches material cards. |

### Scrapers (no public API — need browser automation):

| # | Source | Type | Est. Effort | Signup URL | Priority | Notes |
|---|--------|------|-------------|------------|----------|-------|
| 20 | **NetComponents** | Broker | High | netcomponents.com | HIGH | 60M+ line items. Shows vendor name AND contact info. Highest value scrape target. |
| 21 | **IC Source** | Broker | High | icsource.com | MEDIUM | Membership-based trading platform. |
| 22 | **The Broker Forum (TBF)** | Broker | Medium | brokerforum.com | MEDIUM | 60M+ items. Has "XML Search" — investigate if it's an API. |
| 23 | **FindChips (Supplyframe)** | Aggregator | Medium | findchips.com | LOW | Owned by Siemens/Supplyframe. Well-structured pages. |
| 24 | **LCSC Electronics** | Authorized | Low | lcsc.com | LOW | Chinese distributor. Unofficial internal JSON API. No auth needed. |
| 25 | **AliExpress** | Marketplace | Medium | developers.aliexpress.com | LOW | Reference pricing and Chinese supplier discovery. Has affiliate API. |

### Manual / Internal:

| # | Source | Type | Est. Effort | Priority | Notes |
|---|--------|------|-------------|----------|-------|
| 26 | **Vendor Stock List Import** | Internal | Medium | HIGH | Buyers upload Excel/CSV stock lists. Auto-parse into sightings + vendor cards. |
| 27 | **Email Inbox Mining** | Internal | Done | DONE | Already built — flip `EMAIL_MINING_ENABLED=true` to activate. |

---

## Architecture Notes

### How It Works
- All connectors extend `BaseConnector` with automatic retry (2 retries, exponential backoff)
- `search_service.py` fires ALL configured connectors in **parallel** via `asyncio.gather`
- Each connector tracks stats: total searches, total results, avg response time, last success/error
- Results deduplicated by (vendor_name, mpn, sku) before saving
- Material cards auto-updated with vendor history after each search
- Data Sources management UI at `/` → "Data Sources" nav button

### Adding a New API Source
1. Create connector in `app/connectors/new_source.py` extending `BaseConnector`
2. Add env vars to `app/config.py`
3. Add connector to `search_service.py` `_fetch_fresh()` and `_CONNECTOR_SOURCE_MAP`
4. Add connector to `main.py` `_get_connector_for_source()` (for test button)
5. Add seed entry in `main.py` `_seed_api_sources()`
6. Set env vars in `.env` on server → connector auto-activates

### Env Vars Template (.env)
```bash
# ── Live Connectors ──
NEXAR_CLIENT_ID=
NEXAR_CLIENT_SECRET=
BROKERBIN_API_KEY=
BROKERBIN_API_SECRET=
EBAY_CLIENT_ID=
EBAY_CLIENT_SECRET=
DIGIKEY_CLIENT_ID=
DIGIKEY_CLIENT_SECRET=
MOUSER_API_KEY=
OEMSECRETS_API_KEY=
SOURCENGINE_API_KEY=
EMAIL_MINING_ENABLED=true
EMAIL_MINING_LOOKBACK_DAYS=180
```
