#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
PID_FILE="${STATE_DIR}/tunnel-self-heal.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file found. Daemon is likely not running."
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$PID" ]]; then
  echo "PID file is empty."
  rm -f "$PID_FILE"
  exit 0
fi

if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stopped tunnel self-heal daemon (PID: $PID)."
else
  echo "Process $PID is not running. Cleaning stale PID file."
fi

rm -f "$PID_FILE"
