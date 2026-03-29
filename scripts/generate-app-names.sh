#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COUNT="${1:-300}"
OUTPUT_FILE="$SCRIPT_DIR/app-names.txt"

> "$OUTPUT_FILE"

for i in $(seq 1 "$COUNT"); do
  echo "app-$i" >> "$OUTPUT_FILE"
done

echo "Generated $COUNT app names in $OUTPUT_FILE"
