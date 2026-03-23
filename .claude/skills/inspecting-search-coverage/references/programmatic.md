# Programmatic SEO Reference

## Contents
- When programmatic SEO applies to AvailAI
- Dynamic meta tags for public product/category pages
- Sitemap generation from database data
- Anti-patterns for server-rendered dynamic SEO

---

## When Programmatic SEO Applies

AvailAI is behind auth — programmatic SEO only applies if the product ever exposes:
- Public part/component catalog pages
- Public vendor profile pages
- Public pricing or availability pages

None of these exist yet. This reference documents the **correct patterns to use when they are built**, preventing the anti-patterns that get baked in during rapid feature development.

---

## Dynamic Meta Tags from Route Parameters

If a public component catalog is added (e.g., `/parts/{mpn}`), meta tags must be generated from the MPN data.

```python
# app/routers/catalog.py
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.dependencies import get_db
from app.models.materials import MaterialCard

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

@router.get("/parts/{mpn}", response_class=HTMLResponse)
async def part_detail(mpn: str, request: Request, db: Session = Depends(get_db)):
    card = db.query(MaterialCard).filter(MaterialCard.mpn == mpn).first()
    if not card:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("catalog/part_detail.html", {
        "request": request,
        "card": card,
        "page_title": f"{card.mpn} — {card.manufacturer or 'Electronic Component'} | AvailAI",
        "page_description": f"Source {card.mpn} from verified distributors. {card.description or ''}".strip()[:155],
    })
```

```html
<!-- app/templates/catalog/part_detail.html -->
{% extends "base.html" %}
{% block title %}{{ page_title }}{% endblock %}
{% block meta_description %}{{ page_description }}{% endblock %}
```

**DO:** Truncate descriptions to 155 chars in the view function, not in the template.
**DON'T:** Use `{{ card.description }}` directly in `<meta>` — descriptions are often 500+ chars.

---

## Dynamic Sitemap from Database

For a public catalog, generate the sitemap from live data:

```python
# app/routers/seo.py — extended for dynamic catalog
from sqlalchemy import select
from app.models.materials import MaterialCard
from app.database import SessionLocal

@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap() -> Response:
    urls = ["https://yourdomain.com/"]
    with SessionLocal() as db:
        mpns = db.execute(
            select(MaterialCard.mpn).where(MaterialCard.is_public == True).limit(50000)
        ).scalars().all()
        for mpn in mpns:
            urls.append(f"https://yourdomain.com/parts/{mpn}")

    xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url in urls:
        xml_lines.append(f"  <url><loc>{url}</loc></url>")
    xml_lines.append("</urlset>")
    return Response(content="\n".join(xml_lines), media_type="application/xml")
```

**WARNING:** Never query ALL rows without a `LIMIT`. A catalog of 500k MPNs will OOM the app server generating a sitemap. Use sitemap index files for large catalogs.

---

## Anti-Patterns

### WARNING: Rendering HTMX Partials as Crawlable Pages

**The Problem:**
```python
# BAD — partial rendered without full HTML shell
@router.get("/parts/{mpn}")  # No noindex header, no require_user
async def part_partial(mpn: str):
    return templates.TemplateResponse("htmx/partials/parts/detail.html", {...})
```

**Why This Breaks:**
1. HTMX partials have no `<html>`, `<head>`, or meta tags
2. Google indexes a fragment, not a page
3. Users landing from search see broken, unstyled HTML

**The Fix:** Public catalog pages get their own full-page templates, not partials.

See the **jinja2** skill for template inheritance patterns and the **htmx** skill for distinguishing partial vs. full-page routes.
