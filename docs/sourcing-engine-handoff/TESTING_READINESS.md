# Sourcing Engine — Testing Readiness

## Automated Test Coverage

All sourcing engine tests pass. Run:
```bash
pytest tests/test_sourcing_leads.py tests/test_htmx_sourcing.py tests/test_sourcing_lead_engine.py tests/test_e2e_sourcing_flow.py tests/test_services_sourcing_score.py -v --override-ini="addopts="
```

### Test Files and What They Cover
| Test File | Tests | What It Verifies |
|---|---|---|
| `test_sourcing_leads.py` | 22 | Lead upsert, evidence fields (signal_type, match_type, reliability_band), source categories, corroboration (distinct categories), buyer status, safety flags, dedup, feedback loop, duplicate candidate flagging, verification_state lifecycle |
| `test_htmx_sourcing.py` | 22 | Results partial, filters, sorts, lead detail view, follow-up queue |
| `test_e2e_sourcing_flow.py` | 10 | End-to-end search+lead flow |
| `test_sourcing_lead_engine.py` | 3 | Legacy in-memory engine (retained for safety) |
| `test_services_sourcing_score.py` | 11 | Requisition scoring |

**Total: 131 sourcing tests, all passing.**

---

## Smoke Test Walkthrough

### 1. Open a part requiring sourcing
- Navigate to a requisition, expand a requirement row
- Results panel loads via HTMX at `GET /views/sourcing/{req_row_id}/results`

### 2. Verify leads appear with correct attributes
- **Vendor name**: Shown in result row, linked to lead via `lead_id`
- **Confidence band**: Color-coded ring (green ≥75, amber ≥50, red <50)
- **Safety band**: Badge (LOW RISK green, MEDIUM RISK amber, HIGH RISK red, UNKNOWN gray)
- **Reason summary**: Visible in lead detail view
- **Source badges**: Each result row shows source type badge

### 3. Open a lead detail panel
- Click "View" button on any result row with a lead
- HTMX loads `GET /views/sourcing/leads/{lead_id}` into `#lead-detail-container`
- **Tabs**: Evidence, Safety, Contact, Activity

### 4. Verify lead detail contents
- **Evidence tab**: Shows table with source, type, part observed, freshness, reliability
- **Safety tab**: Shows band badge, score, summary, safety flags
- **Contact tab**: Shows name, email, phone, URL, location
- **Activity tab**: Shows timeline of status changes and notes

### 5. Mark a lead Contacted
- Use the status dropdown on result row or lead detail panel
- API: `PATCH /api/leads/{lead_id}/status` with `{"status": "contacted"}`
- UI refreshes automatically

### 6. Add a note
- In lead detail Activity tab, use the "Add note" form
- API: `POST /api/leads/{lead_id}/feedback` with `{"note": "...", "contact_method": "phone"}`
- Note appears in activity timeline

### 7. Change status to Has Stock or No Stock
- Same status dropdown
- "has_stock" boosts confidence +12, propagates +1 win to VendorCard
- "no_stock" reduces confidence -14
- "bad_lead" reduces confidence -18, safety -8, reduces vendor score -3
- "do_not_contact" reduces safety -30, blacklists vendor card

### 8. Follow-up queue
- Navigate to `GET /views/sourcing/follow-up-queue`
- Shows all leads across requisitions, filterable by status tabs
- Each lead has View and status change actions

### 9. Verify activity history
- Lead detail Activity tab shows chronological feedback events
- Each event: status badge, contact method, timestamp, note

### 10. Duplicate verification
- "Arrow Electronics Inc." and "Arrow Electronics" merge into one lead
- Vendor name normalization strips legal suffixes (Inc, LLC, Ltd, Corp, etc.)
- MPN normalization strips dashes, dots, spaces, slashes

### 11. New vendor safety check
- Unknown vendors get "UNKNOWN" safety band with gray badge
- Summary: "Unknown vendor: no internal history available..."
- Caution language throughout — signals, not accusations

---

## Filter and Sort Options

### Filters
| Filter | What It Shows |
|---|---|
| All | All results |
| Live Stock | brokerbin, nexar, digikey, mouser, etc. |
| Historical | material_history, sighting_history |
| Vendor Affinity | vendor_affinity matches |
| High Confidence | Results with confidence_band = "high" |
| Safe Vendors | Results with safety_band = "low_risk" or no safety data |
| Has Lead | Results linked to a persisted lead |

### Sorts
| Sort | Description |
|---|---|
| Best Overall | Confidence descending (default) |
| Safest | Vendor safety score descending |
| Freshest | Source recency descending |
| Price (low) | Unit price ascending |
| Price (high) | Unit price descending |
| Qty (high) | Quantity available descending |

---

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/requisitions/{id}/leads` | List leads for a requisition |
| GET | `/api/leads/queue` | Cross-requisition follow-up queue |
| GET | `/api/leads/{id}` | Lead detail with evidence + feedback |
| PATCH | `/api/leads/{id}/status` | Update buyer workflow status |
| POST | `/api/leads/{id}/feedback` | Append buyer feedback note |

---

## Key Files Changed

| File | Change |
|---|---|
| `app/services/sourcing_leads.py` | Fixed scoring bands, enhanced safety, dedup via vendor_utils, feedback loop, handoff-spec evidence fields (signal_type, match_type, reliability_band, source_category), cross-category corroboration, duplicate candidate flagging, verification_state lifecycle |
| `app/routers/requisitions/requirements.py` | Fixed NameError, added lead detail/queue/feedback endpoints |
| `app/routers/views.py` | Added lead detail view, follow-up queue view routes |
| `app/schemas/sourcing_leads.py` | Expanded to 30+ field LeadOut, added EvidenceOut, FeedbackEventOut |
| `app/templates/partials/sourcing/lead_detail.html` | New: full lead detail HTMX partial |
| `app/templates/partials/sourcing/follow_up_queue.html` | New: buyer follow-up queue HTMX partial |
| `app/templates/partials/sourcing/result_row.html` | Added View button, "unknown" safety band |
| `app/templates/partials/sourcing/results.html` | Added lead-detail-container, new filter pills, sort options |
| `app/static/app.js` | Added "unknown" safety band to JS config |
| `tests/test_sourcing_leads.py` | 22 tests (evidence spec compliance, source categories, corroboration, safety, dedup, duplicate candidates, verification_state, feedback loop, resync) |
| `tests/test_htmx_sourcing.py` | 22 tests (lead detail, queue, filters, sorts) |

---

## Known Limitations / Tech Debt

1. **Legacy `sourcing_lead_engine.py`** still exists with 77 tests. It's unused by production code but retained for safety. Can be removed when confident the persisted lead system covers all cases.
2. **Contact enrichment** is basic — `contact_email`/`contact_phone` come from VendorCard but aren't always populated. Enrichment pipeline improvements are a separate effort.
3. **Deduplication** handles vendor name suffix normalization and duplicate_candidate flagging (via shared vendor card or domain match). Phone-based dedup (per handoff spec) is not yet implemented.
4. **Corroboration** now requires evidence from 2+ distinct source **categories** (api, marketplace, salesforce_history, etc.), not just 2+ connector names. This matches the handoff spec's cross-source intent.
5. **Verification state** transitions from `raw` → `buyer_confirmed` (has_stock) or `raw` → `rejected` (bad_lead/do_not_contact). The `inferred` state is not yet used — would require automated quality checks.

---

## Deploy Command

```bash
cd /root/availai && git pull origin main && docker compose up -d --build && echo "Done — hard refresh browser"
```
