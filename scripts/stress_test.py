#!/usr/bin/env python3
"""
AvailAI API Stress Test
=======================
Async stress test that hammers every safe API endpoint concurrently
to find performance bottlenecks, race conditions, and error-prone paths.

Usage:
    python3 scripts/stress_test.py [--base-url URL] [--concurrency N] [--reps N]

Requires: httpx, itsdangerous (both already in project deps).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx
import itsdangerous

# ── Configuration ────────────────────────────────────────────────────────

BASE_URL = os.getenv("STRESS_BASE_URL", "http://localhost:8000")
SECRET_KEY = os.getenv(
    "SESSION_SECRET",
    os.getenv(
        "SECRET_KEY",
        "ea277450d8b187b493c424a734864512bef722de5229ae998a558c41a753e5e1",
    ),
)

READ_CONCURRENCY = 50
WRITE_CONCURRENCY = 20
MIXED_CONCURRENCY = 30
MIXED_OPS = 300
READ_REPS = 3

# Users: admin, manager, buyer
USER_IDS = [1, 2, 3]

# ── Colors ───────────────────────────────────────────────────────────────

BOLD = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
DIM = "\033[2m"
RESET = "\033[0m"

# ── Session signing (mirrors tests/e2e/conftest.py) ─────────────────────


def sign_session(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"user_id": user_id}).encode()).decode()
    signer = itsdangerous.TimestampSigner(SECRET_KEY)
    return signer.sign(payload).decode()


# ── Data classes ─────────────────────────────────────────────────────────


@dataclass
class RequestResult:
    method: str
    path: str
    status_code: int
    duration_ms: float
    phase: str
    user_id: int
    error: str = ""


@dataclass
class PhaseStats:
    name: str
    results: list[RequestResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def success(self) -> int:
        return sum(1 for r in self.results if 200 <= r.status_code < 400)

    @property
    def errors(self) -> int:
        return sum(
            1 for r in self.results if r.status_code >= 400 and r.status_code != 429
        )

    @property
    def rate_limited(self) -> int:
        return sum(1 for r in self.results if r.status_code == 429)

    def _durations(self) -> list[float]:
        return sorted(r.duration_ms for r in self.results) if self.results else [0]

    @property
    def p50(self) -> float:
        d = self._durations()
        return d[len(d) // 2]

    @property
    def p95(self) -> float:
        d = self._durations()
        return d[int(len(d) * 0.95)]

    @property
    def p99(self) -> float:
        d = self._durations()
        return d[int(len(d) * 0.99)]


# ── HTTP helpers ─────────────────────────────────────────────────────────


class StressClient:
    """Wraps httpx.AsyncClient with session cookies, CSRF, and result tracking."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.sessions: dict[int, str] = {}  # user_id -> session cookie
        self.csrf_token: str = ""
        self.results: list[RequestResult] = []
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        # Sign sessions for all users
        for uid in USER_IDS:
            self.sessions[uid] = sign_session(uid)
        # Acquire CSRF token via a GET to a non-exempt endpoint
        await self._acquire_csrf()
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def _acquire_csrf(self):
        """Make a GET request to acquire the csrftoken cookie."""
        resp = await self._client.get(
            "/api/requisitions",
            cookies={"session": self.sessions[1]},
        )
        self.csrf_token = resp.cookies.get("csrftoken", "")
        if not self.csrf_token:
            # Try health as fallback (some middleware versions set it everywhere)
            resp = await self._client.get("/health")
            self.csrf_token = resp.cookies.get("csrftoken", "")

    def _cookies(self, user_id: int) -> dict[str, str]:
        cookies = {"session": self.sessions[user_id]}
        if self.csrf_token:
            cookies["csrftoken"] = self.csrf_token
        return cookies

    def _headers(self) -> dict[str, str]:
        h = {}
        if self.csrf_token:
            h["x-csrftoken"] = self.csrf_token
        return h

    async def request(
        self,
        method: str,
        path: str,
        *,
        phase: str,
        user_id: int = 1,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> RequestResult:
        start = time.perf_counter()
        status = 0
        error = ""
        try:
            resp = await self._client.request(
                method,
                path,
                json=json_body,
                params=params,
                cookies=self._cookies(user_id),
                headers=self._headers(),
            )
            status = resp.status_code
            if status >= 400 and status != 429:
                try:
                    body = resp.json()
                    error = body.get("error", body.get("detail", resp.text[:200]))
                except Exception:
                    error = resp.text[:200]
        except httpx.TimeoutException:
            status = 408
            error = "timeout"
        except httpx.ConnectError:
            status = 503
            error = "connection refused"
        except Exception as e:
            status = 500
            error = str(e)[:200]

        duration_ms = (time.perf_counter() - start) * 1000
        result = RequestResult(
            method=method,
            path=path,
            status_code=status,
            duration_ms=duration_ms,
            phase=phase,
            user_id=user_id,
            error=error,
        )
        self.results.append(result)
        return result

    async def get(self, path: str, *, phase: str, user_id: int = 1, **kw):
        return await self.request("GET", path, phase=phase, user_id=user_id, **kw)

    async def post(self, path: str, *, phase: str, user_id: int = 1, **kw):
        return await self.request("POST", path, phase=phase, user_id=user_id, **kw)

    async def put(self, path: str, *, phase: str, user_id: int = 1, **kw):
        return await self.request("PUT", path, phase=phase, user_id=user_id, **kw)

    async def delete(self, path: str, *, phase: str, user_id: int = 1, **kw):
        return await self.request("DELETE", path, phase=phase, user_id=user_id, **kw)


# ── Test data store ──────────────────────────────────────────────────────


@dataclass
class TestData:
    company_id: int = 0
    site_id: int = 0
    site_contact_id: int = 0
    requisition_id: int = 0
    requirement_id: int = 0
    offer_id: int = 0
    vendor_id: int = 0
    vendor_contact_id: int = 0
    error_report_id: int = 0
    # Bulk-created IDs for write stress
    extra_company_ids: list[int] = field(default_factory=list)
    extra_requisition_ids: list[int] = field(default_factory=list)
    extra_vendor_contact_ids: list[int] = field(default_factory=list)
    extra_error_report_ids: list[int] = field(default_factory=list)


# ── Phase implementations ────────────────────────────────────────────────

PHASE = "phase"  # replaced per phase


def _id(resp: RequestResult, raw_resp: dict | None) -> int:
    """Extract ID from a response body, returns 0 on failure."""
    if raw_resp and isinstance(raw_resp, dict):
        return raw_resp.get("id", 0)
    return 0


async def _post_json(client: StressClient, path, body, phase, user_id=1):
    """POST and return (result, response_body).

    Response body may be a dict, list, or empty dict on failure.
    """
    start = time.perf_counter()
    status = 0
    error = ""
    resp_body: dict | list = {}
    try:
        resp = await client._client.request(
            "POST",
            path,
            json=body,
            cookies=client._cookies(user_id),
            headers=client._headers(),
        )
        status = resp.status_code
        try:
            resp_body = resp.json()
        except Exception:
            pass
        if status >= 400 and status != 429:
            if isinstance(resp_body, dict):
                error = resp_body.get("error", resp_body.get("detail", ""))[:200]
            else:
                error = str(resp_body)[:200]
    except Exception as e:
        status = 500
        error = str(e)[:200]

    duration_ms = (time.perf_counter() - start) * 1000
    result = RequestResult(
        method="POST", path=path, status_code=status,
        duration_ms=duration_ms, phase=phase, user_id=user_id, error=error,
    )
    client.results.append(result)
    return result, resp_body


def _extract_id(body) -> int:
    """Extract 'id' from a response body (dict or list-of-dicts)."""
    if isinstance(body, dict):
        return body.get("id", 0)
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body[0].get("id", 0)
    return 0


async def phase_setup(client: StressClient, data: TestData):
    """Phase 1: Create test data sequentially."""
    phase = "Setup"
    print(f"\n{CYAN}▶ Phase 1: Setup{RESET} — creating STRESS_ test data...")

    # Company
    r, body = await _post_json(client, "/api/companies", {"name": "STRESS_TestCo"}, phase)
    data.company_id = _extract_id(body)
    if not data.company_id:
        print(f"  {RED}✗ Company creation failed: {r.error}{RESET}")
        return
    print(f"  Company id={data.company_id}")

    # Site
    r, body = await _post_json(
        client, f"/api/companies/{data.company_id}/sites",
        {"site_name": "STRESS_Site"}, phase,
    )
    data.site_id = _extract_id(body)
    print(f"  Site id={data.site_id}")

    # Site contact
    if data.site_id:
        r, body = await _post_json(
            client, f"/api/sites/{data.site_id}/contacts",
            {"full_name": "STRESS_Contact", "email": "stress@test.example.com"}, phase,
        )
        data.site_contact_id = _extract_id(body)
        print(f"  Site contact id={data.site_contact_id}")

    # Requisition
    r, body = await _post_json(
        client, "/api/requisitions", {"name": "STRESS_TestReq"}, phase,
    )
    data.requisition_id = _extract_id(body)
    print(f"  Requisition id={data.requisition_id}")

    # Requirement
    if data.requisition_id:
        r, body = await _post_json(
            client, f"/api/requisitions/{data.requisition_id}/requirements",
            {"primary_mpn": "STRESSLM7805CT", "target_qty": 1000}, phase,
        )
        data.requirement_id = _extract_id(body)
        print(f"  Requirement id={data.requirement_id}")

    # Offer
    if data.requisition_id:
        r, body = await _post_json(
            client, f"/api/requisitions/{data.requisition_id}/offers",
            {
                "mpn": "STRESSLM7805CT",
                "vendor_name": "STRESS_TestVendor",
                "qty_available": 5000,
                "unit_price": 2.25,
            },
            phase,
        )
        data.offer_id = _extract_id(body)
        print(f"  Offer id={data.offer_id}")

    # Vendor (auto-created by offer, look up)
    try:
        resp = await client._client.request(
            "GET", "/api/vendors", params={"search": "STRESS_"},
            cookies=client._cookies(1), headers=client._headers(),
        )
        vendors_resp = resp.json()
        # Response is {"vendors": [...], "total": int, ...}
        vendor_list = []
        if isinstance(vendors_resp, dict):
            vendor_list = vendors_resp.get("vendors", vendors_resp.get("items", []))
        elif isinstance(vendors_resp, list):
            vendor_list = vendors_resp
        if vendor_list:
            data.vendor_id = vendor_list[0].get("id", 0)
    except Exception:
        pass
    print(f"  Vendor id={data.vendor_id}")

    # Vendor contact
    if data.vendor_id:
        r, body = await _post_json(
            client, f"/api/vendors/{data.vendor_id}/contacts",
            {"email": "stress-vc@test.example.com", "full_name": "STRESS_VContact"},
            phase,
        )
        data.vendor_contact_id = _extract_id(body)
        print(f"  Vendor contact id={data.vendor_contact_id}")

    # Error report
    r, body = await _post_json(
        client, "/api/error-reports",
        {"message": "STRESS_test error message", "title": "STRESS_Report"}, phase,
    )
    data.error_report_id = _extract_id(body)
    print(f"  Error report id={data.error_report_id}")

    ok = sum(1 for res in client.results if res.phase == phase and 200 <= res.status_code < 400)
    total = sum(1 for res in client.results if res.phase == phase)
    print(f"  {GREEN}✓ Setup complete: {ok}/{total} succeeded{RESET}")


async def phase_read_stress(client: StressClient, data: TestData, concurrency: int, reps: int):
    """Phase 2: Hammer GET endpoints with concurrent users."""
    phase = "Read Stress"
    print(f"\n{CYAN}▶ Phase 2: Read Stress{RESET} — {concurrency} concurrent, {reps} reps, {len(USER_IDS)} users...")

    rid = data.requisition_id or 1
    sid = data.site_id or 1
    vid = data.vendor_id or 1
    eid = data.error_report_id or 1

    # Endpoints accessible by all authenticated users
    all_user_endpoints = [
        # Public / Health
        "/health",
        "/auth/status",
        # Requisitions
        "/api/requisitions",
        "/api/requisitions?status=active",
        f"/api/requisitions/{rid}/requirements",
        f"/api/requisitions/{rid}/offers",
        f"/api/requisitions/{rid}/quotes",
        f"/api/requisitions/{rid}/contacts",
        f"/api/requisitions/{rid}/activity",
        # CRM — companies list, site detail, activities
        "/api/companies",
        f"/api/companies/{data.company_id}/activities" if data.company_id else None,
        f"/api/sites/{sid}",
        "/api/buy-plans",
        # Vendors
        "/api/vendors",
        "/api/vendors?search=STRESS_",
        f"/api/vendors/{vid}",
        f"/api/vendors/{vid}/contacts",
        f"/api/vendors/{vid}/parts-summary",
        f"/api/vendors/{vid}/email-metrics",
        # Performance
        "/api/performance/vendors",
        "/api/performance/buyers",
        "/api/performance/buyers/months",
        "/api/performance/salespeople",
        # Proactive
        "/api/proactive/matches",
        "/api/proactive/count",
        "/api/proactive/offers",
        "/api/proactive/scorecard",
        # Sources
        "/api/sources",
        # Enrichment
        "/api/enrichment/queue",
        "/api/enrichment/stats",
        "/api/enrichment/jobs",
        # Sales dashboard
        "/api/sales/my-accounts",
        "/api/sales/notifications",
    ]
    # Filter out None entries
    all_user_endpoints = [ep for ep in all_user_endpoints if ep]

    # Admin-only endpoints (only user_id=1)
    admin_endpoints = [
        "/api/admin/users",
        "/api/admin/config",
        "/api/admin/health",
        "/api/error-reports",
        f"/api/error-reports/{eid}",
        "/api/sales/manager-digest",
    ]

    sem = asyncio.Semaphore(concurrency)

    async def _fetch(endpoint: str, user_id: int):
        async with sem:
            await client.get(endpoint, phase=phase, user_id=user_id)

    tasks = []
    for _ in range(reps):
        for ep in all_user_endpoints:
            for uid in USER_IDS:
                tasks.append(_fetch(ep, uid))
        for ep in admin_endpoints:
            tasks.append(_fetch(ep, 1))  # admin only

    expected = len(tasks)
    print(f"  Dispatching {expected} requests...")
    await asyncio.gather(*tasks)

    phase_results = [r for r in client.results if r.phase == phase]
    ok = sum(1 for r in phase_results if 200 <= r.status_code < 400)
    errs = sum(1 for r in phase_results if r.status_code >= 400 and r.status_code != 429)
    limited = sum(1 for r in phase_results if r.status_code == 429)
    print(f"  {GREEN}✓ Read stress done: {ok} ok, {errs} errors, {limited} rate-limited out of {len(phase_results)}{RESET}")


async def phase_write_stress(client: StressClient, data: TestData, concurrency: int):
    """Phase 3: Concurrent mutations."""
    phase = "Write Stress"
    print(f"\n{CYAN}▶ Phase 3: Write Stress{RESET} — {concurrency} concurrent mutations...")

    sem = asyncio.Semaphore(concurrency)

    async def _create_company(i: int):
        async with sem:
            r, body = await _post_json(
                client, "/api/companies",
                {"name": f"STRESS_BulkCo_{i}"}, phase,
            )
            cid = _extract_id(body)
            if cid:
                data.extra_company_ids.append(cid)

    async def _create_requisition(i: int):
        async with sem:
            r, body = await _post_json(
                client, "/api/requisitions",
                {"name": f"STRESS_BulkReq_{i}"}, phase,
            )
            rid = _extract_id(body)
            if rid:
                data.extra_requisition_ids.append(rid)

    async def _update_company(cid: int, i: int):
        async with sem:
            await client.put(
                f"/api/companies/{cid}",
                phase=phase,
                json_body={"name": f"STRESS_BulkCo_{i}_updated"},
            )

    async def _add_vendor_review(vid: int, i: int):
        async with sem:
            await _post_json(
                client, f"/api/vendors/{vid}/reviews",
                {"rating": random.randint(1, 5), "comment": f"STRESS_review_{i}"},
                phase,
            )

    async def _add_vendor_contact(vid: int, i: int):
        async with sem:
            r, body = await _post_json(
                client, f"/api/vendors/{vid}/contacts",
                {"email": f"stress-bulk-{i}@test.example.com", "full_name": f"STRESS_VC_{i}"},
                phase,
            )
            vcid = _extract_id(body)
            if vcid:
                data.extra_vendor_contact_ids.append(vcid)

    async def _create_error_report(i: int):
        async with sem:
            r, body = await _post_json(
                client, "/api/error-reports",
                {"message": f"STRESS_bulk_error_{i}", "title": f"STRESS_BulkReport_{i}"},
                phase,
            )
            erid = _extract_id(body)
            if erid:
                data.extra_error_report_ids.append(erid)

    # Create companies and requisitions in parallel
    tasks = []
    for i in range(10):
        tasks.append(_create_company(i))
        tasks.append(_create_requisition(i))
    await asyncio.gather(*tasks)

    # Update the companies we just created
    tasks = []
    for i, cid in enumerate(data.extra_company_ids):
        tasks.append(_update_company(cid, i))
    await asyncio.gather(*tasks)

    # Vendor reviews, contacts, error reports
    tasks = []
    vid = data.vendor_id
    if vid:
        for i in range(5):
            tasks.append(_add_vendor_review(vid, i))
            tasks.append(_add_vendor_contact(vid, i))
    for i in range(5):
        tasks.append(_create_error_report(i))
    await asyncio.gather(*tasks)

    phase_results = [r for r in client.results if r.phase == phase]
    ok = sum(1 for r in phase_results if 200 <= r.status_code < 400)
    errs = sum(1 for r in phase_results if r.status_code >= 400 and r.status_code != 429)
    limited = sum(1 for r in phase_results if r.status_code == 429)
    print(f"  {GREEN}✓ Write stress done: {ok} ok, {errs} errors, {limited} rate-limited out of {len(phase_results)}{RESET}")


async def phase_mixed_load(client: StressClient, data: TestData, concurrency: int, total_ops: int):
    """Phase 4: 70% reads / 30% writes mixed load."""
    phase = "Mixed Load"
    print(f"\n{CYAN}▶ Phase 4: Mixed Load{RESET} — {total_ops} ops, {concurrency} concurrent, 70/30 read/write...")

    rid = data.requisition_id or 1
    cid = data.company_id or 1
    vid = data.vendor_id or 1

    read_endpoints = [
        "/api/requisitions",
        f"/api/requisitions/{rid}/requirements",
        "/api/companies",
        "/api/vendors",
        f"/api/vendors/{vid}",
        "/api/performance/vendors",
        "/api/performance/buyers",
        "/api/proactive/matches",
        "/api/proactive/count",
        "/api/enrichment/queue",
        "/api/sources",
        "/health",
    ]

    sem = asyncio.Semaphore(concurrency)
    counter = {"i": 0}

    async def _random_read():
        async with sem:
            ep = random.choice(read_endpoints)
            uid = random.choice(USER_IDS)
            await client.get(ep, phase=phase, user_id=uid)

    async def _random_write():
        async with sem:
            counter["i"] += 1
            i = counter["i"]
            choice = random.randint(0, 2)
            if choice == 0:
                await _post_json(
                    client, "/api/companies",
                    {"name": f"STRESS_Mixed_{i}"}, phase,
                )
            elif choice == 1:
                await _post_json(
                    client, "/api/requisitions",
                    {"name": f"STRESS_MixedReq_{i}"}, phase,
                )
            else:
                await _post_json(
                    client, "/api/error-reports",
                    {"message": f"STRESS_mixed_{i}", "title": f"STRESS_MixedReport_{i}"},
                    phase,
                )

    tasks = []
    for _ in range(total_ops):
        if random.random() < 0.7:
            tasks.append(_random_read())
        else:
            tasks.append(_random_write())

    await asyncio.gather(*tasks)

    phase_results = [r for r in client.results if r.phase == phase]
    ok = sum(1 for r in phase_results if 200 <= r.status_code < 400)
    errs = sum(1 for r in phase_results if r.status_code >= 400 and r.status_code != 429)
    limited = sum(1 for r in phase_results if r.status_code == 429)
    print(f"  {GREEN}✓ Mixed load done: {ok} ok, {errs} errors, {limited} rate-limited out of {len(phase_results)}{RESET}")


async def phase_cleanup(client: StressClient, data: TestData):
    """Phase 5: Delete/deactivate all STRESS_ data in reverse dependency order."""
    phase = "Cleanup"
    print(f"\n{CYAN}▶ Phase 5: Cleanup{RESET} — removing STRESS_ data...")

    # Delete offer
    if data.offer_id:
        await client.delete(f"/api/offers/{data.offer_id}", phase=phase)

    # Delete requirement
    if data.requirement_id:
        await client.delete(f"/api/requirements/{data.requirement_id}", phase=phase)

    # Delete vendor contacts (bulk + setup)
    for vcid in data.extra_vendor_contact_ids:
        if data.vendor_id:
            await client.delete(f"/api/vendors/{data.vendor_id}/contacts/{vcid}", phase=phase)
    if data.vendor_contact_id and data.vendor_id:
        await client.delete(f"/api/vendors/{data.vendor_id}/contacts/{data.vendor_contact_id}", phase=phase)

    # Delete site contact
    if data.site_contact_id and data.site_id:
        await client.delete(f"/api/sites/{data.site_id}/contacts/{data.site_contact_id}", phase=phase)

    # Archive requisitions (bulk + setup)
    for rid in data.extra_requisition_ids:
        await client.put(f"/api/requisitions/{rid}/archive", phase=phase)
    if data.requisition_id:
        await client.put(f"/api/requisitions/{data.requisition_id}/archive", phase=phase)

    # Archive companies (no DELETE endpoint — use PUT to deactivate)
    for cid in data.extra_company_ids:
        await client.put(
            f"/api/companies/{cid}", phase=phase,
            json_body={"name": f"[ARCHIVED] STRESS_BulkCo_{cid}"},
        )
    if data.company_id:
        await client.put(
            f"/api/companies/{data.company_id}", phase=phase,
            json_body={"name": "[ARCHIVED] STRESS_TestCo"},
        )

    # Delete vendor card
    if data.vendor_id:
        await client.delete(f"/api/vendors/{data.vendor_id}", phase=phase)

    # Clean up mixed-phase data: archive remaining STRESS_ companies and requisitions
    try:
        resp = await client._client.request(
            "GET", "/api/companies", params={"search": "STRESS_"},
            cookies=client._cookies(1), headers=client._headers(),
        )
        companies = resp.json()
        # list_companies returns a plain list
        items = companies if isinstance(companies, list) else []
        for c in items:
            cid = c.get("id")
            if cid:
                await client.put(
                    f"/api/companies/{cid}", phase=phase,
                    json_body={"name": f"[ARCHIVED] {c.get('name', 'STRESS')}"},
                )
    except Exception:
        pass

    # Archive all remaining STRESS_ requisitions (paginated search)
    try:
        for _ in range(5):  # max 5 passes to avoid infinite loop
            resp = await client._client.request(
                "GET", "/api/requisitions",
                params={"q": "STRESS_", "status": "active", "limit": "500"},
                cookies=client._cookies(1), headers=client._headers(),
            )
            reqs = resp.json()
            if isinstance(reqs, dict):
                items = reqs.get("requisitions", [])
            elif isinstance(reqs, list):
                items = reqs
            else:
                items = []
            active = [r for r in items if r.get("status") not in ("archived",)]
            if not active:
                break
            for r in active:
                if r.get("id"):
                    await client.put(
                        f"/api/requisitions/{r['id']}/archive", phase=phase,
                    )
    except Exception:
        pass

    phase_results = [r for r in client.results if r.phase == phase]
    ok = sum(1 for r in phase_results if 200 <= r.status_code < 400)
    total = len(phase_results)
    print(f"  {GREEN}✓ Cleanup done: {ok}/{total} succeeded{RESET}")


# ── Reporting ────────────────────────────────────────────────────────────


def print_results(client: StressClient):
    """Print the results table."""
    phases = ["Setup", "Read Stress", "Write Stress", "Mixed Load", "Cleanup"]
    stats = {}
    for p in phases:
        ps = PhaseStats(name=p)
        ps.results = [r for r in client.results if r.phase == p]
        stats[p] = ps

    all_results = client.results
    total = len(all_results)
    total_ok = sum(1 for r in all_results if 200 <= r.status_code < 400)
    total_err = sum(1 for r in all_results if r.status_code >= 400 and r.status_code != 429)
    total_429 = sum(1 for r in all_results if r.status_code == 429)
    denominator = total - total_429
    error_rate = (total_err / denominator * 100) if denominator > 0 else 0
    passed = error_rate <= 5.0

    bar = "═" * 58
    line = "─" * 58

    print(f"\n{BOLD}{bar}{RESET}")
    print(f"{BOLD}  AVAIL STRESS TEST RESULTS{RESET}")
    print(f"{BOLD}{bar}{RESET}")
    print(
        f"  {'Phase':<16s} {'Reqs':>5s} {'OK':>5s} {'Err':>5s} {'429':>5s} "
        f"{'p50':>7s} {'p95':>7s} {'p99':>7s}"
    )

    for p in phases:
        ps = stats[p]
        if ps.total == 0:
            continue
        print(
            f"  {ps.name:<16s} {ps.total:>5d} {ps.success:>5d} {ps.errors:>5d} "
            f"{ps.rate_limited:>5d} {ps.p50:>6.0f}ms {ps.p95:>6.0f}ms {ps.p99:>6.0f}ms"
        )

    print(f"  {line}")
    print(
        f"  {'TOTAL':<16s} {total:>5d} {total_ok:>5d} {total_err:>5d} {total_429:>5d}"
    )

    if passed:
        print(f"  Error Rate: {error_rate:.2f}%  {GREEN}✓ PASS{RESET}")
    else:
        print(f"  Error Rate: {error_rate:.2f}%  {RED}✗ FAIL{RESET}")
    print(f"{BOLD}{bar}{RESET}")

    # Status code distribution
    print(f"\n{BOLD}Status Code Distribution:{RESET}")
    code_counts: dict[int, int] = {}
    for r in all_results:
        code_counts[r.status_code] = code_counts.get(r.status_code, 0) + 1
    for code in sorted(code_counts):
        count = code_counts[code]
        color = GREEN if 200 <= code < 400 else (YELLOW if code == 429 else RED)
        print(f"  {color}{code}{RESET}: {count}")

    # Slowest endpoints
    print(f"\n{BOLD}Top 10 Slowest Requests:{RESET}")
    by_duration = sorted(all_results, key=lambda r: r.duration_ms, reverse=True)[:10]
    for r in by_duration:
        color = GREEN if 200 <= r.status_code < 400 else (YELLOW if r.status_code == 429 else RED)
        print(
            f"  {r.duration_ms:>7.0f}ms  {r.method:>4s} {r.path:<50s} "
            f"{color}{r.status_code}{RESET}  user={r.user_id}"
        )

    # Errors detail
    errors = [r for r in all_results if r.status_code >= 400 and r.status_code != 429]
    if errors:
        print(f"\n{BOLD}Errors ({len(errors)}):{RESET}")
        seen = set()
        for r in errors:
            key = (r.method, r.path, r.status_code)
            if key not in seen:
                seen.add(key)
                err_msg = r.error[:100] if r.error else "no detail"
                print(f"  {RED}{r.method} {r.path} → {r.status_code}{RESET}: {DIM}{err_msg}{RESET}")

    return passed


# ── Main ─────────────────────────────────────────────────────────────────


async def main():
    parser = argparse.ArgumentParser(description="AvailAI API Stress Test")
    parser.add_argument("--base-url", default=BASE_URL, help="Base URL (default: $STRESS_BASE_URL or http://localhost:8000)")
    parser.add_argument("--concurrency", type=int, default=READ_CONCURRENCY, help="Read concurrency (default: 50)")
    parser.add_argument("--reps", type=int, default=READ_REPS, help="Read repetitions (default: 3)")
    args = parser.parse_args()

    print(f"{BOLD}{'═' * 58}{RESET}")
    print(f"{BOLD}  AVAIL STRESS TEST{RESET}")
    print(f"{BOLD}{'═' * 58}{RESET}")
    print(f"  Target:      {args.base_url}")
    print(f"  Concurrency: read={args.concurrency} write={WRITE_CONCURRENCY} mixed={MIXED_CONCURRENCY}")
    print(f"  Reps:        {args.reps}")
    print(f"  Users:       {USER_IDS}")

    # Verify app is healthy before starting
    print(f"\n{DIM}Checking app health...{RESET}")
    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=10) as hc:
            resp = await hc.get("/health")
            if resp.status_code != 200:
                print(f"{RED}✗ App not healthy: {resp.status_code}{RESET}")
                sys.exit(1)
            print(f"  {GREEN}✓ App is healthy{RESET}")
    except Exception as e:
        print(f"{RED}✗ Cannot reach app: {e}{RESET}")
        sys.exit(1)

    data = TestData()
    start_time = time.perf_counter()

    async with StressClient(args.base_url) as client:
        await phase_setup(client, data)
        await phase_read_stress(client, data, args.concurrency, args.reps)
        await phase_write_stress(client, data, WRITE_CONCURRENCY)
        await phase_mixed_load(client, data, MIXED_CONCURRENCY, MIXED_OPS)
        await phase_cleanup(client, data)

        elapsed = time.perf_counter() - start_time
        print(f"\n{DIM}Total wall time: {elapsed:.1f}s{RESET}")

        passed = print_results(client)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
