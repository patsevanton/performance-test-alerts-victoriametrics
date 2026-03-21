#!/usr/bin/env bash
# Общая логика для apply-yaml-batch-*.sh:
# — В vmrules/ полный список YAML (sort -V); батч задаётся 1-based индексами APPLY_INDEX_START..APPLY_INDEX_END.
# — Состояние на диске не пишется: каждый запуск проходит весь диапазон батча с начала. Ошибка → исправить, снова запустить тот же скрипт.
# — Паузы: суммарный sleep ≈ STAGE_DURATION_SEC (по умолчанию 9 ч) на все apply в батче.

set -euo pipefail

VMRULES_SUBDIR="${VMRULES_SUBDIR:-vmrules}"
STAGE_DURATION_SEC="${STAGE_DURATION_SEC:-$((9 * 3600))}"

grafana_annotation() {
  local tag=$1
  local text=$2
  [ -z "${GRAFANA_TOKEN:-}" ] && return 0
  local ms
  ms=$(($(date +%s) * 1000))
  local code
  code=$(curl ${CURL_OPTS:-} -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${GRAFANA_TOKEN}" -H "Content-Type: application/json" \
    "${GRAFANA_URL}/api/annotations" \
    -d "{\"time\":${ms},\"text\":\"${text}\",\"tags\":[\"${tag}\"]}")
  [ "$code" = "200" ]
}

# run_batch <каталог alerts>
# Ожидает: APPLY_BATCH_ID, APPLY_INDEX_START, APPLY_INDEX_END (включительно, с 1).
run_batch() {
  local base_dir=$1
  local vmrules="${base_dir}/${VMRULES_SUBDIR}"
  local batch_id="${APPLY_BATCH_ID:?Задайте APPLY_BATCH_ID}"
  local istart="${APPLY_INDEX_START:?Задайте APPLY_INDEX_START}"
  local iend="${APPLY_INDEX_END:?Задайте APPLY_INDEX_END}"

  GRAFANA_URL="${GRAFANA_URL:-http://grafana.apatsev.org.ru}"
  [ -z "${GRAFANA_TOKEN:-}" ] && read -r -p "GRAFANA_TOKEN: " GRAFANA_TOKEN

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

  echo "Батч ${batch_id}: индексы ${istart}–${iend} (${bcount} файлов), паузы ~${STAGE_DURATION_SEC} с суммарно, шаг sleep ≈ ${sleep_interval} с."

  local gtag="apply-yaml-batch${batch_id}"
  grafana_annotation "$gtag" "apply batch ${batch_id} (${istart}-${iend}) started" && echo "Аннотация Grafana: старт батча ${batch_id}."

  local i
  for ((i = 0; i < bcount; i++)); do
    local f="${slice[i]}"
    kubectl apply -f "$f"
    echo "  Батч ${batch_id}: $((i + 1)) / ${bcount} (глобально $((istart + i)) / ${iend})"
    if [ "$i" -lt $((bcount - 1)) ]; then
      sleep "$sleep_interval"
    fi
  done

  grafana_annotation "$gtag" "apply batch ${batch_id} (${istart}-${iend}) finished" && echo "Аннотация Grafana: финиш батча ${batch_id}."
  echo "Батч ${batch_id} готов."
}
