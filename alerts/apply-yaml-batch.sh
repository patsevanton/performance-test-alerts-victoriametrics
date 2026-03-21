#!/usr/bin/env bash
# VMRule с глобальными индексами 1-500 (sort -V). Каждый запуск - с начала диапазона.
# Паузы между apply рассчитываются так, чтобы суммарный sleep был ~STAGE_DURATION_SEC.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VMRULES_SUBDIR="${VMRULES_SUBDIR:-vmrules}"
STAGE_DURATION_SEC="${STAGE_DURATION_SEC:-$((9 * 3600))}"
APPLY_BATCH_ID="${APPLY_BATCH_ID:-01}"
APPLY_INDEX_START="${APPLY_INDEX_START:-1}"
APPLY_INDEX_END="${APPLY_INDEX_END:-500}"

# Секунды -> краткая русская строка (9 ч, 1 мин 5 с, 45 с).
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

run_batch() {
  local vmrules="${SCRIPT_DIR}/${VMRULES_SUBDIR}"
  local batch_id="$APPLY_BATCH_ID"
  local istart="$APPLY_INDEX_START"
  local iend="$APPLY_INDEX_END"

  if [ ! -d "$vmrules" ]; then
    echo "Каталог не найден: $vmrules" >&2
    return 1
  fi
  if [ "$istart" -lt 1 ] || [ "$iend" -lt "$istart" ]; then
    echo "Неверный диапазон индексов: ${istart}..${iend}" >&2
    return 1
  fi

  mapfile -t all_files < <(find "$vmrules" -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V)
  local total=${#all_files[@]}
  if [ "$total" -eq 0 ]; then
    echo "В ${vmrules} нет YAML." >&2
    return 1
  fi
  if [ "$iend" -gt "$total" ]; then
    echo "APPLY_INDEX_END=${iend} больше числа файлов (${total}). Уменьшите конец диапазона или сгенерируйте VMRule." >&2
    return 1
  fi

  local start_idx=$((istart - 1))
  local end_idx=$((iend - 1))
  local bcount=$((end_idx - start_idx + 1))

  local -a slice=()
  local j
  for ((j = start_idx; j <= end_idx; j++)); do
    slice+=("${all_files[j]}")
  done

  local sleep_interval
  sleep_interval=$(awk -v d="$STAGE_DURATION_SEC" -v c="$bcount" 'BEGIN {
    if (c < 2) print 0
    else printf "%.4f", d / (c - 1)
  }')

  local stage_ru sleep_ru
  stage_ru=$(sec_to_ru "$STAGE_DURATION_SEC")
  sleep_ru=$(sec_to_ru "$sleep_interval")
  echo "Батч ${batch_id}: глобальные индексы ${istart}-${iend} (${bcount} файлов). Паузы между apply суммарно ~${stage_ru}; после каждого файла (кроме последнего) ~${sleep_ru}."
  echo "Ориентир по времени всего скрипта: ~${stage_ru} пауз + ${bcount}x kubectl apply (зависит от API)."

  local i
  for ((i = 0; i < bcount; i++)); do
    local f="${slice[i]}"
    kubectl apply -f "$f"
    echo "  Батч ${batch_id}: $((i + 1)) / ${bcount} (глобально $((istart + i)) / ${iend})"
    if [ "$i" -lt $((bcount - 1)) ]; then
      sleep "$sleep_interval"
    fi
  done

  echo "Батч ${batch_id} готов."
}

run_batch
