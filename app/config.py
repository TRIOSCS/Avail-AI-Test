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
    rate_limit_default: str = "120/minute"
    rate_limit_search: str = "20/minute"
    rate_limit_enabled: bool = True
    rate_limit_ai_search: str = "10/minute"

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

    # --- Tagging ---
    min_tag_confidence: float = 0.90

    # --- AI Features ---
    ai_features_enabled: str = "mike_only"  # "all", "mike_only", "off"

    # --- Email Intelligence ---
    email_mining_enabled: bool = False
    email_mining_lookback_days: int = 180

    # --- M365 Integration v2 ---
    inbox_scan_interval_min: int = 30
    digest_cooldown_seconds: int = 120  # min seconds between AI digest regenerations per entity
    inbox_backfill_days: int = 180
    contacts_sync_enabled: bool = True

    # --- Admin (CSV env var, parsed to list[str] by model_validator) ---
    admin_emails: str | list[str] = ""

    # --- RFQ ---
    follow_up_days: int = 3

    # --- Activity tracking & customer ownership ---
    activity_tracking_enabled: bool = True
    ownership_sweep_enabled: bool = False
    customer_inactivity_days: int = 30
    strategic_inactivity_days: int = 90
    customer_warning_days: int = 23
    offer_attribution_days: int = 14
    vendor_protection_warn_days: int = 60
    vendor_protection_drop_days: int = 90
    routing_window_hours: int = 48
    collision_lookback_days: int = 7

    # --- Proactive offers ---
    proactive_matching_enabled: bool = True
    proactive_throttle_days: int = 21
    proactive_scan_interval_hours: int = 4
    excess_bid_scan_enabled: bool = True
    excess_bid_parse_lookback_days: int = 14
    proactive_min_margin_pct: float = 10.0
    proactive_match_expiry_days: int = 30

    # --- Buy plan (CSV env vars, parsed to list[str] by model_validator) ---
    stock_sale_vendor_names: str | list[str] = "trio,trio supply chain,stock,internal"
    stock_sale_notify_emails: str | list[str] = "logistics@trioscs.com,accounting@trioscs.com"
    buyplan_auto_complete_hour: int = 18
    buyplan_auto_complete_tz: str = "America/New_York"
    po_verify_interval_min: int = 30

    # --- Buy Plan V3 ---
    buyplan_auto_approve_threshold: float = 5000
    buyplan_stale_offer_days: int = 5
    sighting_stale_days: int = 3  # Days before a requirement is flagged stale
    buyplan_min_margin_pct: float = 10
    buyplan_nudge_buyer_hours: int = 4
    buyplan_escalate_manager_hours: int = 8
    buyplan_nudge_ops_hours: int = 2
    buyplan_favoritism_threshold_pct: float = 60
    buyplan_better_offer_pct: float = 5

    # --- Vendor-part unavailability ("Two Windows, Real Proof" temporal policy) ---
    # Read-time suppression windows (stateless predicate, sighting_stale_days
    # precedent): changing a knob re-evaluates EXISTING marks at the next render —
    # there is no stored expiry. different_part never expires by design (identity
    # knowledge, not stock state) — deliberately a hard-coded constant, not a knob.
    unavailability_suppress_days: int = Field(30, ge=1)  # LOT: bought_by_us/sold_elsewhere/broken/other
    unavailability_listing_suppress_days: int = Field(180, ge=1)  # LISTING: not_really_there
    # O2 restock override: fresh qty must be >= factor x qty_at_mark AND strictly
    # greater — strict-greater holds even at factor 1.0, so an identical echo can
    # never surface the row (un-suppress) regardless of misconfiguration. O2 is
    # row-level only; it never writes released_at (only O3 and the offer hook do).
    unavailability_qty_jump_factor: float = Field(2.0, ge=1.0)

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

    # --- Apollo Enrichment ---
    apollo_api_key: str = ""

    # --- Lusha Enrichment (key via get_credential_cached, NOT a Settings field) ---
    lusha_enrichment_enabled: bool = False  # feature gate; off → chain == today
    lusha_cooldown_minutes: int = 15  # quota/rate-limit (402/429) circuit cooldown
    prospect_enrich_contacts_per_account: int = 5  # cap for paid contact pulls

    # --- Azure Communication Services ---
    acs_connection_string: str = ""
    acs_from_phone: str = ""  # ACS-provisioned phone number for caller ID (E.164 format)
    acs_callback_url: str = ""

    # --- MVP Mode ---
    # When True, disables: Dashboard/Analytics, Enrichment, Teams, Task Manager
    # Set MVP_MODE=false in .env to re-enable all features
    mvp_mode: bool = True

    # --- Frontend ---
    use_htmx: bool = True
    # Gates the merged v2 opportunity-table rendering on /requisitions2.
    # See docs/superpowers/specs/2026-04-21-opportunity-table-merged-design.md
    # Flip to false + restart to revert to legacy rendering with no code change.
    avail_opp_table_v2: bool = True

    # --- On-demand enrichment orchestrator ---
    on_demand_enrichment_enabled: bool = True

    # --- Metrics ---
    metrics_token: str = ""  # Required token for /metrics endpoint (X-Metrics-Token header)

    # --- Backup monitoring ---
    backup_max_age_hours: int = 26  # Alert if last backup is older than this

    # --- Prospecting ---
    prospecting_enabled: bool = True
    prospecting_min_fit_for_contacts: int = 60
    prospecting_expire_days: int = 90
    prospecting_resurface_days: int = 180

    # --- SP4: Account Reclamation ---
    # Nightly sweep: reassigns accounts inactive beyond the threshold from their owner
    # into the prospect pool for redistribution. Default off — enable at go-live.
    account_sweep_enabled: bool = False
    # Days without any CRM activity (note, RFQ, meeting, email) before an account is
    # swept from its owner back into the prospect pool.
    account_sweep_inactivity_days: int = 90
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

    @field_validator("min_tag_confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("Confidence/threshold must be between 0.0 and 1.0")
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
