#!/usr/bin/env bash
set -euo pipefail

status=0

echo "[secret-scan] checking tracked env/config secret files..."
if git ls-files | rg -n '(^|/)(\.env(\..*)?|config\.env(\..*)?)$' | rg -v '\.example$'; then
  echo "[secret-scan] ERROR: tracked env/config file detected (non-template)."
  status=1
fi

echo "[secret-scan] checking for Telegram bot token signatures..."
if git grep -nE '[0-9]{8,10}:[A-Za-z0-9_-]{35}' -- ':!*.example' ':!**/*.example'; then
  echo "[secret-scan] ERROR: token-like Telegram credential detected."
  status=1
fi

echo "[secret-scan] checking for hardcoded URL credentials..."
if git grep -nE '://[^/@\s]+:[^@\s]+@' -- ':!*.example' ':!**/*.example'; then
  echo "[secret-scan] ERROR: URL-embedded credentials detected."
  status=1
fi

exit "$status"
