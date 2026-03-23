# Competitive SEO Reference

## Contents
- Competitive landscape for AvailAI
- Target keyword gaps
- Comparison and alternative page patterns
- Internal linking strategy for competitive terms

---

## Competitive Landscape

AvailAI competes with:
- **Octopart / Nexar** — component search aggregators
- **Sourcengine** — distributor marketplace
- **IHS Markit / S&P Global** — enterprise BOM tools
- **Cofactr** — modern procurement automation

AvailAI's differentiator is **RFQ automation + broker network search combined**. Most competitors do one or the other.

---

## Target Keyword Gaps

High-intent terms where AvailAI could rank if public content pages are created:

| Term | Intent | Page type |
|------|--------|-----------|
| `octopart alternative` | Commercial investigation | `/alternatives/octopart` |
| `brokerbin alternative` | Commercial investigation | `/alternatives/brokerbin` |
| `rfq automation software` | Commercial investigation | Landing page |
| `electronic component sourcing software` | Commercial investigation | Landing page |
| `bom sourcing tool` | Commercial investigation | Landing page |

**WARNING:** These pages only work if they are public routes (no `require_user`). Building them inside the authenticated app shell is a common mistake — they get noindexed along with everything else.

---

## Alternative Page Pattern

For each alternative page (e.g., `/alternatives/octopart`):

```python
# app/routers/marketing.py
"""
Public marketing and comparison pages.

Called by: main.py router registration
Depends on: Jinja2 templates, no auth required
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

ALTERNATIVES = {
    "octopart": {
        "title": "Octopart Alternative — AvailAI Component Sourcing",
        "description": "AvailAI searches broker networks Octopart doesn't index. Compare features.",
        "competitor": "Octopart",
        "differentiators": [
            "Searches BrokerBin and 9 other sources simultaneously",
            "Automated RFQ via Microsoft Outlook integration",
            "Vendor reliability scoring across interactions",
        ],
    },
}

@router.get("/alternatives/{competitor}", response_class=HTMLResponse)
async def alternative_page(competitor: str, request: Request):
    data = ALTERNATIVES.get(competitor)
    if not data:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("marketing/alternative.html", {
        "request": request, **data
    })
```

```html
<!-- app/templates/marketing/alternative.html -->
{% extends "base.html" %}
{% block title %}{{ title }}{% endblock %}
{% block meta_description %}{{ description }}{% endblock %}
{% block robots %}index, follow{% endblock %}

{% block content %}
<h1 class="text-3xl font-bold">{{ competitor }} Alternative</h1>
<ul>
  {% for point in differentiators %}
  <li>{{ point }}</li>
  {% endfor %}
</ul>
<a href="/auth/login" class="btn-primary">Try AvailAI Free</a>
{% endblock %}
```

---

## Internal Linking for Competitive Terms

Link alternative pages from the login page footer and from each other:

```html
<!-- app/templates/login.html -->
<nav class="mt-4 text-xs text-gray-400 flex gap-3 justify-center">
  <a href="/alternatives/octopart">vs Octopart</a>
  <a href="/alternatives/brokerbin">vs BrokerBin</a>
  <a href="/alternatives/sourcengine">vs Sourcengine</a>
</nav>
```

**DO:** Keep anchor text descriptive (`vs Octopart`) — exact-match competitor names signal relevance.
**DON'T:** Use `nofollow` on internal links — it wastes crawl budget and dilutes PageRank.

---

## Monitoring Competitors in Search

Use the built-in search infrastructure to monitor when AvailAI starts appearing for competitive terms. If `ACTIVITY_TRACKING_ENABLED=true`, log UTM source on login to track organic vs. direct traffic origins. See the **instrumenting-product-metrics** skill for activity tracking patterns.
