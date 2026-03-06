"""Find Trouble service -- orchestrates the test-fix-retest loop.

Manages background asyncio task that runs:
  Phase 1: Playwright sweep (SiteTester -- clicks every button)
  Phase 2: Claude agent deep testing (test-site.sh subprocess)
  Then: auto-process tickets, wait for fixes, repeat until clean.

Called by: routers/trouble_tickets.py (find-trouble endpoints)
Depends on: services/site_tester.py, services/trouble_ticket_service.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

from loguru import logger

MAX_ROUNDS = 10
CLEAN_ROUNDS_TO_STOP = 2
FIX_QUEUE_POLL_INTERVAL = 15  # seconds
FIX_QUEUE_MAX_WAIT = 300  # 5 minutes

AREAS = [
    "search", "requisitions", "rfq", "crm_companies", "crm_contacts",
    "crm_quotes", "prospecting", "vendors", "tagging", "tickets",
    "admin_api_health", "admin_settings", "notifications", "auth",
    "upload", "pipeline", "activity",
]


class FindTroubleService:
    """Singleton-style manager for the Find Trouble test loop."""

    def __init__(self) -> None:
        self.active_job: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None
        self._events: list[dict[str, Any]] = []

    @property
    def is_running(self) -> bool:
        return self.active_job is not None and self.active_job.get("running", False)

    def try_start(self, base_url: str, session_cookie: str) -> dict | None:
        """Try to start the loop. Returns job info or None if already running."""
        if self.is_running:
            return None

        self.active_job = {
            "running": True,
            "cancel": False,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "round": 0,
            "max_rounds": MAX_ROUNDS,
            "phase": "starting",
            "areas": {a: "pending" for a in AREAS},
            "tickets_created": 0,
            "tickets_healed": 0,
            "consecutive_clean": 0,
        }
        self._events = []

        self._task = asyncio.create_task(
            self._run_loop(base_url, session_cookie)
        )
        self._emit("started", "Find Trouble loop started")
        return {"status": "started"}

    def stop(self) -> bool:
        """Request cancellation of the running job."""
        if not self.is_running or not self.active_job:
            return False
        self.active_job["cancel"] = True
        self._emit("stopping", "Stop requested -- finishing current phase")
        return True

    def get_status(self) -> dict:
        """Current status snapshot."""
        if not self.active_job:
            return {"running": False, "round": 0, "phase": "idle", "areas": {}, "events": []}
        return {
            "running": self.active_job.get("running", False),
            "round": self.active_job.get("round", 0),
            "max_rounds": self.active_job.get("max_rounds", MAX_ROUNDS),
            "phase": self.active_job.get("phase", "idle"),
            "areas": self.active_job.get("areas", {}),
            "tickets_created": self.active_job.get("tickets_created", 0),
            "tickets_healed": self.active_job.get("tickets_healed", 0),
            "consecutive_clean": self.active_job.get("consecutive_clean", 0),
            "started_at": self.active_job.get("started_at"),
            "events": self._events[-50:],
        }

    def consume_events(self, after: int = 0) -> list[dict]:
        """Get events after index `after` for SSE streaming."""
        return self._events[after:]

    def _emit(self, event_type: str, message: str, **extra: Any) -> None:
        self._events.append({
            "type": event_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **extra,
        })

    async def _run_loop(self, base_url: str, session_cookie: str) -> None:
        """Main loop: sweep -> tickets -> heal -> wait -> repeat."""
        job = self.active_job
        if not job:
            return

        try:
            for round_num in range(1, MAX_ROUNDS + 1):
                if job["cancel"]:
                    self._emit("cancelled", f"Cancelled before round {round_num}")
                    break

                job["round"] = round_num
                self._emit("round_start", f"Round {round_num}/{MAX_ROUNDS}")
                logger.info("find_trouble: round {}/{}", round_num, MAX_ROUNDS)

                for a in AREAS:
                    job["areas"][a] = "pending"

                # Phase 1: Playwright sweep
                job["phase"] = "sweep"
                self._emit("phase", "Phase 1: Playwright sweep")
                sweep_issues = await self._run_playwright_sweep(base_url, session_cookie, job)

                if job["cancel"]:
                    break

                # Phase 2: Claude agent deep testing
                job["phase"] = "deep_test"
                self._emit("phase", "Phase 2: Deep agent testing")
                await self._run_deep_test(base_url, job)

                if job["cancel"]:
                    break

                # Create tickets from sweep issues
                job["phase"] = "creating_tickets"
                new_count = await self._create_tickets(sweep_issues, job)

                if job["cancel"]:
                    break

                # Auto-process tickets
                job["phase"] = "healing"
                self._emit("phase", f"Auto-healing {new_count} new tickets")
                healed = await self._auto_process_new_tickets(job)
                job["tickets_healed"] += healed

                if new_count == 0:
                    job["consecutive_clean"] += 1
                    self._emit("clean", f"Clean round ({job['consecutive_clean']} consecutive)")
                    if job["consecutive_clean"] >= CLEAN_ROUNDS_TO_STOP:
                        self._emit("complete", f"Stopped: {CLEAN_ROUNDS_TO_STOP} consecutive clean rounds")
                        break
                else:
                    job["consecutive_clean"] = 0

                # Wait for fix queue to drain
                if new_count > 0:
                    job["phase"] = "waiting_fixes"
                    self._emit("phase", "Waiting for fixes to be applied...")
                    await self._wait_for_fixes(job)

            else:
                self._emit("complete", f"Reached max {MAX_ROUNDS} rounds")

        except Exception as e:
            logger.exception("find_trouble: loop error")
            self._emit("error", f"Loop error: {e}")
        finally:
            job["running"] = False
            job["phase"] = "done"
            self._emit("done", "Find Trouble loop finished")
            logger.info("find_trouble: loop finished -- {} tickets created, {} healed",
                        job["tickets_created"], job["tickets_healed"])

    async def _run_playwright_sweep(
        self, base_url: str, session_cookie: str, job: dict
    ) -> list[dict]:
        """Phase 1: Run SiteTester across all areas."""
        from app.services.site_tester import SiteTester

        tester = SiteTester(base_url=base_url, session_cookie=session_cookie)

        try:
            issues = await tester.run_full_sweep()
        except Exception as e:
            logger.error("find_trouble: sweep failed: {}", e)
            self._emit("error", f"Playwright sweep failed: {e}")
            return []

        for p in tester.progress:
            area = p.get("area", "")
            if area in job["areas"]:
                job["areas"][area] = "pass"

        for issue in issues:
            area = issue.get("area", "")
            if area in job["areas"]:
                job["areas"][area] = "fail"

        self._emit("sweep_done", f"Sweep complete: {len(issues)} issues in {len(tester.progress)} areas")
        return issues

    async def _run_deep_test(self, base_url: str, job: dict) -> None:
        """Phase 2: Run test-site.sh via subprocess for Claude agent testing.

        Uses asyncio.create_subprocess_exec (not shell) to avoid injection.
        The script path is hardcoded relative to this file, not from user input.
        """
        script_path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "test-site.sh")
        script_path = os.path.normpath(script_path)

        if not os.path.isfile(script_path):
            self._emit("skip", "test-site.sh not found -- skipping deep test phase")
            logger.warning("find_trouble: test-site.sh not found at {}", script_path)
            return

        self._emit("deep_test_start", "Launching Claude agent deep tests...")

        try:
            env = os.environ.copy()
            env["BASE_URL"] = base_url

            proc = await asyncio.create_subprocess_exec(
                "bash", script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=os.path.dirname(script_path),
            )

            while True:
                if job["cancel"]:
                    proc.terminate()
                    break
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=660)
                except asyncio.TimeoutError:
                    self._emit("deep_test_timeout", "Deep test stdout idle for 11 min -- moving on")
                    proc.terminate()
                    break
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                if "[PASS]" in text:
                    area = text.split("]")[-1].strip().split()[0] if "]" in text else ""
                    if area in job["areas"]:
                        job["areas"][area] = "pass"
                    self._emit("agent_pass", text, area=area)
                elif "[FAIL]" in text:
                    area = text.split("]")[-1].strip().split()[0] if "]" in text else ""
                    if area in job["areas"]:
                        job["areas"][area] = "fail"
                    self._emit("agent_fail", text, area=area)
                elif "[TIME]" in text:
                    area = text.split("]")[-1].strip().split()[0] if "]" in text else ""
                    if area in job["areas"]:
                        job["areas"][area] = "timeout"
                    self._emit("agent_timeout", text, area=area)

            await proc.wait()
            self._emit("deep_test_done", f"Deep tests finished (exit code {proc.returncode})")

        except FileNotFoundError:
            self._emit("skip", "bash not available -- skipping deep test phase")
        except Exception as e:
            self._emit("error", f"Deep test error: {e}")
            logger.warning("find_trouble: deep test error: {}", e)

    async def _create_tickets(self, sweep_issues: list[dict], job: dict) -> int:
        """Create tickets from Playwright sweep issues with dedup."""
        if not sweep_issues:
            return 0

        from app.database import SessionLocal
        from app.services.site_tester import create_tickets_from_issues

        db = SessionLocal()
        try:
            count = await create_tickets_from_issues(sweep_issues, db)
            job["tickets_created"] += count
            self._emit("tickets_created", f"Created {count} tickets from sweep ({len(sweep_issues)} issues)")
            return count
        except Exception as e:
            self._emit("error", f"Ticket creation failed: {e}")
            return 0
        finally:
            db.close()

    async def _auto_process_new_tickets(self, job: dict) -> int:
        """Auto-diagnose and execute fixes for new playwright tickets."""
        from app.database import SessionLocal
        from app.models.trouble_ticket import TroubleTicket
        from app.services.trouble_ticket_service import auto_process_ticket

        db = SessionLocal()
        try:
            recent = (
                db.query(TroubleTicket)
                .filter(TroubleTicket.source == "playwright")
                .filter(TroubleTicket.status == "submitted")
                .order_by(TroubleTicket.id.desc())
                .limit(50)
                .all()
            )

            healed = 0
            for ticket in recent:
                if job["cancel"]:
                    break
                try:
                    await auto_process_ticket(ticket.id)
                    healed += 1
                    self._emit("healed", f"Auto-processed ticket #{ticket.id}: {ticket.title[:60]}")
                except Exception as e:
                    logger.warning("find_trouble: auto-process failed for #{}: {}", ticket.id, e)

            return healed
        finally:
            db.close()

    async def _wait_for_fixes(self, job: dict) -> None:
        """Wait for fix_queue/ to be empty (watcher applies fixes)."""
        queue_dir = os.environ.get("FIX_QUEUE_DIR", "/app/fix_queue")
        waited = 0

        while waited < FIX_QUEUE_MAX_WAIT:
            if job["cancel"]:
                return

            try:
                pending = [
                    f for f in os.listdir(queue_dir)
                    if f.endswith(".json") and os.path.isfile(os.path.join(queue_dir, f))
                ]
            except FileNotFoundError:
                return

            if not pending:
                self._emit("fixes_done", "Fix queue empty -- ready for next round")
                return

            self._emit("waiting", f"Waiting for {len(pending)} fix(es) to be applied...")
            await asyncio.sleep(FIX_QUEUE_POLL_INTERVAL)
            waited += FIX_QUEUE_POLL_INTERVAL

        self._emit("timeout", "Fix queue wait timed out -- proceeding to next round")


# Module-level singleton
_service = FindTroubleService()


def get_find_trouble_service() -> FindTroubleService:
    return _service
