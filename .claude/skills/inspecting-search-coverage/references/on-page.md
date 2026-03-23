# On-Page SEO Reference

## Contents
- Title and description blocks
- Open Graph tags
- Heading hierarchy
- Login page audit
- DO/DON'T pairs

---

## Title and Description Blocks

The base template must define overridable blocks. Child templates that represent public pages must override them.

```html
<!-- app/templates/base.html -->
<head>
  <title>{% block title %}AvailAI — Electronic Component Sourcing Platform{% endblock %}</title>
  <meta name="description"
    content="{% block meta_description %}AvailAI helps procurement teams source electronic components from 10 supplier networks in seconds.{% endblock %}">
  <meta name="robots"
    content="{% block robots %}index, follow{% endblock %}">
</head>
```

For every authenticated page, override the robots block:

```html
<!-- app/templates/htmx/base_page.html -->
{% block robots %}noindex, nofollow{% endblock %}
```

This ensures the lazy-loader shell and all authenticated views are never indexed, without requiring per-route middleware logic.

---

## Open Graph Tags

Required on the login page. Irrelevant on authenticated pages.

```html
<!-- app/templates/login.html — inside {% block extra_head %} -->
<meta property="og:type" content="website">
<meta property="og:title" content="AvailAI — Electronic Component Sourcing">
<meta property="og:description"
  content="Source components from 10 supplier networks. RFQ automation built in.">
<meta property="og:url" content="{{ request.url.scheme }}://{{ request.url.netloc }}/">
<meta property="og:image" content="{{ url_for('static', path='img/og-image.png') }}">
<meta name="twitter:card" content="summary_large_image">
```

**DO:** Use `url_for('static', path=...)` so the OG image URL is absolute and fingerprinted by Vite. See the **vite** skill for asset fingerprinting.

**DON'T:** Hardcode `https://yourdomain.com` in templates — it breaks in staging environments.

---

## Heading Hierarchy

The login page and any marketing pages must have exactly one `<h1>` that matches the page's primary keyword intent.

```html
<!-- app/templates/login.html -->
<h1 class="text-2xl font-semibold text-gray-900 dark:text-white">
  Electronic Component Sourcing, Automated
</h1>
```

**WARNING:** HTMX partials frequently contain `<h2>` and `<h3>` headings without a parent `<h1>`. This is fine for partials — they're injected into an authenticated page shell that crawlers never see. Don't add an `<h1>` to partials just to "fix" heading hierarchy; it will create duplicate `<h1>` tags when rendered inside the base shell.

---

## Login Page Audit

The login page (`/auth/login` → `app/templates/login.html`) is AvailAI's only meaningful crawlable page. Check:

```bash
# Confirm the login template has title, description, and OG blocks
grep -n "block title\|block meta_description\|og:title\|og:description\|<h1" \
  app/templates/login.html
```

Expected output — all five patterns should match.

---

## DO/DON'T Pairs

**DO:** Set a unique `<title>` on every public template.
**DON'T:** Rely on the base template default for the login page — it reads as generic.

**DO:** Use `{% block robots %}noindex, nofollow{% endblock %}` in `htmx/base_page.html`.
**DON'T:** Add `<meta name="robots" content="noindex">` to the login page by accident when bulk-editing `base.html`.

**DO:** Keep meta descriptions under 155 characters.
**DON'T:** Repeat the page title verbatim as the meta description.

See the **crafting-page-messaging** skill for copy guidance on login page title and description text.
