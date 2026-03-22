#!/usr/bin/env bash
# Постепенное применение VMRule-ресурсов через kubectl apply.
# Файлы из каталога vmrules/ применяются с равномерными паузами,
# чтобы растянуть нагрузку на весь этап (STAGE_DURATION_SEC).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VMRULES_SUBDIR="vmrules"
VMRULES_DIR="${SCRIPT_DIR}/${VMRULES_SUBDIR}"
# Общая длительность этапа (9 часов)
STAGE_DURATION_SEC=$((9 * 3600))
# Скрипт завершается с ошибкой, если число файлов отличается от ожидаемого
EXPECTED_COUNT=500

# Форматирует количество секунд в человекочитаемую строку (ч/мин/с).
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

# Пауза между apply = общее_время / (число_файлов - 1), чтобы
# последний файл применился ровно в конце этапа.
sleep_interval=$(awk -v d="$STAGE_DURATION_SEC" -v c="$total" 'BEGIN {
  printf "%.4f", d / (c - 1)
}')

stage_ru=$(sec_to_ru "$STAGE_DURATION_SEC")
sleep_ru=$(sec_to_ru "$sleep_interval")
echo "Найдено ${total} файлов. Общее время пауз: ~${stage_ru}, пауза между apply: ~${sleep_ru}."

# Основной цикл: применяем файлы по одному с паузой между ними.
# Последний файл применяется без паузы.
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
