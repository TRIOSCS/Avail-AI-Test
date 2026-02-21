"""Configuration — all settings from environment variables."""

import os

APP_VERSION = "2.4.0"


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

    # TME API
    tme_api_token: str = os.getenv("TME_API_TOKEN", "")
    tme_api_secret: str = os.getenv("TME_API_SECRET", "")

    # AI parsing
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    # Enrichment APIs
    clay_api_key: str = os.getenv("CLAY_API_KEY", "")
    explorium_api_key: str = os.getenv("EXPLORIUM_API_KEY", "")
    apollo_api_key: str = os.getenv("APOLLO_API_KEY", "")

    # Deep Enrichment APIs
    hunter_api_key: str = os.getenv("HUNTER_API_KEY", "")
    rocketreach_api_key: str = os.getenv("ROCKETREACH_API_KEY", "")
    clearbit_api_key: str = os.getenv("CLEARBIT_API_KEY", "")

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

    # Scoring weights
    weight_recency: int = int(os.getenv("WEIGHT_RECENCY", "30"))
    weight_quantity: int = int(os.getenv("WEIGHT_QUANTITY", "20"))
    weight_vendor_reliability: int = int(os.getenv("WEIGHT_VENDOR_RELIABILITY", "20"))
    weight_data_completeness: int = int(os.getenv("WEIGHT_DATA_COMPLETENESS", "10"))
    weight_source_credibility: int = int(os.getenv("WEIGHT_SOURCE_CREDIBILITY", "10"))
    weight_price: int = int(os.getenv("WEIGHT_PRICE", "10"))

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

    # Own company domains — used to filter internal emails from vendor threads
    own_domains: frozenset = frozenset(
        d.strip().lower()
        for d in os.getenv("OWN_DOMAINS", "trioscs.com").split(",")
        if d.strip()
    )


settings = Settings()
