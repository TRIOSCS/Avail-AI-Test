# Scoring and Safety Model

## Confidence
Lead Confidence answers:
"How likely is it that this vendor may currently have stock?"

### Confidence inputs
- source reliability
- match quality
- freshness
- contactability
- corroboration
- historical success
- penalties for stale or weak signals

## Safety
Vendor Safety answers:
"How risky or trustworthy does this vendor appear for outreach?"

### Safety inputs
- internal historical experience
- identity consistency
- contact consistency
- business footprint
- public warning signals
- suspicious domain/contact patterns
- repeated bad internal outcomes

## Critical rule
Confidence and Safety must be stored and displayed separately.

Examples:
- High confidence + Unknown safety
- Medium confidence + Low risk
- High confidence + Medium risk
