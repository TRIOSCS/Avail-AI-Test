---
name: warn-raw-ddl
enabled: true
event: bash
pattern: (ALTER\s+TABLE|DROP\s+TABLE|CREATE\s+TABLE|ADD\s+COLUMN|DROP\s+COLUMN|CREATE\s+INDEX|ADD\s+CONSTRAINT)
action: block
---

**BLOCKED: Raw DDL detected outside Alembic**

ALL schema changes MUST go through Alembic migrations. Never run raw DDL.

Correct workflow:
1. Change model in `app/models/`
2. `alembic revision --autogenerate -m "description"`
3. Review generated migration
4. `alembic upgrade head`
