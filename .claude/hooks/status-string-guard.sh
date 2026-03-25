#!/bin/bash
# Warns when raw status strings are used instead of StrEnum constants
for f in $CLAUDE_FILE_PATHS; do
  if echo "$f" | grep -qE '\.py$'; then
    if grep -qE '== "(active|draft|sourcing|offers|quoting|quoted|won|lost|archived|cancelled|pending|completed|open|sent|expired|rejected|approved|bidding|closed|withdrawn|available|awarded|new|reviewed|parsed)"' "$f" 2>/dev/null; then
      echo "WARNING: Raw status string detected in $f — use StrEnum from app/constants.py" >&2
    fi
  fi
done
true
