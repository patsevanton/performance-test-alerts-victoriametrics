#!/bin/bash
# =============================================================================
# monitor-batch.sh — «сторожевой» скрипт, работающий параллельно с apply-yaml.sh.
#
# Назначение:
#   Непрерывно опрашивает три источника данных в цикле:
#     1. kubectl   — проверяет OOMKilled у подов vmalert;
#     2. VictoriaLogs — ищет записи с признаками ошибок (LogsQL);
#     3. VictoriaMetrics (vmselect) — выполняет PromQL-запросы для
#        обнаружения ошибок выполнения правил, лимитов concurrent-select
#        и HTTP 5xx.
#
#   При **первом** обнаружении проблемы скрипт:
#     - печатает краткое сообщение в stdout;
#     - записывает подробности в файл RESULT_FILE (first-error-batch.txt);
#     - завершается с кодом 0.
#
#   Если проблем нет — печатает «OK» каждые INTERVAL секунд.
#
# Анализ логов (VictoriaLogs):
#   Типичные срабатывания — i(error)/fatal/panic, _error, level:error,
#   HTTP 5xx/422 в тексте, «error occured during search»,
#   превышение search.max*, OOM/OOMKilled в сообщении.
#   Переопределите переменную VL_LOGSQL_QUERY, если нужно
#   сузить/расширить фильтр.
#
# Использование:
#   Запускается фоновым процессом перед apply-yaml.sh; после окончания
#   apply завершается через kill. Затем внешний скрипт проверяет
#   наличие и содержимое RESULT_FILE.
# =============================================================================

# ---- Конфигурация ----

# Каталог, в котором лежит данный скрипт (для определения пути к RESULT_FILE)
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Базовые URL внешних систем
VL_URL="https://victorialogs.apatsev.org.ru"   # VictoriaLogs (поиск по логам)
VM_URL="https://vmselect.apatsev.org.ru"        # VictoriaMetrics vmselect (PromQL)

# Файл-отчёт: при первом обнаружении ошибки сюда записывается причина,
# метка времени и подробности. Внешний скрипт проверяет наличие этого файла.
RESULT_FILE="${_SCRIPT_DIR}/first-error-batch.txt"

# Интервал между итерациями основного цикла (секунды)
INTERVAL=15

# Kubernetes namespace, в котором развёрнут vmalert (для проверки OOMKilled)
VMALERT_NAMESPACE="vmks"

# Общие флаги curl: тихий режим + ограничение времени ответа
CURL_OPTS="-s --max-time 15"

# Лимит строк, запрашиваемых из VictoriaLogs за одну итерацию
VL_LOG_LIMIT=200

# Глубина «окна» поиска логов — сколько минут назад от текущего момента
VL_WINDOW_MIN=2

# LogsQL-фильтр — широкий OR по всем типичным признакам ошибок.
# Окно времени (start/end) задаётся отдельно при вызове API.
# Фильтр намеренно агрессивный: лучше получить ложное срабатывание,
# чем пропустить реальную проблему во время нагрузочного теста.
#
# Что покрывает:
#   _error         — служебное поле ошибки VictoriaLogs
#   i(error)       — слово «error» без учёта регистра
#   fatal/panic    — критические сбои приложений
#   level:error …  — структурированный уровень логирования
#   503/502/504    — HTTP-ошибки балансировщика/бекенда
#   422            — unprocessable entity (часто при невалидных правилах)
#   OOM/OOMKilled  — признаки нехватки памяти в тексте логов
VL_LOGSQL_QUERY='(_error OR i(error) OR fatal OR panic OR failed OR exception OR unreachable OR critical OR level:error OR log.level:(error OR fatal OR panic OR critical) OR "level=error" OR 503 OR 502 OR 504 OR 422 OR timeout OR OOM OR OOMKilled)'

# Запоминаем момент старта — попадёт в отчёт для расчёта «сколько работало до ошибки»
START_TS=$(date -Iseconds)

# ---- Вспомогательные функции ----

# Кодирует строку для безопасной подстановки в URL (query-параметры).
# Используется для LogsQL и PromQL запросов, которые могут содержать
# скобки, пробелы, кавычки и другие спецсимволы.
urlencode() {
  jq -n --arg q "$1" '$q|@uri'
}

# Выполняет instant-запрос PromQL к VictoriaMetrics через API vmselect.
# Аргумент — произвольное PromQL-выражение (будет URL-закодировано).
# Возвращает JSON-ответ Prometheus API v1 в stdout.
# При ошибке сети/таймауте возвращает пустую строку (curl -f).
promql_query() {
  local q="$1" eq
  eq=$(urlencode "$q")
  curl -sf $CURL_OPTS "${VM_URL}/select/0/prometheus/api/v1/query?query=${eq}" 2>/dev/null
}

# Проверяет, что числовое значение первого элемента PromQL-ответа > 0.
# Используется как условие: «есть ли ненулевая метрика — значит есть проблема».
# Возвращает 0 (true) если value > 0, иначе 1 (false).
metric_value_gt_zero() {
  local json="$1"
  echo "$json" | jq -e '(.data.result[0].value[1] | tonumber) > 0' >/dev/null 2>&1
}

# Извлекает числовое значение из PromQL-ответа и форматирует строку
# вида «label=value» для записи в отчёт (RESULT_FILE).
# Если значение отсутствует в JSON, подставляет "0".
metric_value_line() {
  local label="$1" json="$2" v
  v=$(echo "$json" | jq -r '.data.result[0].value[1] // "0"')
  echo "${label}=${v}"
}

# Проверяет, не был ли какой-либо контейнер пода vmalert убит ядром (OOMKilled).
# Запрашивает через kubectl все поды с лейблом app.kubernetes.io/name=vmalert
# в заданном namespace и ищет в containerStatuses.lastState.terminated.reason == "OOMKilled".
# Возвращает 0 (true) если OOM обнаружен, 1 (false) — если всё в порядке.
# При ошибке доступа к API Kubernetes также возвращает 1 (не блокирует цикл).
check_oom_vmalert() {
  local out
  out=$(kubectl get pods -n "$VMALERT_NAMESPACE" -l app.kubernetes.io/name=vmalert -o json 2>/dev/null) || return 1
  echo "$out" | jq -e '
    .items[]?.status.containerStatuses[]? |
    select(.lastState.terminated.reason == "OOMKilled") |
    .lastState.terminated.reason
  ' &>/dev/null
}

# Запрашивает логи из VictoriaLogs за скользящее окно (VL_WINDOW_MIN минут назад → сейчас).
# Использует API /select/logsql/query с параметрами start, end, limit.
#
# Кроссплатформенность:
#   GNU date  → date -d "N minutes ago" ...
#   BSD date  → date -v-NM ...
# Первый вариант пробуется первым; при ошибке — fallback на BSD.
#
# Возвращает NDJSON (по одной JSON-записи на строку) в stdout.
query_victorialogs() {
  local start end eq
  start=$(date -u -d "${VL_WINDOW_MIN} minutes ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-${VL_WINDOW_MIN}M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  eq=$(urlencode "$VL_LOGSQL_QUERY")
  curl -sf $CURL_OPTS \
    "${VL_URL}/select/logsql/query?query=${eq}&start=${start}&end=${end}&limit=${VL_LOG_LIMIT}" \
    2>/dev/null
}

# Парсит NDJSON-вывод VictoriaLogs и форматирует каждую запись
# в читаемую строку вида:
#   [namespace/pod/container] текст сообщения
#
# Пропускает записи без поля _msg (или с пустым _msg).
# Поля kubernetes.pod_namespace, pod_name, container_name подставляются
# из Kubernetes-метаданных; при отсутствии — "?".
format_vl_ndjson() {
  local raw="$1" line
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    echo "$line" | jq -r '
      select((._msg | type) == "string" and (._msg | length) > 0)
      | "[\(.kubernetes.pod_namespace // "?")/\(.kubernetes.pod_name // "?")/\(.kubernetes.container_name // "?")] \(.["_msg"])"' 2>/dev/null || true
  done <<< "$raw"
}

# ---- PromQL-запросы к метрикам VictoriaMetrics ----
# Каждая функция возвращает JSON Prometheus API v1 в stdout.
# «or vector(0)» гарантирует, что запрос всегда возвращает хотя бы одно значение,
# чтобы metric_value_gt_zero мог корректно сравнить с нулём.

# Общее число ошибок выполнения alerting/recording-правил в vmalert.
# Растущий счётчик; если значение > 0 — были ошибки за всё время работы.
query_vmselect_errors() {
  promql_query 'sum(vmalert_execution_errors_total) or vector(0)'
}

# Число раз, когда vmselect отклонил запросы из-за исчерпания
# лимита одновременных select-запросов (за последнюю 1 мин).
query_vmselect_limit_reached() {
  promql_query 'sum(increase(vm_concurrent_select_limit_reached_total[1m])) or vector(0)'
}

# Скорость (rate) HTTP 5xx ответов по всем компонентам кластера
# (vmselect, vmstorage, vminsert) за последнюю минуту.
query_vm_http_5xx() {
  promql_query 'sum(rate(vm_http_requests_total{job=~"vmselect|vmstorage|vminsert",code=~"5.."}[1m])) or vector(0)'
}

# Выполняет все PromQL-проверки последовательно и собирает ненулевые значения.
# Возвращает 0 (true) и выводит в stdout строки «метрика=значение», если
# хотя бы одна метрика оказалась > 0 (т.е. обнаружена проблема).
# Если все метрики нулевые — возвращает 1 (false), ничего не выводит.
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

# Записывает информацию о первой обнаруженной проблеме в RESULT_FILE.
# Формат файла — простой key=value, пригодный для чтения как человеком,
# так и скриптами (source / grep).
# Поле detail обрезается до 64 КБ (head -c 65536), чтобы при большом
# количестве логов файл не разросся до неприемлемых размеров.
write_result() {
  local reason=$1
  local when=$2
  local detail=$3
  {
    echo "# Первое срабатывание во время apply-yaml"
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

# ---- Информационный баннер при запуске ----
echo "Мониторинг: VL=$VL_URL VM=$VM_URL ns=$VMALERT_NAMESPACE интервал ${INTERVAL}s (OOM + логи LogsQL + метрики). Старт: $START_TS"
echo "Запись при первой проблеме: $RESULT_FILE"
echo ""

# ---- Основной цикл мониторинга ----
# Выполняется бесконечно до обнаружения первой проблемы или получения сигнала.
# Порядок проверок (от самой дешёвой к самой дорогой):
#   1. OOM vmalert  — один вызов kubectl, быстрая проверка
#   2. Логи VictoriaLogs — HTTP-запрос с LogsQL
#   3. Метрики VM   — несколько PromQL instant-запросов
# При первом же обнаружении проблемы — запись в файл и выход (exit 0).
while true; do
  # Фиксируем текущий момент для привязки к обнаруженной ошибке
  when=$(date -Iseconds)

  # --- Проверка 1: OOMKilled у подов vmalert ---
  if check_oom_vmalert; then
    msg="Pod vmalert: lastState OOMKilled (namespace ${VMALERT_NAMESPACE})"
    echo "[$when] ${msg}"
    write_result OOMKilled "$when" "$msg"
    exit 0
  fi

  # --- Проверка 2: ошибки в логах через VictoriaLogs ---
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

  # --- Проверка 3: PromQL-метрики VictoriaMetrics ---
  if metric_issues=$(collect_metric_issues); then
    echo "[$when] Метрики VictoriaMetrics (ненулевые):"
    echo -n "$metric_issues"
    write_result vm_metrics "$when" "$metric_issues"
    exit 0
  fi

  # Все проверки прошли без срабатываний — всё в порядке
  echo "[$when] OK"
  sleep "$INTERVAL"
done
