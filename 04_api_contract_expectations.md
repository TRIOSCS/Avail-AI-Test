# State and View Model

## Recommended UI View Model

Each lead in UI should be normalized into a single view model like:

```ts
type LeadViewModel = {
  id: string;
  vendorName: string;
  vendorNameNormalized?: string;
  requestedPart: string;
  matchedPart?: string;
  matchType?: "exact" | "normalized" | "fuzzy" | "cross_ref";
  confidenceScore?: number;
  confidenceBand: "high" | "medium" | "low";
  safetyScore?: number;
  safetyBand: "low_risk" | "medium_risk" | "high_risk" | "unknown";
  reasonSummary: string;
  suggestedNextAction?: string;
  freshnessLabel?: string;
  sourceBadges: string[];
  cautionFlags: string[];
  buyerStatus: "New" | "Contacted" | "Replied" | "No Stock" | "Has Stock" | "Bad Lead" | "Do Not Contact";
  contact: {
    name?: string;
    email?: string;
    phone?: string;
    website?: string;
    location?: string;
  };
  corroborated?: boolean;
  evidenceCount?: number;
};
```

## UI State

### Page-level
- loading
- error
- selected lead id
- filters
- sort
- queue mode vs results mode

### Detail drawer
- isOpen
- active lead
- evidence loading state
- status update pending
- note draft

### Mutations
- update buyer status
- add note / feedback
- refresh lead row
- refresh detail panel

## State Management Guidance

Good options:
- React Query / TanStack Query for server state
- local component state for drawer, forms, filters
- small custom hooks for sourcing-specific actions

Avoid:
- massive all-in-one global state store if not needed
- duplicating server state across many nested components
