---
name: deploy
description: Deploy via ./deploy.sh (fresh BUILD_COMMIT, health check, build-tag verify) and report status
disable-model-invocation: true
---

Follow these steps exactly:

1. Run `git status` to see what's changed
2. Deploy with `./deploy.sh` — NEVER bare `docker compose up -d --build` (it lacks the
   unique `BUILD_COMMIT` build-arg and ships stale templates/static — see CLAUDE.md):
   - From `main` (normal case): `cd /root/availai && ./deploy.sh "<commit message>"` —
     syncs main, commits tracked changes (asks nothing; pass the message as the argument),
     pushes, rebuilds app + enrichment-worker with a fresh `BUILD_COMMIT`, waits for the
     health check, verifies the deployed build tag on both containers, checks new Tailwind
     classes exist in the built CSS, and restarts the host `nc`/`ics` worker units
   - From a branch (no commit/push): `cd /root/availai && ./deploy.sh --no-commit`
3. If deploy.sh fails, report the failing step verbatim — do not fall back to raw
   `docker compose` commands
4. Confirm health independently (the app port is not published to the host, so curl from
   inside the container):
   `docker compose exec -T app curl -s http://localhost:8000/health | python3 -m json.tool`
   — expect `status/db/redis: ok` and a `build_commit` matching the just-deployed sha
5. Check logs for startup errors: `docker compose logs --tail=30 app`
6. Report: deployment status, version + build tag from the health endpoint, any warnings
