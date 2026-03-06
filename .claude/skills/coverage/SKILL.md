---
name: coverage
description: Run full test suite with coverage report and flag gaps below 100%
disable-model-invocation: true
---

Run the coverage command:

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

Report:
- Total test count and pass/fail
- Overall coverage percentage
- Any files below 100% coverage with their specific uncovered line numbers
- If any tests failed, show the failure summary
