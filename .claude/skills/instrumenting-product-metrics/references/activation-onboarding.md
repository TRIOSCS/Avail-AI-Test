# Activation & Onboarding Reference

## Contents
- Activation definition for AvailAI
- First-run detection via system_config
- Onboarding checklist pattern
- Auth hook for first-time inbox scan
- Anti-patterns

---

## Activation Definition

A user is **activated** when they complete the core sourcing loop:
1. Requisition created
2. At least one requirement searched (sighting created)
3. At least one RFQ sent
4. At least one offer/response received

Track each stage via `RequirementStatus` and `RequisitionStatus` enums from `app/constants.py`. Never use raw strings.

```python
from app.constants import RequirementStatus, RequisitionStatus

# Stage 1 complete: requisition exists
# Stage 2 complete: any requirement has status RequirementStatus.FOUND
# Stage 3 complete: any sighting has rfq_sent=True
# Stage 4 complete: any VendorResponse exists for this requisition
```

---

## First-Run Detection via system_config

`system_config` is the canonical place for persistent runtime flags. Use it to record one-time activation events.

```python
# app/services/onboarding_service.py
from app.models.config import SystemConfig
from sqlalchemy.orm import Session
from loguru import logger

def mark_onboarding_complete(db: Session, user_id: int) -> None:
    key = f"onboarding_complete_user_{user_id}"
    existing = db.get(SystemConfig, key)
    if existing:
        return
    db.add(SystemConfig(key=key, value="true"))
    db.commit()
    logger.info("Onboarding marked complete", extra={"user_id": user_id})

def is_onboarding_complete(db: Session, user_id: int) -> bool:
    key = f"onboarding_complete_user_{user_id}"
    row = db.get(SystemConfig, key)
    return row is not None and row.value == "true"
```

---

## Auth Hook: First-Time Inbox Scan

The auth router already contains a first-run trigger pattern. Follow this structure for any new first-run side effects.

```python
# app/routers/auth.py — existing pattern
# Trigger first-time backfill if user has never been scanned
from app.services.onboarding_service import is_onboarding_complete

if not is_onboarding_complete(db=db, user_id=user.id):
    background_tasks.add_task(run_first_inbox_scan, user_id=user.id, db=db)
    mark_onboarding_complete(db=db, user_id=user.id)
```

Use `BackgroundTasks` — never block the login response for first-run work.

---

## Activation Checklist Pattern

Surface this in an HTMX partial. Compute state server-side; Alpine.js handles the animation only.

```python
# app/services/activation_service.py
from dataclasses import dataclass

@dataclass
class ActivationChecklist:
    requisition_created: bool
    first_search_run: bool
    first_rfq_sent: bool
    first_offer_received: bool

    @property
    def activation_pct(self) -> int:
        steps = [
            self.requisition_created,
            self.first_search_run,
            self.first_rfq_sent,
            self.first_offer_received,
        ]
        return int(sum(steps) / len(steps) * 100)

    @property
    def is_activated(self) -> bool:
        return self.activation_pct == 100
```

```html
<!-- app/templates/htmx/partials/onboarding/checklist.html -->
<div x-data="{ open: true }">
  <button @click="open = !open" class="text-sm font-medium text-brand-600">
    Setup checklist ({{ checklist.activation_pct }}%)
  </button>
  <ul x-show="open" class="mt-2 space-y-1">
    {% for label, done in steps %}
    <li class="flex items-center gap-2 text-sm">
      <span class="{{ 'text-green-500' if done else 'text-slate-400' }}">
        {{ '✓' if done else '○' }}
      </span>
      {{ label }}
    </li>
    {% endfor %}
  </ul>
</div>
```

---

## Anti-Patterns

**NEVER block login on first-run work.** Any slow initialization (inbox scan, enrichment seed) must use `BackgroundTasks`.

**NEVER store activation state in a cookie or Alpine store.** It will desync. Use `system_config` or the relevant model column.

**NEVER compute activation state in the template.** Compute in the service, pass a typed object to the template context.

---

## Related Skills

- See the **designing-onboarding-paths** skill for empty-state UI patterns
- See the **sqlalchemy** skill for `system_config` queries
- See the **fastapi** skill for `BackgroundTasks` dependency patterns
