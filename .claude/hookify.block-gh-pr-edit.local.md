---
name: block-gh-pr-edit
enabled: true
event: bash
pattern: gh\s+pr\s+edit
action: block
---

**BLOCKED: `gh pr edit` is broken in this environment**

It hits deprecated Projects-classic GraphQL endpoints and **silently fails** (PR body/title unchanged, exit 0 sometimes).

**Use instead:**
```bash
gh api -X PATCH /repos/TRIOSCS/Avail-AI-Test/pulls/<N> --input - <<'JSON'
{"body": "...new body..."}
JSON
```
(`title` works the same way.)
