"""Shared constants used across multiple modules.

Consolidated here to avoid duplication of noise/junk domain lists,
email prefixes, and other constants that were previously defined
independently in email_service.py and utils/vendor_helpers.py.

Called by: email_service.py, utils/vendor_helpers.py
Depends on: nothing
"""

# ── Noise / Junk domains ────────────────────────────────────────────────
# Domains that should not be treated as vendor contacts.
# Merged from email_service.NOISE_DOMAINS + vendor_helpers._JUNK_DOMAINS.

JUNK_DOMAINS: set[str] = {
    # Email / cloud providers
    "microsoft.com",
    "microsoftonline.com",
    "office365.com",
    "office.com",
    "google.com",
    "googleapis.com",
    "googlemail.com",
    # Social media
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
    # Dev / infra
    "github.com",
    "slack.com",
    "zoom.us",
    "teams.microsoft.com",
    "sentry.io",
    "cloudflare.com",
    "gstatic.com",
    # Marketing / email services
    "mailchimp.com",
    "constantcontact.com",
    "sendgrid.net",
    "amazonses.com",
    "hubspot.com",
    "salesforce.com",
    "marketo.com",
    # Shipping
    "fedex.com",
    "ups.com",
    "usps.com",
    "dhl.com",
    # Finance
    "intuit.com",
    "quickbooks.com",
    "paypal.com",
    "stripe.com",
    # Document / storage
    "docusign.com",
    "dropbox.com",
    "box.com",
    # Web standards / CDN (from vendor_helpers)
    "example.com",
    "schema.org",
    "w3.org",
    "jquery.com",
    "bootstrapcdn.com",
    "gravatar.com",
    "wordpress.org",
}

# ── Noise email prefixes ────────────────────────────────────────────────
# Local parts that indicate automated/non-vendor senders.
# Merged from email_service.NOISE_PREFIXES + vendor_helpers._JUNK_EMAILS.

JUNK_EMAIL_PREFIXES: set[str] = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "webmaster",
    "notifications",
    "alerts",
    "newsletter",
    "marketing",
    "support",
    "billing",
    "privacy",
    "abuse",
    "spam",
    "unsubscribe",
    "root",
    "hostmaster",
    "example",
    "test",
    "admin@example",
}

# ── Junk vendor names ─────────────────────────────────────────────────
# Vendor names that should be stripped from search results.
# Moved from search_service.py to avoid duplication.

JUNK_VENDORS: set[str] = {
    "",
    "unknown",
    "(no sellers listed)",
    "no sellers listed",
    "n/a",
    "none",
    "(none)",
    "-",
    "no vendor",
    "no seller",
}
