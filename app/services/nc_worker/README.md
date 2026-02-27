# NetComponents Search Worker

Automated browser-based search worker for the NetComponents electronic component marketplace.

## Prerequisites

- Ubuntu 22.04+ (DigitalOcean droplet)
- Google Chrome installed
- Xvfb for virtual display
- Patchright (undetected Playwright fork)
- Valid NetComponents account credentials

## Installation

```bash
sudo bash scripts/setup_nc_worker.sh
```

## Configuration

Add to your `.env` file:

```env
NC_USERNAME=your_nc_email@company.com
NC_PASSWORD=your_nc_password
NC_MAX_DAILY_SEARCHES=75
NC_BROWSER_PROFILE_DIR=/home/avail/nc_browser_profile
```

Optional overrides (with defaults):

```env
NC_MAX_HOURLY_SEARCHES=12
NC_MIN_DELAY_SECONDS=120
NC_MAX_DELAY_SECONDS=420
NC_TYPICAL_DELAY_SECONDS=240
NC_DEDUP_WINDOW_DAYS=7
NC_BUSINESS_HOURS_START=8
NC_BUSINESS_HOURS_END=18
```

## Service Management

```bash
# Start
sudo systemctl start avail-xvfb
sudo systemctl start avail-nc-worker

# Stop
sudo systemctl stop avail-nc-worker

# Restart (after code changes)
sudo systemctl restart avail-nc-worker

# Check status
sudo systemctl status avail-nc-worker

# View logs (live)
sudo journalctl -u avail-nc-worker -f

# View last 100 lines
sudo journalctl -u avail-nc-worker -n 100
```

## How It Works

1. **Queue**: When requirements are created in AVAIL, they're automatically added to `nc_search_queue`
2. **AI Gate**: Claude Haiku classifies parts as worth searching (semiconductors, ICs) or skip (passives, connectors)
3. **Search**: Worker opens Chrome via Xvfb, logs into NetComponents, searches queued parts
4. **Parse**: HTML results are parsed into structured sighting records
5. **Save**: Sightings are written to AVAIL's sightings table (source_type='netcomponents')

## Safety Features

- **Business hours only**: Searches run 8 AM - 6 PM Eastern, weekdays
- **Rate limiting**: Max 75 searches/day, 120-420s between searches
- **Human simulation**: Random typing speed, click positions, and delays
- **Circuit breaker**: Auto-stops on captcha, rate limiting, or blocking signals
- **Deduplication**: Same MPN won't be searched again within 7 days
- **Periodic breaks**: Random 5-25 minute breaks every 8-15 searches

## Troubleshooting

### Worker won't start
- Check Xvfb is running: `sudo systemctl status avail-xvfb`
- Check Chrome is installed: `google-chrome --version`
- Check env vars: ensure NC_USERNAME and NC_PASSWORD are set

### Circuit breaker tripped
- Check logs: `sudo journalctl -u avail-nc-worker -n 50`
- View breaker status: `GET /api/nc/worker/health`
- Force-search a specific item: `POST /api/nc/queue/{id}/force-search`
- The breaker auto-recovers after 1 hour sleep

### Session issues
- The worker auto-re-authenticates when sessions expire
- If login keeps failing, check NC credentials
- Try deleting the browser profile: `rm -rf /home/avail/nc_browser_profile/*`

### No results being found
- Check if parts are being gated out: `GET /api/nc/queue/items?status=gated_out`
- Force-search a known part: `POST /api/nc/queue/{id}/force-search`
- Check the HTML parser: results HTML structure may have changed (check logs for hash warnings)

## Ramp-Up Schedule

- **Week 1**: 20 searches/day (`NC_MAX_DAILY_SEARCHES=20`)
- **Week 2**: 50 searches/day (if no blocking signals)
- **Week 3**: 75 searches/day (full speed)
