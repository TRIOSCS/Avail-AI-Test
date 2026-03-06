---
name: warn-destructive-git
enabled: true
event: bash
pattern: git\s+(push\s+--force|reset\s+--hard|checkout\s+\.|clean\s+-f|branch\s+-D)
action: block
---

**BLOCKED: Destructive git operation detected**

Force push, hard reset, and clean -f can cause irreversible data loss.
Ask the user for explicit confirmation before proceeding with destructive git operations.
