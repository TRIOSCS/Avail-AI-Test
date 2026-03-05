"""Tests for Apollo sync schemas.

Validates request/response models for Apollo integration endpoints.
Called by: pytest
Depends on: app.schemas.apollo
"""

from app.schemas.apollo import (
    ApolloCreditsResponse,
    ApolloDiscoverRequest,
    ApolloEnrichResponse,
    ApolloSyncResponse,
    DiscoveredContact,
)


def test_discover_request_defaults():
    req = ApolloDiscoverRequest(domain="example.com")
    assert req.domain == "example.com"
    assert req.max_results == 10
    assert len(req.title_keywords) > 0  # has defaults


def test_discovered_contact_model():
    c = DiscoveredContact(
        apollo_id="abc123",
        full_name="Jane Doe",
        title="VP Procurement",
        seniority="decision_maker",
    )
    assert c.apollo_id == "abc123"
    assert c.email_masked is None  # optional


def test_sync_response():
    r = ApolloSyncResponse(synced=5, skipped=2, errors=0)
    assert r.synced == 5


def test_enrich_response_credits():
    r = ApolloEnrichResponse(enriched=3, verified=2, credits_used=3, credits_remaining=92)
    assert r.credits_remaining == 92


def test_credits_response():
    r = ApolloCreditsResponse(
        lead_credits_remaining=95,
        lead_credits_used=0,
        direct_dial_remaining=160,
        direct_dial_used=0,
    )
    assert r.lead_credits_remaining == 95
