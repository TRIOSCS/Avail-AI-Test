"""
Purpose: Application configuration from environment variables.
Description: All settings loaded via pydantic-settings BaseSettings with typed
    fields and validation. Fail-fast at startup if a required value is missing
    or has the wrong type.
Business Rules: DATABASE_URL must be postgresql:// (or sqlite:// when TESTING=1).
Called by: Nearly every module in the app.
Depends on: pydantic-settings.
"""

import os

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_VERSION = "3.1.0"

# Microsoft Graph API scopes — single source of truth for auth + token refresh.
# These must match exactly to prevent scope loss after token refresh.
GRAPH_SCOPES = (
    "openid profile email offline_access "
    "Mail.Send Mail.ReadWrite Contacts.Read MailboxSettings.Read User.Read "
    "Files.ReadWrite Chat.ReadWrite Calendars.Read "
    "ChannelMessage.Send Team.ReadBasic.All Channel.ReadBasic.All "
    "Presence.Read.All"
)


def _csv_to_list(val: str | list[str], *, lower: bool = True) -> list[str]:
    """Split a comma-separated string into a cleaned list."""
    if isinstance(val, list):
        return [v.lower() for v in val] if lower else val
    items = [v.strip() for v in val.split(",") if v.strip()]
    return [v.lower() for v in items] if lower else items


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---
    app_url: str = "http://localhost:8000"
    secret_key: str = "change-me-in-production"
    encryption_salt: str = ""
    database_url: str = "postgresql://availai:availai@db:5432/availai"

    # --- Sentry ---
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.1
    sentry_profiles_sample_rate: float = 0.1

    # --- Rate limiting ---
    # Global per-IP default applied by SlowAPIMiddleware to every route that lacks its
    # own @limiter.limit. 600/minute (10 req/s sustained) is sized for an htmx-heavy
    # admin session: several 2s status pollers (30 req/min each), 3s outreach/datasheet
    # pollers (20/min), plus badges and the per-navigation partial fan-out routinely put
    # a single active tab well past 120/min — the old default would 429 the user's OWN
    # UI. 600/min absorbs that headroom while still capping abuse from one IP. Streaming
    # (SSE) and infra (/health,/metrics) endpoints are @limiter.exempt (see app/main.py).
    rate_limit_default: str = "600/minute"
    rate_limit_enabled: bool = True

    # --- Cross-app alerts ---
    alert_recency_days: int = 30  # FYI alerts only count items newer than this
    alerts_epoch: str = ""  # ISO datetime; FYI items dated before this never count (default: no epoch floor)

    # --- Redis ---
    redis_url: str = "redis://redis:6379/0"
    cache_backend: str = "redis"

    # --- Microsoft Azure OAuth ---
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = ""
    # Company-wide SharePoint datasheet library (app-only Graph). Empty = storage skipped.
    datasheet_library_drive_id: str = ""
    datasheet_library_subpath: str = "Datasheets"

    # --- Nexar (Octopart) ---
    nexar_client_id: str = ""
    nexar_client_secret: str = ""
    octopart_api_key: str = ""

    # --- BrokerBin ---
    brokerbin_api_key: str = ""
    brokerbin_api_secret: str = ""

    # --- eBay ---
    ebay_client_id: str = ""
    ebay_client_secret: str = ""

    # --- DigiKey ---
    digikey_client_id: str = ""
    digikey_client_secret: str = ""

    # --- Mouser ---
    mouser_api_key: str = ""

    # --- OEMSecrets ---
    oemsecrets_api_key: str = ""

    # --- Sourcengine ---
    sourcengine_api_key: str = ""

    # --- element14 / Newark ---
    element14_api_key: str = ""

    # --- AI ---
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # --- OEM Spec Code Resolver ---
    # Feature flag; resolver only fires when enabled. Min confidence is the
    # absolute floor (post web-search penalty) below which results are dropped
    # rather than written to oem_spec_codes_pending. Model tier selects which
    # claude_client.MODELS entry the resolver routes through — "opus" is the
    # default for hallucination-resistance on a low-volume grounded call.
    spec_resolver_enabled: bool = False
    spec_resolver_min_confidence: float = 0.3
    spec_resolver_model_tier: str = "opus"

    # --- Agent service-to-service auth ---
    agent_api_key: str = ""

    # --- Explorium / Vibe Prospecting ---
    explorium_api_key: str = ""
    explorium_api_base_url: str = "https://api.explorium.ai"
    # Opt-in: Explorium only runs when explicitly enabled AND a key is configured.
    # (Off by default — it was previously always-on, which wasted calls when the
    # integration wasn't actually working.)
    explorium_enrichment_enabled: bool = False
    explorium_cooldown_minutes: int = 15  # quota/rate-limit circuit cooldown

    # --- Customer enrichment ---
    customer_enrichment_enabled: bool = True
    customer_enrichment_cooldown_days: int = 90
    customer_enrichment_contacts_per_account: int = 5

    # --- Material card AI enrichment ---
    # Deterministic MPN→spec decoders (storage/DRAM): zero-network, zero-LLM, regex-gated.
    # Safe to leave on — values are enum-validated by record_spec. See app/services/mpn_decoder.
    mpn_decode_enabled: bool = True
    # Deterministic description→spec extraction (storage/DRAM token grammar): zero-network,
    # zero-LLM. Runs between mpn-decode (0.95) and the AI spec reader (0.85). Safe to leave
    # on — values are enum-validated by record_spec. See app/services/desc_extractor.
    desc_parse_enabled: bool = True
    # PartSurfer description enrichment: for UNCATEGORIZED HP/HPE cards, fetch the OEM's own
    # verbatim description live from partsurfer.hpe.com (robots-allowed, 1 GET / 2s) and feed
    # it into the desc grammar to categorize the card. Default ON — it's the win for the ~70k
    # uncategorized HP spares; cheap, free, robots-allowed. Values arbitrate through the F1
    # ladder at partsurfer_desc (tier 84). See app/services/enrichment_worker/partsurfer_resolver.
    partsurfer_desc_enabled: bool = True
    connector_desc_harvest_enabled: bool = True
    # eBay-title mining: eBay listing TITLES are free-text part descriptions; feed each into
    # the desc grammar (categorize uncategorized cards + fill facets) at ebay_title/tier 83,
    # arbitrated by the F1 ladder. Dormant no-op until EBAY_CLIENT_ID/EBAY_CLIENT_SECRET are
    # set (creds absent → skipped, like every other connector). See enrichment.harvest_ebay_titles.
    ebay_title_mining_enabled: bool = True
    # Cap on live PartSurfer fetches per batch (each is a polite 1-req/2s GET — the worker
    # paces them). Keeps a batch's wall-time bounded; uncategorized HP cards drain over batches.
    partsurfer_fetch_per_batch: int = 5
    # Deterministic FRU crosswalk decode (IBM/Lenovo FRU → approved mfg models): zero-network,
    # zero-LLM — strict-intersects the regex-gated decodes of a FRU's mfg_model links. Runs
    # between mpn-decode (0.95) and desc-parse (0.90) at 0.93. Safe to leave on — values are
    # enum-validated by record_spec. See app/services/fru_crosswalk_enrich.
    fru_crosswalk_enrich_enabled: bool = True
    # Widen the FRU crosswalk DECODE channel to also decode the drive_pn rel_kind's related
    # parts (today the decode channel reads mfg_model links only; drive_pn rows still feed the
    # DESC channel regardless of this flag). GATED on a measured misread rate: a 100-row dry-run
    # (app.management.run_fru_crosswalk --measure-drive-pn) found 0/3328 drive_pn related parts
    # decode at all (they are IBM/Lenovo FRU numbers, not canonical manufacturer MPNs), so the
    # OEM-firmware-suffix misread rate is 0% (≤2% gate) — safe to default ON. The regex gate in
    # decode_mpn is the source of truth, so a non-canonical drive_pn never misdecodes; record_spec
    # re-validates every value. Flip OFF only if a future drive_pn ingest carries canonical models
    # whose firmware suffixes are re-measured above the 2% gate. See app/services/fru_crosswalk_enrich.
    fru_crosswalk_drive_pn_decode_enabled: bool = True
    # OEM web-resolution crosswalk (PartSurfer/PSREF spare → canonical MPN cache): gates BOTH
    # worker passes — Pass B (the deterministic tier-80 writer over cached oem_crosswalk rows:
    # zero-network, zero-LLM, safe-on) and Pass A (the paced Claude-grounded resolution, which
    # is ADDITIONALLY inert until the per-batch/daily caps allow — see EnrichmentWorkerConfig
    # oem_resolve_per_batch / oem_resolve_daily_cap). See app/services/oem_crosswalk_enrich.
    oem_crosswalk_enrich_enabled: bool = True
    # Worker lane split (call routing only — never write pre-gating; the F1 ladder still
    # arbitrates every write). Bulk-lane cards (enrich_requested_at IS NULL) run the FREE
    # connectors + deterministic passes only: the web tier (extract_part_from_web), the
    # OEM tiers (cross_reference_mpn / extract_oem_description) and the Opus infer_part
    # fallback are skipped (measured ~$6-10/day for ~0 accepted writes). Priority-lane
    # cards (user single-add stamps enrich_requested_at) keep the full pipeline.
    # Env: ENRICHMENT_LANE_SPLIT_ENABLED.
    enrichment_lane_split_enabled: bool = True
    # Skip extract_part_from_web for OEM/FRU-shaped MPNs (any classify_oem_vendor hit:
    # HP 6digit-3 / option-kit / L-series, Lenovo/IBM FRU shapes, EMC 303-x, Dell 5-char,
    # Acer, ASUS) on EVERY lane — the measured ~95% no-trusted-source reject class
    # (reseller-only pages). The OEM tiers + Opus fallback still run on the priority
    # lane. Interim until the direct-HTTP OEM resolver lands.
    # Env: ENRICHMENT_SKIP_WEB_FOR_OEM_MPNS.
    enrichment_skip_web_for_oem_mpns: bool = True

    # --- AI Features ---
    ai_features_enabled: str = "mike_only"  # "all", "mike_only", "off"

    # --- Email Intelligence ---
    email_mining_enabled: bool = False
    # Cap on how many mined domains (top N by inbox volume) get an eager Explorium
    # match per batch. The long tail is created unenriched and enriched on demand later.
    # This is the only metered-spend lever for the email-mining path.
    email_mining_enrich_cap: int = 25
    # Daily ceiling on the number of Claude requests the email-mining inbox-parse BATCH
    # path (email_service._submit_parse_batch / its sequential fallback) may dispatch per
    # UTC day. Every pending vendor reply = one fast-tier Claude call billed to
    # cost_bucket="email_mining"; without a cap a large first-time inbox backfill could
    # submit thousands of requests unbounded. When the day's metered+submitted call count
    # reaches this cap the batch stops enqueuing and logs (raw rows stay re-parsable next
    # day). Reuses the claude_usage:email_mining metering counters as today's spend and
    # mirrors the enrichment-worker daily_cap / ai_screen_daily_cap count-cap pattern.
    # 0 (or negative) disables the cap -> pre-cap unbounded behavior (graceful default).
    email_mining_batch_daily_cap: int = 1000

    # --- M365 Integration v2 ---
    inbox_scan_interval_min: int = 30
    digest_cooldown_seconds: int = 120  # min seconds between AI digest regenerations per entity
    inbox_backfill_days: int = 180
    contacts_sync_enabled: bool = True

    # --- Admin (CSV env var, parsed to list[str] by model_validator) ---
    admin_emails: str | list[str] = ""
    # When true (go-live posture), an unknown email with no pre-provisioned user row is
    # rejected at login instead of auto-provisioned. ADMIN_EMAILS always bypass.
    enable_user_allowlist: bool = True

    # --- RFQ ---
    follow_up_days: int = 3

    # --- Activity tracking & customer ownership ---
    activity_tracking_enabled: bool = True
    # When on, the nightly ownership sweep runs in WARNINGS-ONLY mode: it emails owners
    # of accounts approaching the company inactivity threshold but never clears ownership
    # (the SP4 account sweep is the single park+cooldown+notify path). See
    # account_sweep_inactivity_days for the one threshold both read.
    ownership_sweep_enabled: bool = False
    # Site-level ownership sweep + activity-health dashboards only. Company-ownership
    # dormancy is governed by the single account_sweep_inactivity_days threshold below.
    customer_inactivity_days: int = 30
    strategic_inactivity_days: int = 90
    customer_warning_days: int = 23
    vendor_protection_warn_days: int = 60

    # --- Proactive offers ---
    proactive_matching_enabled: bool = True
    proactive_throttle_days: int = 21
    proactive_scan_interval_hours: int = 4
    proactive_min_margin_pct: float = 10.0
    proactive_match_expiry_days: int = 30

    # --- Buy plan (CSV env vars, parsed to list[str] by model_validator) ---
    stock_sale_vendor_names: str | list[str] = "trio,trio supply chain,stock,internal"
    stock_sale_notify_emails: str | list[str] = "logistics@trioscs.com,accounting@trioscs.com"
    buyplan_auto_complete_hour: int = 18
    buyplan_auto_complete_tz: str = "America/New_York"
    po_verify_interval_min: int = 30

    # --- Buy Plan V3 ---
    buyplan_stale_offer_days: int = 5
    sighting_stale_days: int = 3  # Days before a requirement is flagged stale
    buyplan_min_margin_pct: float = 10
    buyplan_nudge_buyer_hours: int = 4
    buyplan_nudge_ops_hours: int = 2
    buyplan_favoritism_threshold_pct: float = 60
    buyplan_better_offer_pct: float = 5

    # --- Vendor-part unavailability ("Two Windows, Real Proof" temporal policy) ---
    # Read-time suppression windows (stateless predicate, sighting_stale_days
    # precedent): changing a knob re-evaluates EXISTING marks at the next render —
    # there is no stored expiry. different_part never expires by design (identity
    # knowledge, not stock state) — deliberately a hard-coded constant, not a knob.
    unavailability_suppress_days: int = Field(default=30, ge=1)  # LOT: bought_by_us/sold_elsewhere/broken/other
    unavailability_listing_suppress_days: int = Field(default=180, ge=1)  # LISTING: not_really_there
    # O2 restock override: fresh qty must be >= factor x qty_at_mark AND strictly
    # greater — strict-greater holds even at factor 1.0, so an identical echo can
    # never surface the row (un-suppress) regardless of misconfiguration. O2 is
    # row-level only; it never writes released_at (only O3 and the offer hook do).
    unavailability_qty_jump_factor: float = Field(default=2.0, ge=1.0)

    # --- Search ---
    search_concurrency_limit: int = 10
    # Total wall-clock budget for one _fetch_fresh() fan-out. Slower connectors
    # are cancelled when exceeded so the orchestrator returns partial results
    # well under Caddy's 30s lb_try_duration.
    search_total_timeout_s: float = 12.0

    # --- Contact intelligence ---
    contact_scoring_enabled: bool = True
    contact_nudge_dormant_days: int = 30
    contact_nudge_cooling_days: int = 14

    # --- Own company domains (CSV env var, parsed to frozenset[str] by model_validator) ---
    own_domains: str | frozenset[str] = "trioscs.com"

    # --- 8x8 Work Analytics ---
    eight_by_eight_api_key: str = ""
    eight_by_eight_username: str = ""
    eight_by_eight_password: str = ""
    eight_by_eight_pbx_id: str = ""
    eight_by_eight_timezone: str = "America/Los_Angeles"
    eight_by_eight_enabled: bool = False
    eight_by_eight_poll_interval_minutes: int = 30

    # --- Lusha Enrichment (key via get_credential_cached, NOT a Settings field) ---
    lusha_enrichment_enabled: bool = False  # feature gate; off → chain == today
    lusha_cooldown_minutes: int = 15  # quota/rate-limit (402/429) circuit cooldown
    prospect_enrich_contacts_per_account: int = 5  # cap for paid contact pulls

    # --- Hunter.io Enrichment ---
    # On by default; degrades cleanly when HUNTER_API_KEY is absent (no key → contact
    # fetch returns [] without an outbound call — never raises).
    hunter_enrichment_enabled: bool = True  # feature gate; off → Hunter not triggered

    # --- SAM.gov Enrichment ---
    # On by default; free public API. When SAM_GOV_API_KEY is absent the connector uses
    # the public DEMO_KEY tier and degrades to None on any error — never raises.
    sam_gov_enrichment_enabled: bool = True  # feature gate; off → SAM.gov not triggered

    # --- Worker liveness watchdog (scheduler job in the supervised app) ---
    # Workers heartbeat every loop tick; this job alerts when one that should be
    # running goes silent (hung/crashed) or trips its circuit breaker.
    worker_liveness_check_minutes: int = 5
    worker_heartbeat_stale_minutes: int = 15
    worker_alert_debounce_minutes: int = 60

    # --- Clay Enrichment (MCP connector; CLAY_API_KEY via credential store) ---
    clay_enrichment_enabled: bool = False  # feature gate; off → Clay MCP not triggered
    clay_cooldown_minutes: int = 15  # quota/rate-limit circuit cooldown

    # --- Azure Communication Services ---
    acs_connection_string: str = ""
    acs_from_phone: str = ""  # ACS-provisioned phone number for caller ID (E.164 format)
    acs_callback_url: str = ""
    # Shared secret minted into the ACS/Event Grid webhook subscription URL as a
    # `?secret=` query param (Event Grid has no clientState-style body field like
    # Graph, so the secret travels in the URL instead). Required for the webhook
    # to accept events — unset/empty means POST /api/webhooks/acs fails closed
    # (403) even if ACS is otherwise configured. See app/routers/v13_features/activity.py.
    acs_webhook_secret: str = ""

    # --- MVP Mode ---
    # Now gates ONLY the Teams chat integration: the POST /api/webhooks/teams endpoint
    # (returns 404 when True) and Graph Teams-chat subscription creation in
    # ensure_all_users_subscribed. Dashboard/Analytics, Enrichment, and Task Manager were
    # un-gated by the module consolidation and are always-on regardless of this flag.
    # Default is False ("no MVP" — full surface incl. Teams; Teams subscription creation
    # degrades gracefully if the public webhook callback isn't configured yet).
    # Set MVP_MODE=true in .env only to suppress the Teams integration.
    mvp_mode: bool = False

    # --- Metrics ---
    metrics_token: str = ""  # Required token for /metrics endpoint (X-Metrics-Token header)

    # --- Backup monitoring ---
    backup_max_age_hours: int = 26  # Alert if last backup is older than this

    # --- Prospecting ---
    prospecting_enabled: bool = True
    prospecting_min_fit_for_contacts: int = 60
    prospecting_expire_days: int = 90

    # --- SP4: Account Reclamation ---
    # Nightly sweep: reassigns accounts inactive beyond the threshold from their owner
    # into the prospect pool for redistribution. Default off — enable at go-live.
    account_sweep_enabled: bool = False
    # THE single company-ownership inactivity threshold (default 45). Days without any
    # CRM activity (note, RFQ, meeting, email) at ANY of the company's sites before the
    # account is swept from its owner back into the prospect pool by SP4 job_account_sweep
    # (the one park+cooldown+notify path). Inactivity is measured across ALL sites: the
    # sweep reads get_last_activity_at (MAX activity over the parent company_id, which every
    # site activity carries), so a contact at ANY site keeps the whole account active. The
    # warnings-only ownership sweep reads the SAME value and emails the owner
    # WARNING_LEAD_DAYS before it — so the two never double-act on the same account.
    account_sweep_inactivity_days: int = 45
    # Post-park cooldown (default 30 days): the former owner cannot reclaim/claim a
    # freshly-swept account until swept_at + this many days passes (it stays in the pool
    # for OTHER reps to claim normally; after the cooldown it is open to anyone). Only a
    # manager/admin may put it back early, via reassign_account (which overrides the block).
    account_sweep_reclaim_cooldown_days: int = 30
    # Manager email that receives the nightly sweep digest (blank = no digest sent).
    account_sweep_manager_email: str = ""
    # Auto-surface previously swept accounts that have become active again (new RFQ,
    # inbound email, etc.) — re-adds them to the prospect pool as suggested.
    account_reactivation_sweep_enabled: bool = True

    # --- SP3: AI Account Screening ---
    # Feature gate — default off; flip on when ready to spend Claude credits on screening.
    ai_screen_enabled: bool = False
    # Minimum trio_match_score to pass the screen (< threshold → screened_out bucket).
    ai_screen_min_match: int = 40
    # Max accounts screened per UTC calendar day (mirrors enrichment daily_cap pattern).
    ai_screen_daily_cap: int = 200
    # When True, an insufficient_data verdict triggers a single web_search to try to
    # resolve grounding gaps before falling back to insufficient_data.
    ai_screen_web_search_enabled: bool = False

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        testing = os.getenv("TESTING", "").strip()
        if testing == "1" and v.startswith("sqlite"):
            return v
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must start with 'postgresql://'")
        return v

    @field_validator("sentry_traces_sample_rate", "sentry_profiles_sample_rate")
    @classmethod
    def validate_sample_rate(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("Sample rate must be between 0.0 and 1.0")
        return v

    @model_validator(mode="after")
    def parse_csv_fields(self) -> "Settings":
        """Parse CSV env-var strings into their proper collection types.

        Fields are declared as ``str | list[str]`` (or ``str | frozenset[str]``)
        so pydantic-settings can load plain CSV strings from dotenv, while the
        runtime type after this validator is always the collection type.
        """
        if isinstance(self.admin_emails, str):
            object.__setattr__(self, "admin_emails", _csv_to_list(self.admin_emails))
        if isinstance(self.stock_sale_vendor_names, str):
            object.__setattr__(self, "stock_sale_vendor_names", _csv_to_list(self.stock_sale_vendor_names))
        if isinstance(self.stock_sale_notify_emails, str):
            object.__setattr__(self, "stock_sale_notify_emails", _csv_to_list(self.stock_sale_notify_emails))
        if isinstance(self.own_domains, str):
            object.__setattr__(self, "own_domains", frozenset(_csv_to_list(self.own_domains)))
        return self


# Handle SESSION_SECRET → secret_key fallback before instantiation
if os.getenv("SESSION_SECRET") and not os.getenv("SECRET_KEY"):
    os.environ["SECRET_KEY"] = os.getenv("SESSION_SECRET", "")

settings = Settings()
