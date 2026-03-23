# Content SEO Reference

## Contents
- Keyword intent for AvailAI's public pages
- Copy patterns for title and description
- Content gaps on the login page
- Internal linking on public pages

---

## Keyword Intent

AvailAI's only indexable page is the login/landing screen. The primary intent is **navigational** (existing users returning) plus **commercial investigation** (procurement teams evaluating sourcing tools).

Target terms for on-page signals:
- `electronic component sourcing`
- `electronic component RFQ automation`
- `BOM sourcing platform`
- `broker distributor search tool`

These should appear naturally in: `<h1>`, `<meta name="description">`, and the first visible paragraph.

---

## Copy Patterns

### Title formula

```
[Primary Action] — [Product Name] | [Brand]
Electronic Component Sourcing — AvailAI
```

### Description formula (≤155 chars)

```
[Who it's for] + [core action] + [differentiator].
Procurement teams source components from 10 supplier networks — BrokerBin, DigiKey, Mouser and more — with automated RFQ workflows.
```

Character count matters. Verify in template:

```bash
python3 -c "
s = 'Procurement teams source components from 10 supplier networks — BrokerBin, DigiKey, Mouser and more — with automated RFQ workflows.'
print(len(s), 'chars')
"
```

---

## Content Gaps on the Login Page

The login page (`app/templates/login.html`) typically contains only an OAuth button. For crawlable signal, add a feature summary below the login form:

```html
<!-- app/templates/login.html — below the OAuth button -->
<section class="mt-8 text-sm text-gray-500 dark:text-gray-400 space-y-1">
  <p>Search 10 supplier APIs in parallel.</p>
  <p>Send RFQs via Microsoft Outlook. Parse replies automatically.</p>
  <p>Track vendor reliability with every interaction.</p>
</section>
```

**DO:** Keep this text minimal and factual — it signals to crawlers what the app does.
**DON'T:** Add keyword-stuffed paragraphs. The page is primarily navigational; over-optimising dilutes trust.

See the **crafting-page-messaging** skill for exact copy and CTA guidance.

---

## Internal Linking on Public Pages

For a behind-auth app, internal links on public pages are rare. If a marketing/pricing page exists, link it from the login page footer:

```html
<!-- app/templates/login.html -->
<footer class="mt-6 text-center text-xs text-gray-400">
  <a href="/pricing" class="hover:underline">Pricing</a> ·
  <a href="/about" class="hover:underline">About</a>
</footer>
```

Ensure any linked public routes are NOT protected by `require_user`.

```bash
# Verify /pricing is not behind auth
grep -n "pricing" app/routers/ -r | grep "require_"
# Should return nothing
```

---

## WARNING: No Blog or Content Hub

AvailAI has no blog or content pages in the current codebase. For a B2B sourcing tool, content marketing (component shortage guides, RFQ best practices) drives high-intent organic traffic. This is a known gap.

If content pages are added, they must:
1. Live at public routes (not behind `require_user`)
2. Have unique `{% block title %}` and `{% block meta_description %}`
3. Be added to `sitemap.xml`
4. Not share templates with authenticated HTMX views
