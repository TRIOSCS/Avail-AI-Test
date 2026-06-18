# P1d: Sent-Folder Lookup Window + Graph ID Recovery — Investigation Report

## Investigation Summary

### What scan_sent_folder (post-P1b) already covers

`scan_sent_folder` runs every 30 min via delta query (app/jobs/email_jobs.py:812). When it encounters a sent message whose subject carries an `[AVAIL-{id}]` token, it reconciles:

1. Finds the send-time `ActivityLog` row (external_id=NULL, matched by user+recipient+req+direction within a 48-hour window) and stamps `external_id` / notes the `graph_conversation_id`.
2. Also backfills `Contact.graph_message_id` and `Contact.graph_conversation_id` for any `status='sent'` Contact with NULL graph ids matching the same user+vendor_contact+requisition (lines 992–1004).

This means: **any Contact whose message appears in the sent-folder delta (i.e., sent after the last delta checkpoint) will have its graph ids backfilled within ~30 minutes**. This covers the tail of first-pass misses from `_find_sent_message`.

### Remaining gap: the first-pass window is too small

`_find_sent_message` fetches `$top=25` from Sent Items (app/email_service.py:415). In an RFQ batch fan-out of N vendors:

- All N messages share the same tagged subject and are sent in rapid succession.
- The Graph API returns them ordered by `sentDateTime desc`, so vendor messages near the end of the batch sit at slot ≥ N-1.
- Any vendor at slot ≥ 25 (the 26th or later) falls below the window and gets NULL graph ids at send time.

`scan_sent_folder` does recover them, but only after up to 30 minutes. During that window, any reply that arrives within ~30 min of the RFQ (uncommon but possible) would fall through to Tier-2/3/4 heuristics instead of Tier-1 exact-conversation matching.

Raising `$top` to 50 halves the miss rate with zero cost (same API call, the Graph just returns more items). It does not duplicate or compete with `scan_sent_folder`'s reconcile.

### No separate recovery job needed

`scan_sent_folder`'s Contact backfill (lines 992–1004) is the canonical recovery pass. It already:
- Targets `graph_message_id IS NULL` Contacts
- Filters by user + vendor_contact email + requisition_id + status='sent'
- Runs every 30 min
- Uses the existing delta query (no extra API calls)

A dedicated recovery job would be a redundant duplicate of this logic. **Deliberately not built.**

## What Was Built

### #1: Widen `_find_sent_message` window (app/email_service.py line ~415)

Changed `$top` from `"25"` to `"50"`.

- Halves the miss rate for batches with ≤ 50 vendors (covers all known real-world batch sizes).
- For the residual tail (>50 vendors), `scan_sent_folder` already provides recovery within 30 min.
- No schema change, no new job, no migration.

### #2: Redundant recovery job — deliberately NOT built

`scan_sent_folder`'s reconcile block (email_jobs.py lines 989–1004) already covers recovery for all recently-NULL Contacts. Adding another job that does the same lookup would:
- Duplicate Graph API calls
- Risk double-writing graph ids if both run concurrently
- Add maintenance surface with no additional coverage

## TDD Evidence

### RED
Test `TestFindSentMessage::test_window_is_at_least_50` written first. Simulates a 30-message batch where the target vendor is at index 29. Asserts `$top >= 50`.

Failure output before fix:
```
AssertionError: $top must be >=50, got 25
assert 25 >= 50
```

### GREEN
After changing `$top` from `"25"` to `"50"`:
- `test_window_is_at_least_50`: PASSED
- `TestFindSentMessage` (all 11 tests): PASSED
- `tests/test_email_service.py` (full suite, 162 tests): PASSED
- `tests/test_email_jobs.py` + `tests/test_p1b_send_clock.py` (68 tests): PASSED
- `tests/test_m365_strengthen.py` (15 tests): PASSED

No regressions.

## Concerns

1. **Batches > 50 vendors**: Still relies on `scan_sent_folder` (30-min delay). In practice, known batch sizes are well under 50. If future batches grow larger, raising `$top` further or adding a Graph `$filter` on `toRecipients` (not reliably supported on all M365 tenants) would be the next lever.

2. **Graph API propagation delay**: `_find_sent_message` retries with 1s/2s/4s backoff (7 seconds total). If Graph takes longer than 7 seconds to index a sent message (rare but possible), `$top=50` still returns `None` and `scan_sent_folder` picks it up. No change needed here — the retry delay is already conservative.

3. **Reply-matching during the 30-min gap**: If a vendor replies within 30 minutes of a batch send AND their Contact has NULL graph ids (i.e., the rare >50-vendor batch case), `poll_inbox` falls to Tier-2/3 subject+email matching, which is correct behavior as defined.

## Files Changed

- `app/email_service.py`: `$top` raised from `"25"` to `"50"` in `_find_sent_message`
- `tests/test_email_service.py`: `test_window_is_at_least_50` added to `TestFindSentMessage`
- `sdd/p1d-report.md`: this file
