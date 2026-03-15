LOCAL_SETUP.md — Local development and testing for AvailAI

This file explains how to run AvailAI on your own machine for development:
what you need installed, how to start the stack in Docker, how to run the app
directly with uvicorn, and how to run the test suite. It is read by humans
only (no code imports it) and depends on the main repo files like
`docker-compose.yml`, `docker-compose.local.yml`, `.env`, and `requirements.txt`.

---

## 1. Prerequisites

- **Docker + Docker Compose** (recommended way to run everything locally)
- **Python 3.12** (for running the app directly or tests without Docker)
- **Node 20 + npm** (only if you are rebuilding the frontend)

You should also have a **`.env`** file at the project root. For local dev you
can usually copy the example and edit:

```bash
cp .env.example .env   # if the example file exists
```

At minimum set:

- `SESSION_SECRET` to a random hex string (not the default)
- any API keys you actually want to use locally (can be fake in TESTING mode)

---

## 2. Run everything locally with Docker (recommended)

This uses PostgreSQL, Redis, the app, and Caddy, with local ports that do not
collide with system services.

From the project root:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
```

What this does:

- Builds the app image and installs dependencies
- Runs Alembic migrations (`alembic upgrade head`) inside the app container
- Starts:
  - app: FastAPI backend on port `8000` (mapped as `http://localhost:8000`)
  - db: PostgreSQL on `localhost:5432`
  - redis: Redis cache
  - caddy: reverse proxy on `http://localhost:8080` and `https://localhost:8443`

**Logs:**

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f app
```

**Stop everything:**

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

**Notes about logs:**

- `EXTRA_LOGS=0` (set in `docker-compose.local.yml`) gives **human-readable**
  logs and hides noisy APScheduler job messages.
- Set `EXTRA_LOGS=1` if you want full JSON logs and all scheduler messages.

---

## 3. Run the app locally without Docker

You can also run the FastAPI app directly, using your own Python environment.

From the project root:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

Make sure your `.env` has a valid `DATABASE_URL` (e.g. a local Postgres, or the
same DB used by Docker). Then:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Now open `http://localhost:8000` in your browser.

If you still want Redis and Postgres managed by Docker while running the app
directly, you can start only the infra:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d db redis
```

---

## 4. Running tests locally

Tests use in-memory SQLite and `TESTING=1`, so they do **not** hit real
external services or require a live Postgres database.

From the project root with your virtualenv activated:

```bash
pytest tests/
```

Run a single test file:

```bash
pytest tests/test_routers_rfq.py
```

Run a single test by name:

```bash
pytest tests/test_routers_rfq.py -k "test_send"
```

More verbose output:

```bash
pytest -v
```

---

## 5. Migrations and one-time fixes

**Correct migration sequence (single head):**
`… → 017_proactive_matches_cph → 018_missing_orm_cols → 019_activity_req_channel → 020 → … → 047`

- **018** already adds `site_contacts.contact_status` (and other ORM columns). There is no separate “019 contact_status” migration in the repo.
- If you see **“Multiple head revisions”** or **“Can't locate revision 019_site_contacts_contact_status_ensure”**, your DB’s `alembic_version` is pointing at a revision that was removed. Reset it so the next `alembic upgrade head` runs the canonical 019 → 047 chain.

**One-time fix (local DB stuck on removed revision):**

1. Start only the DB (so you can run SQL):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml up -d db
   ```
2. Set the stored revision back to 018:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml exec db \
     psql -U availai -d availai -c "UPDATE alembic_version SET version_num = '018_missing_orm_cols' WHERE version_num = '019_site_contacts_contact_status_ensure';"
   ```
3. Bring up the full stack (entrypoint will run `alembic upgrade head` → 019 through 047):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml down
   docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
   ```

Optional: run the same SQL from the script:
`docker compose -f docker-compose.yml -f docker-compose.local.yml exec -T db psql -U availai -d availai < scripts/fix_alembic_stuck_019.sql`

---

## 6. Handy Docker commands (local)

From the project root:

```bash
# Restart the app container
docker compose -f docker-compose.yml -f docker-compose.local.yml restart app

# See running containers
docker compose -f docker-compose.yml -f docker-compose.local.yml ps

# Follow all logs
docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f
```

(Previously “Section 5” — renumbered to 6.)
