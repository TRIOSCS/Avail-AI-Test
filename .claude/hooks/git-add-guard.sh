#!/usr/bin/env bash
# git-add-guard.sh — PreToolUse(Bash) hook.
# Blocks `git add -A`, `git add .`, and `git add --all` in this shared checkout, where
# such a blanket stage would pull in the node_modules symlink and the .superpowers/ scratch
# tree. Stage explicit paths instead. Reads the tool-call JSON on stdin; exit 2 = block.
# Called by: /root/availai/.claude/settings.local.json PreToolUse Bash matcher.
# Depends on: jq, grep.
cmd=$(jq -r '.tool_input.command // ""' 2>/dev/null)
if printf '%s' "$cmd" | grep -qE 'git[[:space:]]+add' \
   && printf '%s' "$cmd" | grep -qE '(^|[[:space:]])(-A|--all|\.)([[:space:]]|$)'; then
  echo 'BLOCKED: never `git add -A` / `.` / `--all` here (stages the node_modules symlink + .superpowers/ scratch). Stage explicit paths.' >&2
  exit 2
fi
exit 0
