---
name: new-migration
description: Create a new Alembic migration with proper naming and rollback
disable-model-invocation: true
---

1. Ask what the migration does if not provided as an argument
2. Generate with: `cd /root/availai && alembic revision --autogenerate -m "{description}"`
3. Open the generated file and verify:
   - upgrade() has the correct operations
   - downgrade() properly reverses everything
   - No data loss in downgrade (DROP COLUMN should have a comment noting data loss)
   - Indexes are created for foreign keys
4. Run: `cd /root/availai && TESTING=1 PYTHONPATH=/root/availai alembic check` to validate migration head
5. Show the user the final migration file for review
