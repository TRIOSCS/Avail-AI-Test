#!/usr/bin/env bash
# requirements-guard.sh — PreToolUse(Edit) hook.
# Blocks edits to requirements.txt / requirements-dev.txt — those are pip-compile-generated
# lockfiles. Edit the matching requirements*.in source and recompile (pip-compile) instead.
# Reads the edited paths from $CLAUDE_FILE_PATHS; exit 2 = block.
# Called by: /root/availai/.claude/settings.local.json PreToolUse Edit matcher.
for f in $CLAUDE_FILE_PATHS; do
  if echo "$f" | grep -qE 'requirements(-dev)?\.txt$'; then
    echo 'BLOCKED: requirements*.txt is a pip-compile lockfile — edit the matching .in source and recompile (pip-compile); never hand-edit the .txt.' >&2
    exit 2
  fi
done
exit 0
