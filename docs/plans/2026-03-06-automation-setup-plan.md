# Automation Setup Plan — 2026-03-06

## Tasks

### Task 1: Add context7 MCP Server
- Run: `claude mcp add context7 -- npx -y @upstash/context7-mcp@latest`
- Verify it appears in MCP config

### Task 2: Create db-health Skill
- Create `.claude/skills/db-health/SKILL.md`
- Purpose: Validate Alembic migration chain (no branching), detect schema drift, suggest missing migrations
- User-invocable as `/db-health`
- Should run: `alembic check`, `alembic heads`, compare model metadata vs DB

### Task 3: Create connector-health Skill
- Create `.claude/skills/connector-health/SKILL.md`
- Purpose: Test all API integrations (Nexar, BrokerBin, DigiKey, Mouser, OEMSecrets, Element14, Lusha, Hunter, Apollo, Explorium, Clearbit, Gradient)
- User-invocable as `/connector-health`
- Should use the existing health monitor service at `app/services/health_monitor.py`
- Check: `GET /api/admin/api-health/dashboard` for current status

### Task 4: Create frontend-build Skill
- Create `.claude/skills/frontend-build/SKILL.md`
- Purpose: Run `cd /root/availai && npm run build`, check for Vite errors, verify dist/ output
- User-invocable as `/frontend-build`
- Should also check for broken window exports in app.js/crm.js

### Task 5: Create performance-analyzer Agent
- Create `.claude/agents/performance-analyzer.md`
- Purpose: Detect N+1 queries, slow endpoints, unbounded .all() queries, missing indexes
- Should grep for patterns: `.all()` without `.limit()`, eager loading issues, missing `selectinload`/`joinedload`
- Check for endpoints missing pagination

### Task 6: Create api-documenter Agent
- Create `.claude/agents/api-documenter.md`
- Purpose: Extract FastAPI OpenAPI schema, list all endpoints with methods/auth/params
- Should run the app briefly to get `/openapi.json` or introspect router files directly

### Task 7: Run full test suite to verify nothing broke
- Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
- Verify all tests still pass
- Check coverage hasn't dropped
