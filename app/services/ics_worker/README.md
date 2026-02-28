# ICsource Search Worker

Browser-automation worker that searches icsource.com for electronic component inventory listings.
Mirrors the architecture of the NetComponents worker (`app/services/nc_worker/`).

## Quick Start

```bash
# Set credentials in .env
ICS_USERNAME=trioscs
ICS_PASSWORD=trio92627
ICS_BROWSER_PROFILE_DIR=/root/ics_browser_profile

# Run the worker (requires Xvfb)
DISPLAY=:99 PYTHONPATH=/root/availai python -m app.services.ics_worker.worker
```

## Architecture

```
ics_worker/
├── worker.py          # Main loop: gate → poll → search → parse → save
├── config.py          # ICS_* env vars with defaults
├── session_manager.py # Patchright browser + ICsource login
├── search_engine.py   # ASP.NET WebForms search via form fill + click
├── result_parser.py   # BeautifulSoup HTML → IcsSighting dataclass
├── sighting_writer.py # IcsSighting → AVAIL Sighting DB records
├── queue_manager.py   # Enqueue, dedup, poll, status updates
├── ai_gate.py         # Claude Haiku commodity classification
├── circuit_breaker.py # Detection avoidance (captcha, redirect, etc.)
├── scheduler.py       # Log-normal delays, breaks, business hours
├── human_behavior.py  # Keystroke timing, click jitter
├── mpn_normalizer.py  # Strip packaging suffixes for dedup
└── monitoring.py      # Sentry alerts, HTML structure tracking
```

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `ICS_USERNAME` | (empty) | ICsource login username |
| `ICS_PASSWORD` | (empty) | ICsource login password |
| `ICS_MAX_DAILY_SEARCHES` | 50 | Max searches per day |
| `ICS_MIN_DELAY_SECONDS` | 150 | Min seconds between searches |
| `ICS_TYPICAL_DELAY_SECONDS` | 270 | Typical delay (log-normal center) |
| `ICS_MAX_DELAY_SECONDS` | 420 | Max seconds between searches |
| `ICS_DEDUP_WINDOW_DAYS` | 7 | Skip re-searching same MPN within N days |
| `ICS_BUSINESS_HOURS_START` | 8 | Start hour (Eastern time) |
| `ICS_BUSINESS_HOURS_END` | 18 | End hour (Eastern time) |
| `ICS_BROWSER_PROFILE_DIR` | `/root/ics_browser_profile` | Persistent cookie storage |

## Deployment (systemd)

```bash
cp deploy/avail-ics-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable avail-ics-worker
systemctl start avail-ics-worker
```

Requires `avail-xvfb.service` running on DISPLAY=:99 (shared with NC worker).

## Database Tables

Created by migration `031_ics_search_tables`:
- `ics_search_queue` — queue items with AI gate classification
- `ics_search_log` — audit trail per search
- `ics_worker_status` — singleton health row (id=1)
- `ics_classification_cache` — persisted AI gate decisions

## Admin API

- `GET /api/ics/queue/stats` — queue statistics
- `GET /api/ics/queue/items?status=queued` — list items
- `POST /api/ics/queue/{id}/force-search` — re-queue item
- `POST /api/ics/queue/{id}/skip` — skip item
- `GET /api/ics/worker/health` — worker health + circuit breaker

## Backfill

Queue existing requirements that haven't been searched on ICsource:

```bash
PYTHONPATH=/root/availai python scripts/ics_backfill.py --limit 100
PYTHONPATH=/root/availai python scripts/ics_backfill.py --dry-run
```

## Troubleshooting

- **Login fails**: Check credentials. ICsource uses Telerik AJAX — login button must be clicked (not Enter).
- **No results**: Check circuit breaker status at `/api/ics/worker/health`.
- **Browser crashes**: Ensure Xvfb is running: `systemctl status avail-xvfb`.
- **Session expired**: Worker auto-reconnects. Check `ICS_BROWSER_PROFILE_DIR` exists and is writable.
