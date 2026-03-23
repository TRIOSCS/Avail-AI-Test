# Feedback & Insights Reference

## Contents
- Server Log Mining
- User-Reported Error Signals
- Friction Point Detection via Playwright
- Inbox Monitor as Feedback Source
- Anti-Patterns

---

## Server Log Mining

AvailAI's primary feedback signal is structured Loguru output. Use it to find where users encounter errors or dead-ends.

```bash
# Find most common 4xx errors in the last 24 hours
docker compose logs app --since 24h | \
  python3 -c "
import sys, json, collections
errors = collections.Counter()
for line in sys.stdin:
    try:
        r = json.loads(line)
        code = r.get('status_code', 0)
        if 400 <= code < 500:
            errors[f\"{code} {r.get('path', '?')}\"] += 1
    except: pass
for k, v in errors.most_common(10):
    print(v, k)
"
```

High 404 counts on `/v2/*` routes indicate broken HTMX links — a direct friction signal. High 403 counts indicate users hitting permission walls without explanation.

---

## User-Reported Error Signals

When users report "nothing happened" or "page went blank", the root cause is almost always one of:

1. **HTMX swap target mismatch** — route returns HTML for `#main-content` but the link targets `#modal-content`
2. **Empty partial with no empty state** — for-loop produces zero output, HTMX clears the target
3. **500 error with no error partial** — HTMX swaps nothing on non-2xx by default unless `htmx-ext-response-targets` is configured

Diagnose with:

```bash
# Check for 500 errors on HTMX routes (hx-get / hx-post requests)
docker compose logs app --since 1h | grep '"status_code": 5'
```

```javascript
// Add to Playwright workflows spec to catch blank swaps
test('no HTMX swap produces empty main-content', async ({ page }) => {
  page.on('response', response => {
    if (response.url().includes('/v2/') && response.status() >= 400) {
      throw new Error(`HTMX route ${response.url()} returned ${response.status()}`);
    }
  });
  await page.goto('/v2/requisitions');
  const content = await page.locator('#main-content').innerText();
  expect(content.trim().length).toBeGreaterThan(0);
});
```

---

## Friction Point Detection via Playwright

The `dead-ends` Playwright project is the primary tool for systematic friction detection. Run it after every HTMX partial change.

```javascript
// tests/e2e/dead-ends/navigation.spec.ts
const PRIMARY_ROUTES = [
  '/v2/requisitions',
  '/v2/vendors',
  '/v2/companies',
  '/v2/proactive',
  '/v2/excess',
];

for (const route of PRIMARY_ROUTES) {
  test(`${route} loads and is non-empty`, async ({ page }) => {
    await page.goto(route);
    await page.waitForSelector('#main-content');
    const html = await page.locator('#main-content').innerHTML();
    expect(html.trim()).not.toBe('');
  });
}
```

Run with: `npx playwright test --project=dead-ends`

---

## Inbox Monitor as Feedback Source

`app/jobs/inbox_monitor.py` processes RFQ replies. Low `offer_created` rates relative to `rfq_sent` rates indicate parsing failures — a feedback signal for the AI extraction pipeline.

```python
# app/jobs/inbox_monitor.py — log parse confidence for monitoring
logger.info(
    "rfq_reply_parsed",
    extra={
        "requisition_id": requisition.id,
        "confidence": result.confidence,
        "auto_created": result.confidence >= 0.8,
        "flagged_for_review": 0.5 <= result.confidence < 0.8,
        "rejected": result.confidence < 0.5,
    }
)
```

A spike in `rejected` events means the reply format changed (vendor switched email templates) or the Claude prompt needs tuning. Query for this daily:

```bash
docker compose logs app | grep 'rfq_reply_parsed' | \
  python3 -c "
import sys, json, collections
outcomes = collections.Counter()
for line in sys.stdin:
    try:
        r = json.loads(line)
        if r.get('message') == 'rfq_reply_parsed':
            if r['auto_created']: outcomes['auto_created'] += 1
            elif r['flagged_for_review']: outcomes['flagged'] += 1
            else: outcomes['rejected'] += 1
    except: pass
print(dict(outcomes))
"
```

---

## Anti-Patterns

### WARNING: Ignoring HTMX Response Codes

By default, HTMX only swaps on 2xx responses. A 422 validation error silently discards the response unless `htmx-ext-response-targets` is configured to redirect 4xx to an error target.

```html
<!-- DO: configure error target so validation failures are visible -->
<form hx-post="/api/requisitions"
      hx-target="#form-result"
      hx-ext="response-targets"
      hx-target-422="#form-errors">
  ...
  <div id="form-errors"></div>
  <div id="form-result"></div>
</form>
```

Without this, validation errors from FastAPI's 422 response are invisible to the user — a silent friction point that is impossible to detect without log mining.

See the **htmx** skill for full `response-targets` extension usage.
