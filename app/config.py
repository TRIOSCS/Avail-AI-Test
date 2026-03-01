"""Configuration — all settings from environment variables."""

import os

APP_VERSION = "3.0.0"

# Microsoft Graph API scopes — single source of truth for auth + token refresh.
# These must match exactly to prevent scope loss after token refresh.
GRAPH_SCOPES = (
    "openid profile email offline_access "
    "Mail.Send Mail.ReadWrite Contacts.Read MailboxSettings.Read User.Read "
    "Files.ReadWrite Chat.ReadWrite Calendars.Read "
    "ChannelMessage.Send Team.ReadBasic.All Channel.ReadBasic.All"
)


class Settings:
    app_url: str = os.getenv("APP_URL", "http://localhost:8000")
    secret_key: str = os.getenv("SESSION_SECRET") or os.getenv(
        "SECRET_KEY", "change-me-in-production"
    )
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://availai:availai@db:5432/availai"
    )

    # Sentry error tracking
    sentry_dsn: str = os.getenv("SENTRY_DSN", "")
    sentry_traces_sample_rate: float = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
    sentry_profiles_sample_rate: float = float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1"))

    # Rate limiting
    rate_limit_default: str = os.getenv("RATE_LIMIT_DEFAULT", "120/minute")
    rate_limit_search: str = os.getenv("RATE_LIMIT_SEARCH", "20/minute")
    rate_limit_enabled: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"

    # Redis caching
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    cache_backend: str = os.getenv("CACHE_BACKEND", "redis")

    # Microsoft Azure OAuth
    azure_client_id: str = os.getenv("AZURE_CLIENT_ID", "")
    azure_client_secret: str = os.getenv("AZURE_CLIENT_SECRET", "")
    azure_tenant_id: str = os.getenv("AZURE_TENANT_ID", "")

    # Nexar (Octopart) API
    nexar_client_id: str = os.getenv("NEXAR_CLIENT_ID", "")
    nexar_client_secret: str = os.getenv("NEXAR_CLIENT_SECRET", "")
    octopart_api_key: str = os.getenv("OCTOPART_API_KEY", "")

    # BrokerBin API
    brokerbin_api_key: str = os.getenv("BROKERBIN_API_KEY", "")
    brokerbin_api_secret: str = os.getenv("BROKERBIN_API_SECRET", "")

    # eBay Browse API
    ebay_client_id: str = os.getenv("EBAY_CLIENT_ID", "")
    ebay_client_secret: str = os.getenv("EBAY_CLIENT_SECRET", "")

    # DigiKey API
    digikey_client_id: str = os.getenv("DIGIKEY_CLIENT_ID", "")
    digikey_client_secret: str = os.getenv("DIGIKEY_CLIENT_SECRET", "")

    # Mouser API
    mouser_api_key: str = os.getenv("MOUSER_API_KEY", "")

    # OEMSecrets API
    oemsecrets_api_key: str = os.getenv("OEMSECRETS_API_KEY", "")

    # Sourcengine API
    sourcengine_api_key: str = os.getenv("SOURCENGINE_API_KEY", "")

    # element14 / Newark API
    element14_api_key: str = os.getenv("ELEMENT14_API_KEY", "")

    # AI parsing
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    # DigitalOcean Gradient AI (serverless inference)
    do_gradient_api_key: str = os.getenv("DO_GRADIENT_API_KEY", "")
    do_gradient_model: str = os.getenv(
        "DO_GRADIENT_MODEL", "anthropic-claude-sonnet-4-6"
    )

    # Agent service-to-service auth
    agent_api_key: str = os.getenv("AGENT_API_KEY", "")

    # Explorium / Vibe Prospecting API
    explorium_api_key: str = os.getenv("EXPLORIUM_API_KEY", "")
    explorium_api_base_url: str = os.getenv(
        "EXPLORIUM_API_BASE_URL", "https://api.explorium.ai"
    )

    # Enrichment APIs
    apollo_api_key: str = os.getenv("APOLLO_API_KEY", "")
    apollo_rate_limit_per_min: int = int(os.getenv("APOLLO_RATE_LIMIT_PER_MIN", "5"))
    apollo_monthly_credit_limit: int = int(os.getenv("APOLLO_MONTHLY_CREDIT_LIMIT", "10000"))

    # Deep Enrichment APIs
    hunter_api_key: str = os.getenv("HUNTER_API_KEY", "")
    hunter_monthly_search_limit: int = int(os.getenv("HUNTER_MONTHLY_SEARCH_LIMIT", "500"))
    hunter_monthly_verify_limit: int = int(os.getenv("HUNTER_MONTHLY_VERIFY_LIMIT", "500"))
    rocketreach_api_key: str = os.getenv("ROCKETREACH_API_KEY", "")
    clearbit_api_key: str = os.getenv("CLEARBIT_API_KEY", "")
    lusha_api_key: str = os.getenv("LUSHA_API_KEY", "")
    lusha_monthly_credit_limit: int = int(os.getenv("LUSHA_MONTHLY_CREDIT_LIMIT", "6400"))

    # Customer enrichment waterfall (Apollo → Hunter → Lusha phones → Other)
    customer_enrichment_enabled: bool = (
        os.getenv("CUSTOMER_ENRICHMENT_ENABLED", "true").lower() == "true"
    )
    customer_enrichment_cooldown_days: int = int(
        os.getenv("CUSTOMER_ENRICHMENT_COOLDOWN_DAYS", "90")
    )
    customer_enrichment_contacts_per_account: int = int(
        os.getenv("CUSTOMER_ENRICHMENT_CONTACTS_PER_ACCOUNT", "5")
    )

    # Deep Enrichment feature flags
    deep_enrichment_enabled: bool = (
        os.getenv("DEEP_ENRICHMENT_ENABLED", "false").lower() == "true"
    )
    deep_email_mining_enabled: bool = (
        os.getenv("DEEP_EMAIL_MINING_ENABLED", "false").lower() == "true"
    )
    deep_enrichment_auto_apply_threshold: float = float(
        os.getenv("DEEP_ENRICHMENT_AUTO_APPLY_THRESHOLD", "0.8")
    )
    deep_enrichment_review_threshold: float = float(
        os.getenv("DEEP_ENRICHMENT_REVIEW_THRESHOLD", "0.5")
    )
    deep_enrichment_stale_days: int = int(
        os.getenv("DEEP_ENRICHMENT_STALE_DAYS", "30")
    )

    # Material card AI enrichment
    material_enrichment_enabled: bool = (
        os.getenv("MATERIAL_ENRICHMENT_ENABLED", "true").lower() == "true"
    )
    material_enrichment_batch_size: int = int(
        os.getenv("MATERIAL_ENRICHMENT_BATCH_SIZE", "300")
    )

    # AI Features (Definitive Spec)
    ai_features_enabled: str = os.getenv(
        "AI_FEATURES_ENABLED", "mike_only"
    )  # "all", "mike_only", "off"

    # Email Intelligence
    email_mining_enabled: bool = (
        os.getenv("EMAIL_MINING_ENABLED", "false").lower() == "true"
    )
    email_mining_lookback_days: int = int(
        os.getenv("EMAIL_MINING_LOOKBACK_DAYS", "180")
    )

    # M365 Integration v2
    inbox_scan_interval_min: int = int(os.getenv("INBOX_SCAN_INTERVAL_MIN", "30"))
    inbox_backfill_days: int = int(os.getenv("INBOX_BACKFILL_DAYS", "180"))
    contacts_sync_enabled: bool = (
        os.getenv("CONTACTS_SYNC_ENABLED", "true").lower() == "true"
    )

    # Admin
    admin_emails: list = [
        e.strip().lower()
        for e in os.getenv("ADMIN_EMAILS", "").split(",")
        if e.strip()
    ]

    # Microsoft Teams (channel notifications)
    teams_webhook_url: str = os.getenv("TEAMS_WEBHOOK_URL", "")
    teams_team_id: str = os.getenv("TEAMS_TEAM_ID", "")
    teams_channel_id: str = os.getenv("TEAMS_CHANNEL_ID", "")
    teams_hot_threshold: float = float(os.getenv("TEAMS_HOT_THRESHOLD", "10000"))

    # RFQ follow-up threshold (days before a sent/opened email is stale)
    follow_up_days: int = int(os.getenv("FOLLOW_UP_DAYS", "3"))

    # Activity tracking & customer ownership (v1.3.0)
    activity_tracking_enabled: bool = (
        os.getenv("ACTIVITY_TRACKING_ENABLED", "true").lower() == "true"
    )
    customer_inactivity_days: int = int(os.getenv("CUSTOMER_INACTIVITY_DAYS", "30"))
    strategic_inactivity_days: int = int(os.getenv("STRATEGIC_INACTIVITY_DAYS", "90"))
    customer_warning_days: int = int(os.getenv("CUSTOMER_WARNING_DAYS", "23"))
    offer_attribution_days: int = int(os.getenv("OFFER_ATTRIBUTION_DAYS", "14"))
    vendor_protection_warn_days: int = int(
        os.getenv("VENDOR_PROTECTION_WARN_DAYS", "60")
    )
    vendor_protection_drop_days: int = int(
        os.getenv("VENDOR_PROTECTION_DROP_DAYS", "90")
    )
    routing_window_hours: int = int(os.getenv("ROUTING_WINDOW_HOURS", "48"))
    collision_lookback_days: int = int(os.getenv("COLLISION_LOOKBACK_DAYS", "7"))

    # Proactive offers
    proactive_matching_enabled: bool = (
        os.getenv("PROACTIVE_MATCHING_ENABLED", "true").lower() == "true"
    )
    proactive_archive_age_days: int = int(os.getenv("PROACTIVE_ARCHIVE_AGE_DAYS", "30"))
    proactive_throttle_days: int = int(os.getenv("PROACTIVE_THROTTLE_DAYS", "21"))
    proactive_scan_interval_hours: int = int(os.getenv("PROACTIVE_SCAN_INTERVAL_HOURS", "4"))
    proactive_min_margin_pct: float = float(os.getenv("PROACTIVE_MIN_MARGIN_PCT", "10.0"))
    proactive_match_expiry_days: int = int(os.getenv("PROACTIVE_MATCH_EXPIRY_DAYS", "30"))

    # Buy plan — stock sale detection & auto-complete
    stock_sale_vendor_names: list = [
        n.strip().lower()
        for n in os.getenv(
            "STOCK_SALE_VENDOR_NAMES", "trio,trio supply chain,stock,internal"
        ).split(",")
        if n.strip()
    ]
    stock_sale_notify_emails: list = [
        e.strip().lower()
        for e in os.getenv(
            "STOCK_SALE_NOTIFY_EMAILS", "logistics@trioscs.com,accounting@trioscs.com"
        ).split(",")
        if e.strip()
    ]
    buyplan_auto_complete_hour: int = int(os.getenv("BUYPLAN_AUTO_COMPLETE_HOUR", "18"))
    buyplan_auto_complete_tz: str = os.getenv("BUYPLAN_AUTO_COMPLETE_TZ", "America/New_York")
    po_verify_interval_min: int = int(os.getenv("PO_VERIFY_INTERVAL_MIN", "30"))

    # Buy Plan V3 — AI thresholds
    buyplan_auto_approve_threshold: float = float(
        os.getenv("BUYPLAN_AUTO_APPROVE_THRESHOLD", "5000")
    )
    buyplan_stale_offer_days: int = int(os.getenv("BUYPLAN_STALE_OFFER_DAYS", "5"))
    buyplan_min_margin_pct: float = float(os.getenv("BUYPLAN_MIN_MARGIN_PCT", "10"))
    buyplan_nudge_buyer_hours: int = int(os.getenv("BUYPLAN_NUDGE_BUYER_HOURS", "4"))
    buyplan_escalate_manager_hours: int = int(os.getenv("BUYPLAN_ESCALATE_MANAGER_HOURS", "8"))
    buyplan_nudge_ops_hours: int = int(os.getenv("BUYPLAN_NUDGE_OPS_HOURS", "2"))
    buyplan_favoritism_threshold_pct: float = float(
        os.getenv("BUYPLAN_FAVORITISM_THRESHOLD_PCT", "60")
    )
    buyplan_better_offer_pct: float = float(
        os.getenv("BUYPLAN_BETTER_OFFER_PCT", "5")
    )

    # Search concurrency
    search_concurrency_limit: int = int(os.getenv("SEARCH_CONCURRENCY_LIMIT", "10"))

    # Buy Plan V1 deprecation
    buy_plan_v1_enabled: bool = os.getenv("BUY_PLAN_V1_ENABLED", "true").lower() == "true"

    # Contact intelligence
    contact_scoring_enabled: bool = (
        os.getenv("CONTACT_SCORING_ENABLED", "true").lower() == "true"
    )
    contact_nudge_dormant_days: int = int(os.getenv("CONTACT_NUDGE_DORMANT_DAYS", "30"))
    contact_nudge_cooling_days: int = int(os.getenv("CONTACT_NUDGE_COOLING_DAYS", "14"))

    # Own company domains — used to filter internal emails from vendor threads
    own_domains: frozenset = frozenset(
        d.strip().lower()
        for d in os.getenv("OWN_DOMAINS", "trioscs.com").split(",")
        if d.strip()
    )

    # Prospecting module (Phase 8)
    prospecting_enabled: bool = (
        os.getenv("PROSPECTING_ENABLED", "true").lower() == "true"
    )
    prospecting_min_fit_for_contacts: int = int(
        os.getenv("PROSPECTING_MIN_FIT_FOR_CONTACTS", "60")
    )
    prospecting_expire_days: int = int(
        os.getenv("PROSPECTING_EXPIRE_DAYS", "90")
    )
    prospecting_resurface_days: int = int(
        os.getenv("PROSPECTING_RESURFACE_DAYS", "180")
    )


settings = Settings()
