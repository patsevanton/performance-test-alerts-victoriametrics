#!/bin/bash
# Мониторинг во время apply-yaml-batch-*.sh: логи VictoriaLogs, метрики VictoriaMetrics, OOM vmalert.
# При первом срабатывании печатает текст ошибки в stdout, пишет момент и детали в RESULT_FILE и завершается.
#
# Анализ логов (VictoriaLogs): типичные срабатывания — i(error)/fatal/panic, _error, level:error,
# HTTP 5xx/422 в тексте, «error occured during search», превышение search.max*, OOM/OOMKilled в сообщении.
# Переопределите VL_LOGSQL_QUERY при необходимости сузить/расширить фильтр.

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VL_URL="${VL_URL:-https://victorialogs.apatsev.org.ru}"
VM_URL="${VM_URL:-https://vmselect.apatsev.org.ru}"
RESULT_FILE="${RESULT_FILE:-${_SCRIPT_DIR}/first-error-batch.txt}"
INTERVAL="${INTERVAL:-30}"
VMALERT_NAMESPACE="${VMALERT_NAMESPACE:-vmks}"
CURL_OPTS="${CURL_OPTS:--s --max-time 15}"
VL_LOG_LIMIT="${VL_LOG_LIMIT:-20}"
VL_WINDOW_MIN="${VL_WINDOW_MIN:-2}"

# Дефолтный LogsQL: широкий OR по признакам ошибок (окно времени задаётся start/end в API).
_DEFAULT_VL_LOGSQL='(_error OR i(error) OR fatal OR panic OR failed OR exception OR unreachable OR critical OR level:error OR log.level:(error OR fatal OR panic OR critical) OR "level=error" OR 503 OR 502 OR 504 OR 422 OR timeout OR OOM OR OOMKilled)'
VL_LOGSQL_QUERY="${VL_LOGSQL_QUERY:-${_DEFAULT_VL_LOGSQL}}"

START_TS=$(date -Iseconds)

urlencode() {
  jq -n --arg q "$1" '$q|@uri'
}

promql_query() {
  local q="$1" eq
  eq=$(urlencode "$q")
  curl -sf $CURL_OPTS "${VM_URL}/select/0/prometheus/api/v1/query?query=${eq}" 2>/dev/null
}

metric_value_gt_zero() {
  local json="$1"
  echo "$json" | jq -e '(.data.result[0].value[1] | tonumber) > 0' >/dev/null 2>&1
}

metric_value_line() {
  local label="$1" json="$2" v
  v=$(echo "$json" | jq -r '.data.result[0].value[1] // "0"')
  echo "${label}=${v}"
}

check_oom_vmalert() {
  local out
  out=$(kubectl get pods -n "$VMALERT_NAMESPACE" -l app.kubernetes.io/name=vmalert -o json 2>/dev/null) || return 1
  echo "$out" | jq -e '
    .items[]?.status.containerStatuses[]? |
    select(.lastState.terminated.reason == "OOMKilled") |
    .lastState.terminated.reason
  ' &>/dev/null
}

query_victorialogs() {
  local start end eq
  start=$(date -u -d "${VL_WINDOW_MIN} minutes ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-${VL_WINDOW_MIN}M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  eq=$(urlencode "$VL_LOGSQL_QUERY")
  curl -sf $CURL_OPTS \
    "${VL_URL}/select/logsql/query?query=${eq}&start=${start}&end=${end}&limit=${VL_LOG_LIMIT}" \
    2>/dev/null
}

# NDJSON от /select/logsql/query: по строке на запись.
format_vl_ndjson() {
  local raw="$1" line
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    echo "$line" | jq -r '
      select((._msg | type) == "string" and (._msg | length) > 0)
      | "[\(.kubernetes.pod_namespace // "?")/\(.kubernetes.pod_name // "?")/\(.kubernetes.container_name // "?")] \(.["_msg"])"' 2>/dev/null || true
  done <<< "$raw"
}

query_vmselect_errors() {
  promql_query 'sum(vmalert_execution_errors_total) or vector(0)'
}

query_vmselect_limit_reached() {
  promql_query 'sum(increase(vm_concurrent_select_limit_reached_total[1m])) or vector(0)'
}

query_vm_http_5xx() {
  promql_query 'sum(rate(vm_http_requests_total{job=~"vmselect|vmstorage|vminsert",code=~"5.."}[1m])) or vector(0)'
}

collect_metric_issues() {
  local out="" json
  json=$(query_vmselect_errors)
  if [[ -n "$json" ]] && metric_value_gt_zero "$json"; then
    out+=$(metric_value_line "vmalert_execution_errors_total" "$json")$'\n'
  fi
  json=$(query_vmselect_limit_reached)
  if [[ -n "$json" ]] && metric_value_gt_zero "$json"; then
    out+=$(metric_value_line "vm_concurrent_select_limit_reached_total_1m_increase" "$json")$'\n'
  fi
  json=$(query_vm_http_5xx)
  if [[ -n "$json" ]] && metric_value_gt_zero "$json"; then
    out+=$(metric_value_line "vm_http_requests_5xx_rate_1m_vmselect_vmstorage_vminsert" "$json")$'\n'
  fi
  [[ -n "$out" ]] && printf '%s' "$out" && return 0
  return 1
}

write_result() {
  local reason=$1
  local when=$2
  local detail=$3
  {
    echo "# Первое срабатывание во время apply-yaml-batch"
    echo "first_error_reason=${reason}"
    echo "first_error_utc=${when}"
    echo "stage_start_utc=${START_TS}"
    echo "victorialogs=${VL_URL}"
    echo "vmselect=${VM_URL}"
    if [[ -n "${detail}" ]]; then
      echo ""
      echo "--- detail ---"
      printf '%s\n' "${detail}" | head -c 65536
      echo ""
    fi
  } > "$RESULT_FILE"
}

echo "Мониторинг: VL=$VL_URL VM=$VM_URL ns=$VMALERT_NAMESPACE интервал ${INTERVAL}s (OOM + логи LogsQL + метрики). Старт: $START_TS"
echo "Запись при первой проблеме: $RESULT_FILE"
echo ""

while true; do
  when=$(date -Iseconds)
  if check_oom_vmalert; then
    msg="Pod vmalert: lastState OOMKilled (namespace ${VMALERT_NAMESPACE})"
    echo "[$when] ${msg}"
    write_result OOMKilled "$when" "$msg"
    exit 0
  fi

  vl_raw=""
  if vl_raw=$(query_victorialogs); then
    vl_msgs=$(format_vl_ndjson "$vl_raw")
    if [[ -n "$vl_msgs" ]]; then
      echo "[$when] VictoriaLogs (совпадения за ~${VL_WINDOW_MIN} мин, до ${VL_LOG_LIMIT} строк):"
      echo "$vl_msgs"
      write_result victorialogs "$when" "$vl_msgs"
      exit 0
    fi
  fi

  if metric_issues=$(collect_metric_issues); then
    echo "[$when] Метрики VictoriaMetrics (ненулевые):"
    echo -n "$metric_issues"
    write_result vm_metrics "$when" "$metric_issues"
    exit 0
  fi

  echo "[$when] OK"
  sleep "$INTERVAL"
done
