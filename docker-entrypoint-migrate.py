"""docker-entrypoint-migrate.py — serialize `alembic upgrade head` across containers
sharing one database.

Phase-4 infra split gave APScheduler its own `scheduler` compose service,
which shares the app image/Dockerfile/ENTRYPOINT (docker-entrypoint.sh) with
`app` and `enrichment-worker`. Every container from this image runs `alembic
upgrade head` at boot; `scheduler`'s depends_on is db+redis only (not `app`,
per the Phase-4 locked ruling), so on a schema-changing deploy `app` and
`scheduler` can start their migration runs at the same moment. Plain
`alembic upgrade head` has no built-in concurrency protection — two
containers racing DDL against the same schema can deadlock or partially
apply a migration.

This wrapper takes a Postgres session-level advisory lock BEFORE invoking
alembic and holds it for the lock's connection's lifetime (i.e. until this
process exits, since session-level advisory locks release automatically when
their connection closes — no explicit pg_advisory_unlock needed). A
container that loses the race simply blocks until the winner's migration
completes; by the time it acquires the lock, alembic is already at head and
its own `alembic upgrade head` is a no-op.

Runs BEFORE any app code is meaningfully importable as a server process, so
this stays dependency-free beyond what the image already ships for the app
itself: psycopg2-binary (already a hard requirement.txt dependency — the
same driver SQLAlchemy uses) and the stdlib.

Called by: docker-entrypoint.sh, in place of a bare `alembic upgrade head`,
via `runuser -u appuser -- python3 docker-entrypoint-migrate.py` (so the
alembic subprocess it spawns inherits the same non-root appuser identity the
bare command ran under before this change).
"""

import os
import subprocess
import sys

import psycopg2

# Fixed, arbitrary lock key — must be identical across every process running
# this script (app, enrichment-worker, scheduler) so they all contend for the
# SAME lock. Two-int (classid, objid) form for readability; the values carry
# no meaning beyond "unique to AVAIL migrations" and must never collide with
# another advisory lock elsewhere in the codebase.
_LOCK_CLASSID = 87_412_301
_LOCK_OBJID = 1


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL is not set — cannot acquire the migration lock.", file=sys.stderr)
        return 1

    try:
        conn = psycopg2.connect(database_url)
    except Exception as exc:
        print(f"ERROR: could not connect to the database to acquire the migration lock: {exc}", file=sys.stderr)
        return 1

    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            print("Waiting for migration lock...", flush=True)
            cur.execute("SELECT pg_advisory_lock(%s, %s)", (_LOCK_CLASSID, _LOCK_OBJID))
            print("Migration lock acquired. Running alembic upgrade head...", flush=True)

        result = subprocess.run(["alembic", "upgrade", "head"])
        return result.returncode
    finally:
        # Closing the connection releases the session-level advisory lock
        # automatically — the next-in-line container's pg_advisory_lock call
        # unblocks as soon as this happens, whether we got here via a clean
        # return or an exception from alembic/subprocess above.
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
