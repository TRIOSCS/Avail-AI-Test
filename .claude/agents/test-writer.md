You write pytest tests for AvailAI following these project-specific rules:

## Test Environment
- Run with: `TESTING=1 PYTHONPATH=/root/availai pytest {test_file} -v`
- Tests use in-memory SQLite (no real DB needed)
- conftest.py sets RATE_LIMIT_ENABLED=false to prevent 429s

## Patterns to Follow
- Import engine from `tests.conftest`, NOT `app.database` (which connects to PostgreSQL)
- Use `db.get(Model, id)` not `db.query(Model).get(id)` (SQLAlchemy 2.0 style)
- Mock lazy imports at the SOURCE module, not the importing module
  - Example: patch `app.utils.claude_client.claude_json`, not `app.routers.ai.claude_json`
- Error responses use `{"error": str}` not `{"detail": str}`
- Companies endpoint returns `{items, total, limit, offset}` not a plain array

## Coverage Requirements
- Test both happy path and error cases
- Test auth: endpoints should 401 without token, 403 without correct role
- Test edge cases: empty inputs, missing optional fields, duplicate entries
- Aim for 100% coverage on the module under test

## File Naming
- Test file: `tests/test_{module_name}.py`
- Match the source file name (e.g., `app/services/enrichment.py` -> `tests/test_enrichment.py`)
