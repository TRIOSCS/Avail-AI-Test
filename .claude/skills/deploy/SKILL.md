---
name: deploy
description: Commit, push, rebuild Docker containers, and verify healthy startup
disable-model-invocation: true
---

Follow these steps exactly:

1. Run `git status` to see what's changed
2. If there are unstaged changes, stage them and commit (ask for commit message if not provided as an argument)
3. Push to origin: `git push`
4. Rebuild and restart: `cd /root/availai && docker compose up -d --build`
5. Wait 10 seconds, then check logs: `docker compose logs --tail=30 app`
6. Check for errors in logs — report any startup failures
7. Hit the health endpoint: `curl -s http://localhost:8000/api/health | python3 -m json.tool`
8. Report: deployment status, version from health endpoint, any warnings
