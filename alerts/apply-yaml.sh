#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VMRULES_SUBDIR="${VMRULES_SUBDIR:-vmrules}"
VMRULES_DIR="${SCRIPT_DIR}/${VMRULES_SUBDIR}"
STAGE_DURATION_SEC="${STAGE_DURATION_SEC:-$((9 * 3600))}"
EXPECTED_COUNT=500

sec_to_ru() {
  awk -v s="$1" 'BEGIN {
    if (s >= 3600) {
      h = int(s / 3600)
      rest = s - h * 3600
      m = int(rest / 60)
      if (m == 0) printf "%d ч", h
      else printf "%d ч %d мин", h, m
    } else if (s >= 60) {
      m = int(s / 60)
      sec = int(s - m * 60 + 0.5)
      if (sec == 60) { m++; sec = 0 }
      printf "%d мин %d с", m, sec
    } else {
      printf "%.0f с", s
    }
  }'
}

if [ ! -d "$VMRULES_DIR" ]; then
  echo "Каталог не найден: $VMRULES_DIR" >&2
  exit 1
fi

echo "Поиск YAML файлов в ${VMRULES_DIR}..."
mapfile -t files < <(find "$VMRULES_DIR" -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V)
total=${#files[@]}

if [ "$total" -eq 0 ]; then
  echo "YAML файлы не найдены." >&2
  exit 1
fi

if [ "$total" -ne "$EXPECTED_COUNT" ]; then
  echo "Ожидалось ${EXPECTED_COUNT} файлов, найдено ${total}." >&2
  exit 1
fi

sleep_interval=$(awk -v d="$STAGE_DURATION_SEC" -v c="$total" 'BEGIN {
  printf "%.4f", d / (c - 1)
}')

stage_ru=$(sec_to_ru "$STAGE_DURATION_SEC")
sleep_ru=$(sec_to_ru "$sleep_interval")
echo "Найдено ${total} файлов. Общее время пауз: ~${stage_ru}, пауза между apply: ~${sleep_ru}."

for ((i = 0; i < total; i++)); do
  file="${files[i]}"
  index=$((i + 1))

  kubectl apply -f "$file"
  echo "  ✓ ${index}/${total}: ${file}"

  if [ "$index" -lt "$total" ]; then
    sleep "$sleep_interval"
  fi
done

echo "Готово! Применено ${total} VMRule."
