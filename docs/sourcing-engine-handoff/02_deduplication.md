# Deduplication Brief

## Goal
Avoid showing the same vendor multiple times under slightly different identities.

## Normalization fields
- vendor_name_normalized
- email_domain_normalized
- website_domain_normalized
- phone_normalized
- city/state/country normalized

## Merge levels
### Exact duplicate
Auto-merge:
- same canonical_vendor_id
- same normalized domain
- same normalized phone
- same normalized vendor name + same part

### Strong likely duplicate
Auto-merge only if at least two medium signals agree.

### Possible duplicate
Flag as duplicate_candidate, do not auto-merge.

## Guardrails
- dedupe at vendor + part level
- preserve all source attribution
- keep ambiguous records separate
- false negatives are safer than false positives