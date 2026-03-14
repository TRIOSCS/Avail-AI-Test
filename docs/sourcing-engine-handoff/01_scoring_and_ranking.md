# Scoring and Ranking Brief

## Scoring philosophy
The score should answer: "How worthwhile is it for a buyer to contact this vendor about this part right now?"

## Separate score streams
1. Lead Confidence — stock likelihood
2. Vendor Safety — outreach / trust risk review

## Lead-confidence contributors
### Positive
- Trusted structured source
- Exact or normalized part match
- Recent signal
- Multiple-source corroboration
- Historical vendor success
- Strong contactability
- Prior buyer-confirmed useful outcomes

### Negative
- Stale signal
- Fuzzy-only match
- No contact path
- Low-trust source
- Repeated bad-lead outcomes
- Repeated no-stock outcomes

## Vendor-safety contributors
### Positive
- Known vendor in Salesforce / Avail
- Consistent identity across sources
- Stable domain/contact information
- Valid business footprint
- Positive internal history

### Negative
- Conflicting identity/contact details
- Missing business footprint
- Suspicious domain or website patterns
- Public complaint signals
- Prior internal bad experiences
- Repeated do-not-contact outcomes

## Confidence bands
- High: believable and actionable
- Medium: useful but not strongly proven
- Low: exploratory, weak, stale, or poorly contactable

## Safety bands
- Low Risk
- Medium Risk
- High Risk
- Unknown

## Suggested ranking order
1. confidence_band
2. confidence_score
3. freshness
4. contactability
5. corroboration
6. safety as cautionary modifier, not absolute blocker