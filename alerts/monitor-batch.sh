#!/bin/bash
# Мониторинг во время apply-yaml-batch-*.sh: логи VictoriaLogs, метрики vmselect, OOM vmalert.
# При первом срабатывании пишет момент в RESULT_FILE и завершается.

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VL_URL="${VL_URL:-https://victorialogs.apatsev.org.ru}"
VM_URL="${VM_URL:-https://vmselect.apatsev.org.ru}"
RESULT_FILE="${RESULT_FILE:-${_SCRIPT_DIR}/first-error-batch.txt}"
INTERVAL="${INTERVAL:-30}"
VMALERT_NAMESPACE="${VMALERT_NAMESPACE:-vmks}"
CURL_OPTS="${CURL_OPTS:--s --max-time 15}"

check_oom_vmalert() {
  local out
  out=$(kubectl get pods -n "$VMALERT_NAMESPACE" -l app.kubernetes.io/name=vmalert -o json 2>/dev/null) || return 1
  echo "$out" | jq -e '
    .items[]?.status.containerStatuses[]? |
    select(.lastState.terminated.reason == "OOMKilled") |
    .lastState.terminated.reason
  ' &>/dev/null
}

START_TS=$(date -Iseconds)

query_victorialogs() {
  local start end
  start=$(date -u -d '2 minutes ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-2M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  curl -f $CURL_OPTS \
    "${VL_URL}/select/logsql/query?query=error%20or%20_error%20or%20level%3Derror&start=${start}&end=${end}&limit=5" \
    2>/dev/null | head -c 4096
}

query_vmselect_errors() {
  local q='sum(vmalert_execution_errors_total) or vector(0)'
  curl -f $CURL_OPTS \
    "${VM_URL}/select/0/prometheus/api/v1/query?query=${q}" 2>/dev/null
}

query_vmselect_limit_reached() {
  local q='sum(increase(vm_concurrent_select_limit_reached_total[1m])) or vector(0)'
  curl -f $CURL_OPTS \
    "${VM_URL}/select/0/prometheus/api/v1/query?query=${q}" 2>/dev/null
}

query_vmselect_5xx() {
  local q='sum(rate(vm_http_requests_total{job=~"vmselect",code=~"5.."}[1m])) or vector(0)'
  curl -f $CURL_OPTS \
    "${VM_URL}/select/0/prometheus/api/v1/query?query=${q}" 2>/dev/null
}

has_errors() {
  local vl vl_ok vm_err vm_lim vm_5xx val
  vl=$(query_victorialogs)
  vl_ok=$?
  if [[ $vl_ok -eq 0 && -n "$vl" ]]; then
    if echo "$vl" | grep -qE '"message"|"msg"|"level"|error|Error|ERROR'; then
      return 0
    fi
  fi

  vm_err=$(query_vmselect_errors)
  if [[ $? -eq 0 && -n "$vm_err" ]]; then
    val=$(echo "$vm_err" | jq -r '.data.result[0].value[1] // "0"')
    if [[ -n "$val" && "$val" != "0" && "$val" != "null" ]]; then
      return 0
    fi
  fi

  vm_lim=$(query_vmselect_limit_reached)
  if [[ $? -eq 0 && -n "$vm_lim" ]]; then
    val=$(echo "$vm_lim" | jq -r '.data.result[0].value[1] // "0"')
    if [[ -n "$val" && "$val" != "0" && "$val" != "null" ]]; then
      return 0
    fi
  fi

  vm_5xx=$(query_vmselect_5xx)
  if [[ $? -eq 0 && -n "$vm_5xx" ]]; then
    val=$(echo "$vm_5xx" | jq -r '.data.result[0].value[1] // "0"')
    if [[ -n "$val" && "$val" != "0" && "$val" != "null" ]]; then
      return 0
    fi
  fi

  return 1
}

write_result() {
  local reason=$1
  local when=$2
  {
    echo "# Первое срабатывание во время apply-yaml-batch"
    echo "first_error_reason=${reason}"
    echo "first_error_utc=${when}"
    echo "stage_start_utc=${START_TS}"
    echo "victorialogs=${VL_URL}"
    echo "vmselect=${VM_URL}"
  } > "$RESULT_FILE"
}

echo "Мониторинг: VL=$VL_URL VM=$VM_URL ns=$VMALERT_NAMESPACE интервал ${INTERVAL}s (OOM + логи/метрики). Старт: $START_TS"
echo "Запись при первой проблеме: $RESULT_FILE"
echo ""

while true; do
  when=$(date -Iseconds)
  if check_oom_vmalert; then
    echo "[$when] OOMKilled (vmalert). Записываю момент."
    write_result OOMKilled "$when"
    exit 0
  fi
  if has_errors; then
    echo "[$when] Ошибки (логи/метрики). Записываю момент."
    write_result metrics_or_logs "$when"
    exit 0
  fi
  echo "[$when] OK"
  sleep "$INTERVAL"
done
