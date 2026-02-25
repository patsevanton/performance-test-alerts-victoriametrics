#!/bin/bash
# Этап 1: файлы 1–400, интервал 5 с

list=$(find vmrules -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V | sed -n '1,400p')
echo "$list" | while read -r f; do
  kubectl apply -f "$f"
  echo "  Этап 1"
  sleep 5
done
echo "Этап 1 готов."
