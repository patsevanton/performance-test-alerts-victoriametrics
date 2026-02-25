#!/bin/bash
# Этап 3: файлы 1223–10000, интервал 90 с

list=$(find vmrules -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V | sed -n '1223,10000p')
echo "$list" | while read -r f; do
  kubectl apply -f "$f"
  echo "  Этап 3"
  sleep 90
done
echo "Этап 3 готов."
