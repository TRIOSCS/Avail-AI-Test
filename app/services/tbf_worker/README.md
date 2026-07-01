# The Broker Forum (TBF) Search Worker

Browser-automation worker that searches thebrokersite.com — a European broker
marketplace — for electronic component inventory listings. Mirrors the architecture
of the ICsource worker (`app/services/ics_worker/`).

**Phase 1 ships DORMANT:** the full scaffold + DB + ops + registration exist, but the
four site-specific files (`session_manager`, `search_engine`, `result_parser`,
`circuit_breaker`) carry placeholder selectors marked `# TODO(phase2)`. The worker
stays idle until member credentials and real selectors (from a logged-in capture) are
in place.

## Quick Start

```bash
# Set credentials in .env.tbf-worker (chmod 600, host-only, never committed)
TBF_USERNAME=your-member-login
TBF_PASSWORD=your-member-password
TBF_BROWSER_PROFILE_DIR=/root/tbf_browser_profile

# Run the worker (requires Xvfb)
DISPLAY=:99 PYTHONPATH=/root/availai python -m app.services.tbf_worker.worker
```

## Architecture

```
tbf_worker/
├── worker.py          # Main loop: gate → poll → search → parse → save
├── config.py          # TBF_* env vars with defaults
├── session_manager.py # Patchright browser + TBF login            (phase-2 selectors)
├── search_engine.py   # Browser search via form fill + click       (phase-2 selectors)
├── result_parser.py   # BeautifulSoup HTML → TbfSighting dataclass  (phase-2 selectors)
├── sighting_writer.py # TbfSighting → AVAIL Sighting DB records
├── queue_manager.py   # Enqueue, dedup, poll, status updates
├── ai_gate.py         # Claude Haiku commodity classification (in-memory cache)
├── circuit_breaker.py # Detection avoidance                         (phase-2 markers)
├── scheduler.py       # Log-normal delays, breaks, business hours
├── human_behavior.py  # Keystroke timing, click jitter
├── mpn_normalizer.py  # Strip packaging suffixes for dedup
└── monitoring.py      # Sentry alerts, HTML structure tracking
```

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `TBF_USERNAME` | (empty) | TBF member login username |
| `TBF_PASSWORD` | (empty) | TBF member login password |
| `TBF_MAX_DAILY_SEARCHES` | 50 | Max searches per day |
| `TBF_MIN_DELAY_SECONDS` | 180 | Min seconds between searches |
| `TBF_TYPICAL_DELAY_SECONDS` | 300 | Typical delay (log-normal center) |
| `TBF_MAX_DELAY_SECONDS` | 600 | Max seconds between searches |
| `TBF_DEDUP_WINDOW_DAYS` | 7 | Skip re-searching same MPN within N days |
| `TBF_BROWSER_PROFILE_DIR` | `/root/tbf_browser_profile` | Persistent cookie storage |
| `TBF_SEARCH_TIMEOUT_SECONDS` | 150 | Hard cap on one search |
| `TBF_BREAKER_COOLDOWN_MINUTES` | 30 | Circuit-breaker self-heal cooldown |

## Deployment (systemd)

```bash
sudo bash scripts/setup_tbf_worker.sh   # one-time bootstrap (Xvfb, Chrome, venv, unit)
sudo systemctl start avail-tbf-worker
sudo journalctl -u avail-tbf-worker -f
```

Requires `avail-xvfb.service` running on DISPLAY=:99 (shared with NC/ICS workers).

## Database Tables

Created by migration `130_tbf_search_tables`:
- `tbf_search_queue` — queue items with AI gate classification + compound
  `(requirement_id, normalized_mpn)` unique constraint
- `tbf_search_log` — audit trail per search
- `tbf_worker_status` — singleton health row (id=1)

(No TBF classification-cache DB table — the AI gate caches in-process only.)

## Rollout (3-week ramp, Phase 2)

After a logged-in capture fills the four stub files' selectors and credentials live
in `.env.tbf-worker`, ramp the daily cap conservatively:

1. **Week 1** — `TBF_MAX_DAILY_SEARCHES=50`, watch `journalctl -u avail-tbf-worker`
   for login health and the circuit breaker.
2. **Week 2** — if clean, raise gradually while monitoring for captcha / rate-limit
   trips.
3. **Week 3** — settle at a steady-state cap that stays well under any observed
   rate-limit behavior.

## Troubleshooting

- **Login fails**: Check `TBF_USERNAME` / `TBF_PASSWORD` in `.env.tbf-worker`.
- **`NotImplementedError: phase2: selectors`**: expected in Phase 1 — selectors not
  yet encoded; the worker is intentionally dormant.
- **Browser crashes**: Ensure Xvfb is running: `systemctl status avail-xvfb`.
- **Session expired**: Worker auto-reconnects (once Phase 2 markers exist). Check
  `TBF_BROWSER_PROFILE_DIR` exists and is writable.
