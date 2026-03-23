# Technical SEO Reference

## Contents
- robots.txt setup
- X-Robots-Tag header for HTMX partials
- Canonical tag implementation
- Sitemap generation
- Common errors

---

## robots.txt

AvailAI has almost no public surface. Every authenticated route must be blocked.

```text
# app/static/robots.txt
User-agent: *
Disallow: /v2/
Disallow: /api/
Disallow: /auth/callback
Disallow: /auth/logout
Allow: /
Allow: /auth/login

Sitemap: https://yourdomain.com/sitemap.xml
```

Serve it via FastAPI static files (already mounted in `main.py`):

```python
# app/main.py — already present, confirms robots.txt is served
app.mount("/static", StaticFiles(directory="app/static"), name="static")
```

robots.txt must be at `app/static/robots.txt` — Caddy proxies `/static/` correctly.

**WARNING:** If `robots.txt` is missing, crawlers will index every HTMX partial URL (`/v2/requisitions/123/parts`). These return HTML fragments without `<html>` — they look broken to indexers and contaminate search results.

---

## X-Robots-Tag for HTMX Partials

HTMX partials are served at `/v2/*` routes. Add a middleware header to prevent indexing, since crawlers ignore `robots.txt` disallow for already-discovered URLs.

```python
# app/main.py — add to middleware stack
@app.middleware("http")
async def noindex_partials(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/v2/") or path.startswith("/api/"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response
```

See the **fastapi** skill for middleware patterns.

---

## Canonical Tag

Every public page needs a canonical to prevent duplicate content from query strings.

```html
<!-- app/templates/base.html — inside <head> -->
<link rel="canonical"
  href="{{ request.url.scheme }}://{{ request.url.netloc }}{{ request.url.path }}">
```

**DO:** Use `request.url.path` (no query string) for the canonical.
**DON'T:** Use `request.url` — it includes `?next=/dashboard` on the login redirect, creating duplicate canonicals.

---

## Sitemap

For a behind-auth app, the sitemap is trivial — typically one or two URLs.

```python
# app/routers/seo.py
"""
Sitemap and robots.txt for public SEO surfaces.

Called by: main.py router registration
Depends on: FastAPI, app/config.py for BASE_URL
"""
from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://yourdomain.com/</loc>
    <changefreq>monthly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>"""

@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap() -> Response:
    return Response(content=SITEMAP_XML, media_type="application/xml")
```

Register in `app/main.py`:
```python
from app.routers import seo
app.include_router(seo.router)
```

---

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| Partial HTML indexed | `/v2/` not blocked in `robots.txt` | Add `Disallow: /v2/` |
| Duplicate titles indexed | No `{% block title %}` in child templates | Add block override per template |
| Canonical includes `?next=` | Using `request.url` not `request.url.path` | Use `.path` only |
| Sitemap returns 404 | Router not registered in `main.py` | Add `app.include_router(seo.router)` |
