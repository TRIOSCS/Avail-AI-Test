---
name: protect-env-secrets
enabled: true
event: file
conditions:
  - field: file_path
    operator: regex_match
    pattern: \.env$
action: block
---

**BLOCKED: Direct .env file edit**

Never edit .env files directly — they contain production secrets.
Use `.env.example` for template changes, and edit `.env` manually via the shell if needed.
