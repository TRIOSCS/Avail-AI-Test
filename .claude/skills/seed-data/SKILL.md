---
name: seed-data
description: Run the test data seed script to populate all transaction types in every status
disable-model-invocation: true
---

Run the seed script inside the Docker container and report results:

```bash
docker compose exec -T app python scripts/seed_test_data.py
```

The script is idempotent — safe to re-run without creating duplicates.

After running, report the final record counts from the output.
