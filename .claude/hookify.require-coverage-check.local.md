---
name: require-coverage-check
enabled: true
event: stop
pattern: .*
action: warn
---

**Before completing this task, verify:**

1. Run the full test suite: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
2. Check coverage: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
3. Coverage must remain at or above current level (target: 100%)
4. No commit should reduce coverage

If you haven't run these checks yet, do so now before claiming the work is done.
