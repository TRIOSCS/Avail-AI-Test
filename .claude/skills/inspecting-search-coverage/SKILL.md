---
name: inspecting-search-coverage
description: |
  Audits technical and on-page search coverage for AvailAI's FastAPI + Jinja2 stack.
  Use when: auditing meta tags in Jinja2 templates, checking robots.txt/sitemap coverage,
  reviewing structured data on public-facing pages, diagnosing missing canonical tags,
  inspecting Open Graph tags on the login or marketing pages, or verifying search-engine
  discoverability of any public routes before a release.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__playwright__browser_navigate, mcp__playwright__browser_snapshot, mcp__playwright__browser_take_screenshot, mcp__playwright__browser_evaluate, mcp__playwright__browser_console_messages, mcp__playwright__browser_network_requests, mcp__playwright__browser_wait_for, mcp__playwright__browser_close
---

# Inspecting Search Coverage

AvailAI is a **behind-auth B2B app**. The crawlable surface is small: the login page, any marketing/landing routes, and static assets. Every authenticated route should be blocked from indexing. This skill focuses on getting those boundaries right and maximising signal on the handful of public pages that exist.

## Quick Start

### Check what's publicly crawlable

```bash
grep -r "require_user\|require_buyer\|require_admin" app/routers/ | wc -l
# Every route NOT protected by a dependency is publicly reachable
grep -r "@router\." app/routers/ | grep -v "require_" | head -20
```

### Inspect meta tags in base template

```bash
grep -n "og:\|twitter:\|<title\|<meta name\|canonical" app/templates/base.html
```

### Verify robots rules

```bash
cat app/static/robots.txt 2>/dev/null || echo "MISSING — create one"
```

## Key Concepts

| Concept | Location | What to check |
|---------|----------|---------------|
| Page title | `app/templates/base.html` | `{% block title %}` per-page override |
| Meta description | `app/templates/base.html` | `{% block meta_description %}` |
| Canonical tag | `app/templates/base.html` | `<link rel="canonical">` present |
| OG tags | `app/templates/base.html` | `og:title`, `og:description`, `og:url` |
| robots.txt | `app/static/robots.txt` | Disallow: `/v2/` (all auth routes) |
| Sitemap | `app/routers/` or `app/static/` | Only public URLs |

## Common Patterns

### Protect auth routes from indexing

**When:** Auditing that authenticated HTMX partials are never indexed.

```html
<!-- app/templates/base.html — inside <head> -->
{% if request.url.path.startswith('/v2/') or request.url.path.startswith('/api/') %}
<meta name="robots" content="noindex, nofollow">
{% endif %}
```

### Per-page title + description blocks

```html
<!-- app/templates/base.html -->
<title>{% block title %}AvailAI — Electronic Component Sourcing{% endblock %}</title>
<meta name="description"
  content="{% block meta_description %}Source electronic components faster with AvailAI.{% endblock %}">
```

```html
<!-- app/templates/login.html -->
{% block title %}Sign In — AvailAI{% endblock %}
{% block meta_description %}Sign in to your AvailAI sourcing account.{% endblock %}
```

## Audit Checklist

Copy and track progress:
- [ ] `robots.txt` exists and disallows `/v2/`, `/api/`, `/auth/callback`
- [ ] `base.html` has `{% block title %}` and `{% block meta_description %}`
- [ ] Login page has unique, descriptive title + description
- [ ] Canonical tag present on all public pages
- [ ] OG tags present on login/landing page
- [ ] No `<meta name="robots" content="noindex">` accidentally on the login page
- [ ] Sitemap lists only public URLs (if one exists)
- [ ] HTMX partials (`/v2/*`) return `X-Robots-Tag: noindex` header

## See Also

- [technical](references/technical.md)
- [on-page](references/on-page.md)
- [content](references/content.md)
- [programmatic](references/programmatic.md)
- [schema](references/schema.md)
- [competitive](references/competitive.md)

## Related Skills

- See the **jinja2** skill for template inheritance and block overrides
- See the **fastapi** skill for adding response headers (X-Robots-Tag)
- See the **htmx** skill for understanding which routes serve partials vs. full pages
- See the **crafting-page-messaging** skill for title/description copy
- See the **tuning-landing-journeys** skill for public page conversion alongside SEO
- See the **clarifying-market-fit** skill for keyword intent alignment
