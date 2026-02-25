#!/bin/bash
# Этап 2: файлы 401–1222, интервал 30 с

list=$(find vmrules -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V | sed -n '401,1222p')
echo "$list" | while read -r f; do
  kubectl apply -f "$f"
  echo "  Этап 2"
  sleep 30
done
echo "Этап 2 готов."
