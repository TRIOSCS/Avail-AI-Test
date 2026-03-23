# Measurement and Testing Reference

## Contents
- What to Measure in AvailAI
- Activation Funnel Definition
- Playwright Journey Tests
- A/B Testing Constraints
- Analytics Gap Analysis

---

## What to Measure in AvailAI

AvailAI has **no analytics instrumentation** currently — no gtag, PostHog, Segment, or custom event tracking in `app/static/htmx_app.js` or any template.

The existing `ActivityService` (used for CRM activity logs) is the closest analogue. Conversion measurement must be built on top of it or via a new lightweight event table.

**Three metrics that matter:**

| Metric | Definition | Where to measure |
|--------|-----------|-----------------|
| Activation rate | % of new users who send their first RFQ | `ActivityService` or new `UserEvent` model |
| Time to activation | Days from first login to first RFQ sent | Timestamps on `User` + `RFQSend` activity |
| Feature adoption | % of activated users who use proactive matching | Activity events per feature area |

---

## Activation Funnel Definition

```
Step 1: First login         (User.created_at or login_count == 1)
Step 2: Dashboard viewed    (no tracking yet — add htmx load event)
Step 3: Requisition created (Requisition.created_at for user)
Step 4: Search run          (Requirement with sightings > 0)
Step 5: RFQ sent            (EmailService.send_batch_rfq() called)
```

Each step needs an activity event to measure drop-off between steps.

### Recommended Event Schema

```python
# app/models/user_events.py
class UserEvent(Base):
    __tablename__ = "user_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    event: Mapped[str]       # "dashboard_viewed", "requisition_created", "rfq_sent"
    created_at: Mapped[datetime] = mapped_column(default=func.now())
```

Create via Alembic migration. See the **sqlalchemy** skill for model patterns.

---

## Playwright Journey Tests

Use Playwright to test the full login-to-activation journey as a regression guard:

```typescript
// tests/e2e/activation-journey.spec.ts
import { test, expect } from '@playwright/test';

test.describe('Activation journey', () => {
  test('login page loads with value copy', async ({ page }) => {
    await page.goto('/auth/login');
    await expect(page.locator('h2')).toContainText('Sign in');
    // Value hook should be present
    await expect(page.locator('.text-brand-300').first()).toBeVisible();
  });

  test('dashboard shows primary CTA after auth', async ({ page, context }) => {
    // Set auth cookie for test user
    await context.addCookies([{ name: 'session', value: 'test-session', ... }]);
    await page.goto('/v2/');
    await expect(page.locator('button:has-text("New Requisition"), button:has-text("Create Requisition")')).toBeVisible();
  });

  test('empty requisitions list has CTA', async ({ page, context }) => {
    // ... auth setup
    await page.goto('/v2/requisitions');
    // If list is empty, CTA must be present
    const emptyState = page.locator('[data-empty-state]');
    if (await emptyState.isVisible()) {
      await expect(emptyState.locator('button')).toBeVisible();
    }
  });
});
```

See the **playwright** skill for authenticated test setup and project configuration.

---

## A/B Testing Constraints

AvailAI has no A/B testing infrastructure. For a closed internal tool with a small user base (~10-50 users), A/B testing is impractical.

**Recommended approach:** Before/after measurement with a defined rollout date.

```python
# app/routers/htmx_views.py — feature flag pattern for copy experiments
# Read from system_config table (already exists in startup.py seed)
def get_copy_variant(db: Session) -> str:
    config = db.query(SystemConfig).filter_by(key="login_copy_variant").first()
    return config.value if config else "control"
```

```html
{# login.html — variant-driven copy #}
{% if copy_variant == "value_hook" %}
<p class="text-sm text-brand-300 text-center mb-6">
  Source from 10 APIs. Send RFQs in one click.
</p>
{% endif %}
```

---

## Analytics Gap Analysis

### WARNING: No Conversion Tracking

**Detected:** Zero analytics instrumentation in `app/static/htmx_app.js`, `app/templates/htmx/base.html`, or any template.

**Impact:** No visibility into which CTAs drive action, where users drop off, or whether UI changes improve conversion. Operating blind.

**Minimum viable instrumentation** — wire to existing ActivityService:

```python
# app/services/activity_service.py — add conversion event logging
async def log_conversion_event(db: Session, user_id: int, event: str) -> None:
    activity = Activity(
        user_id=user_id,
        action=event,           # "dashboard_viewed", "requisition_created", "rfq_sent"
        entity_type="conversion",
        created_at=datetime.utcnow()
    )
    db.add(activity)
    db.commit()
```

Call this from:
- `dashboard_partial()` in `htmx_views.py` — on every dashboard load
- `create_requisition()` — on successful requisition creation
- `send_batch_rfq()` in `email_service.py` — on every RFQ send

See the **instrumenting-product-metrics** skill for full event schema and funnel query patterns.
