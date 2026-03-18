#!/bin/bash
# Simple cron wrapper for the gate freshness checker.
# Logs to syslog on failure so external alerting can pick it up.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN=${PYTHON_BIN:-python3}
REDIS_URL=${VALKEY_URL:-redis://localhost:6379/0}
INSTRUMENTS=${HOTBAND_PAIRS:-EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CHF,USD_CAD,NZD_USD}

OUTPUT="$(cd "$ROOT_DIR" && PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" -m scripts.tools.health_check_service --redis "$REDIS_URL" --instruments "$INSTRUMENTS" 2>&1)"
STATUS=$?

if [ $STATUS -ne 0 ]; then
  logger -t sep-gate-freshness "Gate freshness check failed (status $STATUS): $OUTPUT"
  echo "$OUTPUT" >&2
  exit $STATUS
fi

echo "$OUTPUT"
exit 0
