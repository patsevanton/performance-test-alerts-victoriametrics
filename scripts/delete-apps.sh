#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAMESPACE="${NAMESPACE:-load-test}"
NAMES_FILE="${NAMES_FILE:-$SCRIPT_DIR/app-names.txt}"
PARALLEL="${PARALLEL:-10}"

if [ ! -f "$NAMES_FILE" ]; then
  echo "Error: $NAMES_FILE not found."
  exit 1
fi

TOTAL=$(wc -l < "$NAMES_FILE")
echo "Deleting $TOTAL apps from namespace '$NAMESPACE' (parallelism: $PARALLEL)"

delete_app() {
  local name="$1"
  echo "[$(date +%H:%M:%S)] Deleting $name ..."
  helm uninstall "$name" --namespace "$NAMESPACE" 2>&1 | tail -1
}

export -f delete_app
export NAMESPACE

cat "$NAMES_FILE" | xargs -P "$PARALLEL" -I {} bash -c 'delete_app "$@"' _ {}

echo ""
echo "All $TOTAL apps deleted."
