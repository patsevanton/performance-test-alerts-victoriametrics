#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VMRULES_DIR="${SCRIPT_DIR}/vmrules"
APPLY_TIMEOUT=70

# 500 files total, 100 alerts per file -> 45_000..50_000 alerts = files 451..500.
START_INDEX=451
END_INDEX=500
EXPECTED_TOTAL=500

if [ ! -d "$VMRULES_DIR" ]; then
  echo "Каталог не найден: $VMRULES_DIR" >&2
  exit 1
fi

mapfile -t files < <(find "$VMRULES_DIR" -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V)
total=${#files[@]}

if [ "$total" -ne "$EXPECTED_TOTAL" ]; then
  echo "Ожидалось ${EXPECTED_TOTAL} файлов, найдено: ${total}" >&2
  exit 1
fi

if [ "$START_INDEX" -lt 1 ] || [ "$END_INDEX" -gt "$total" ] || [ "$START_INDEX" -gt "$END_INDEX" ]; then
  echo "Некорректный диапазон ${START_INDEX}-${END_INDEX} для ${total} файлов." >&2
  exit 1
fi

slice_count=$((END_INDEX - START_INDEX + 1))
echo "Этап 4: применяю файлы ${START_INDEX}-${END_INDEX} (${slice_count} шт), пауза ${APPLY_TIMEOUT}s."

for ((i = START_INDEX - 1; i <= END_INDEX - 1; i++)); do
  index=$((i + 1))
  file="${files[i]}"
  kubectl apply -f "$file"
  echo "  ✓ ${index}/${total}: ${file}"

  if [ "$index" -lt "$END_INDEX" ]; then
    sleep "$APPLY_TIMEOUT"
  fi
done

echo "Готово: этап 4 (~45000 -> ~50000 ALERTS) завершен."
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VMRULES_DIR="${SCRIPT_DIR}/vmrules"
APPLY_TIMEOUT=70

# 500 files total, 100 alerts per file -> 45_000..50_000 alerts = files 451..500.
START_INDEX=451
END_INDEX=500
EXPECTED_TOTAL=500

if [ ! -d "$VMRULES_DIR" ]; then
  echo "Каталог не найден: $VMRULES_DIR" >&2
  exit 1
fi

mapfile -t files < <(find "$VMRULES_DIR" -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V)
total=${#files[@]}

if [ "$total" -ne "$EXPECTED_TOTAL" ]; then
  echo "Ожидалось ${EXPECTED_TOTAL} файлов, найдено: ${total}" >&2
  exit 1
fi

if [ "$START_INDEX" -lt 1 ] || [ "$END_INDEX" -gt "$total" ] || [ "$START_INDEX" -gt "$END_INDEX" ]; then
  echo "Некорректный диапазон ${START_INDEX}-${END_INDEX} для ${total} файлов." >&2
  exit 1
fi

slice_count=$((END_INDEX - START_INDEX + 1))
echo "Этап 2: применяю файлы ${START_INDEX}-${END_INDEX} (${slice_count} шт), пауза ${APPLY_TIMEOUT}s."

for ((i = START_INDEX - 1; i <= END_INDEX - 1; i++)); do
  index=$((i + 1))
  file="${files[i]}"
  kubectl apply -f "$file"
  echo "  ✓ ${index}/${total}: ${file}"

  if [ "$index" -lt "$END_INDEX" ]; then
    sleep "$APPLY_TIMEOUT"
  fi
done

echo "Готово: этап 2 (~45000 -> ~50000 ALERTS) завершен."
