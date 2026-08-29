#!/usr/bin/env python3
"""Объединённый скрипт: деплой приложений + сбор снимков Capacity Planning.

Заменяет связку `scripts/deploy-apps.sh` + `scripts/fetch_capacity_snapshots.py`.
Запускается ОДИН раз и выполняет последовательно:

  1. Читает `app-names.txt`, формирует список
     `apps[START_INDEX-1 : START_INDEX-1+TARGET_APPS]`.
  2. Устанавливает app по одному через `helm upgrade --install --wait`
     (с ретраями до 3 раз при ошибке). Счётчик `installed` — это номер
     завершённого релиза в цикле (детерминирован, не зависит от kubectl).
   3. После каждого успешного helm install получает VMRule через Kubernetes API.
      Число алертов считается как количество VMRule, умноженное на число rules
      внутри VMRule (для неодинаковых объектов используется точная сумма rules).
   4. При `alerts_count >= TARGETS[next_idx]` делает instant-снимок всех
      метрик из QUERIES через `ThreadPoolExecutor(max_workers=16)`. В снимок
      записываются `alerts_count`, `alerts_count_estimated=False`,
      `installed`.
   5. После последнего порога выжидает SETTLE_WAIT секунд (по умолчанию 600)
      и делает финальный снимок «после через 10 мин установки N app».

Источник числа алертов для порогов — Kubernetes API: количество VMRule,
умноженное на количество rules внутри VMRule. Запросы к VictoriaMetrics для
подсчёта `ALERTS` не выполняются.

Переменные окружения (deploy, из scripts/deploy-apps.sh):
  CHART_PATH         по умолчанию <repo>/chart
  NAMES_FILE         по умолчанию <scripts>/app-names.txt
  IMAGE_REPO         по умолчанию ghcr.io/patsevanton/performance-test-alerts-victoriametrics
  IMAGE_TAG          по умолчанию 1.9.3
  ALERTS_PER_APP     по умолчанию 50  — алертов на одно приложение
  BASE_ALERTS_COUNT  по умолчанию 10  — базовых алертов на приложение
  EXTRA_ALERTS_COUNT по умолчанию ALERTS_PER_APP - BASE_ALERTS_COUNT
  START_INDEX        по умолчанию 1   — индекс первого app в app-names.txt
  TARGET_APPS        по умолчанию 1700 — число разворачиваемых приложений
  APP_TENANTS/APP_ROUTES/APP_HIST_BUCKETS/APP_REGION/APP_VERSION —
                     cardinality-параметры (передаются в Helm только при явном задании)

Переменные окружения (snapshot, из scripts/fetch_capacity_snapshots.py):
   POLL_INTERVAL      по умолчанию 10  — секунды между ретраями запроса VMRule
  MIN_SNAPSHOT_GAP   по умолчанию 120 — сохранено как env для совместимости;
                     при последовательном deploy не применяется.
  SETTLE_WAIT        по умолчанию 600 — пауза (с) после deploys всех app перед
                     финальным снимком «после через 10 мин»
  VMSELECT_URL       по умолчанию `terraform output -raw vmselect_url`
  RELEASE_NAME       по умолчанию vmks — имя release чарта
                     victoria-metrics-k8s-stack

Результаты:
  - capacity_snapshots.json : сырые значения метрик по порогам + rule_count
  - capacity_snapshots.txt  : форматированная таблица
  - stdout                  : та же таблица

Для досрочной остановки нажмите Ctrl-C — будут выгружены собранные снимки.
"""
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Аналог curl -sk (ingress может использовать самоподписанный сертификат)
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

# Куда писать результаты (рядом со скриптом).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
JSON_PATH = os.path.join(SCRIPT_DIR, "capacity_snapshots.json")
TXT_PATH = os.path.join(SCRIPT_DIR, "capacity_snapshots.txt")

# ---------------------------------------------------------------------------
# Параметры deploys (из scripts/deploy-apps.sh).
# ---------------------------------------------------------------------------
CHART_PATH = os.environ.get("CHART_PATH", os.path.join(REPO_ROOT, "chart"))
NAMES_FILE = os.environ.get("NAMES_FILE", os.path.join(SCRIPT_DIR, "app-names.txt"))
IMAGE_REPO = os.environ.get(
    "IMAGE_REPO", "ghcr.io/patsevanton/performance-test-alerts-victoriametrics")
IMAGE_TAG = os.environ.get("IMAGE_TAG", "1.9.3")
ALERTS_PER_APP = int(os.environ.get("ALERTS_PER_APP", "50"))
BASE_ALERTS_COUNT = int(os.environ.get("BASE_ALERTS_COUNT", "10"))
EXTRA_ALERTS_COUNT = int(os.environ.get(
    "EXTRA_ALERTS_COUNT", str(ALERTS_PER_APP - BASE_ALERTS_COUNT)))
START_INDEX = int(os.environ.get("START_INDEX", "1"))
TARGET_APPS = int(os.environ.get("TARGET_APPS", "1700"))

# Кардинальность метрик генератора (PLAN-high-cardinality.md, этап 3).
# Передаются в Helm как app.cardinality.* только при явном задании через env.
APP_TENANTS = os.environ.get("APP_TENANTS", "")
APP_ROUTES = os.environ.get("APP_ROUTES", "")
APP_HIST_BUCKETS = os.environ.get("APP_HIST_BUCKETS", "")
APP_REGION = os.environ.get("APP_REGION", "")
APP_VERSION = os.environ.get("APP_VERSION", "")

# ---------------------------------------------------------------------------
# Параметры snapshot (из scripts/fetch_capacity_snapshots.py).
# ---------------------------------------------------------------------------
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
# Минимальная пауза (с) между фиксацией снимков.
# Сохранено как env для совместимости. При последовательном deploy не применяется:
# естественная длительность установки уже разделяет снимки.
MIN_SNAPSHOT_GAP = int(os.environ.get("MIN_SNAPSHOT_GAP", "120"))
SETTLE_WAIT = int(os.environ.get("SETTLE_WAIT", "600"))
RELEASE_NAME = os.environ.get("RELEASE_NAME", "vmks")

EXPECTED_MAX_ALERTS = TARGET_APPS * ALERTS_PER_APP

# Базовый URL vmselect. Порядок разрешения:
#   1) переменная окружения VMSELECT_URL;
#   2) `terraform output -raw vmselect_url` (FQDN через sslip.io из публичного IP ingress);
#   3) ошибка с подсказкой.
# Ожидается URL вида http://vmselect.<IP>.sslip.io — путь
# /select/0/prometheus/api/v1/query добавляется автоматически.
def _resolve_base() -> str:
    url = os.environ.get("VMSELECT_URL")
    if not url:
        try:
            out = subprocess.run(
                ["terraform", "output", "-raw", "vmselect_url"],
                capture_output=True, text=True, cwd=REPO_ROOT,
            )
        except FileNotFoundError:
            out = None
        if out is not None and out.returncode == 0:
            url = out.stdout.strip()
    if not url:
        print("Не удалось определить VMSELECT_URL. Задайте переменную окружения "
              "VMSELECT_URL или выполните `terraform output -raw vmselect_url`.",
              file=sys.stderr)
        sys.exit(2)
    return url.rstrip("/") + "/select/0/prometheus/api/v1/query"


BASE = _resolve_base()

# Пороги количества rules, при достижении которых фиксируется снимок.
# Последний порог равен ожидаемому максимуму алертов (TARGET_APPS*ALERTS_PER_APP),
# чтобы скрипт продолжал сбор до фактического завершения deploys всех приложений.
TARGETS = [500, 5000, 10000, 15000, 20000, 25000, 30000, 35000,
           40000, 45000, 50000, 55000, 60000, 65000, 70000, 75000, 80000, 85000]

# Префиксы имён подов и job'ов компонент vmks для подстановки в PromQL.
_VMKS_STACK_PREFIX = f"{RELEASE_NAME}-victoria-metrics-k8s-stack"
_OPERATOR_PREFIX = f"{RELEASE_NAME}-victoria-metrics-operator"

QUERIES = {
    "vmalert_cpu": f'avg(rate(pod_cpu_usage_seconds_total{{namespace="vmks",pod=~"vmalert-{_VMKS_STACK_PREFIX}-.*"}}[2m]))*1000',
    "vmstorage_cpu": f'avg(rate(pod_cpu_usage_seconds_total{{namespace="vmks",pod=~"vmstorage-{_VMKS_STACK_PREFIX}-.*"}}[2m]))*1000',
    "vmselect_cpu": f'avg(rate(pod_cpu_usage_seconds_total{{namespace="vmks",pod=~"vmselect-{_VMKS_STACK_PREFIX}-.*"}}[2m]))*1000',
    "vminsert_cpu": f'avg(rate(pod_cpu_usage_seconds_total{{namespace="vmks",pod=~"vminsert-{_VMKS_STACK_PREFIX}-.*"}}[2m]))*1000',
    "vmagent_cpu": f'avg(rate(pod_cpu_usage_seconds_total{{namespace="vmks",pod=~"vmagent-{_VMKS_STACK_PREFIX}-.*"}}[2m]))*1000',
    "operator_cpu": f'avg(rate(pod_cpu_usage_seconds_total{{namespace="vmks",pod=~"{_OPERATOR_PREFIX}-.*"}}[2m]))*1000',
    "vmalert_mem": f'avg(pod_memory_working_set_bytes{{namespace="vmks",pod=~"vmalert-{_VMKS_STACK_PREFIX}-.*"}})/1024/1024',
    "vmstorage_mem": f'avg(pod_memory_working_set_bytes{{namespace="vmks",pod=~"vmstorage-{_VMKS_STACK_PREFIX}-.*"}})/1024/1024',
    "vmselect_mem": f'avg(pod_memory_working_set_bytes{{namespace="vmks",pod=~"vmselect-{_VMKS_STACK_PREFIX}-.*"}})/1024/1024',
    "vminsert_mem": f'avg(pod_memory_working_set_bytes{{namespace="vmks",pod=~"vminsert-{_VMKS_STACK_PREFIX}-.*"}})/1024/1024',
    "vmagent_mem": f'avg(pod_memory_working_set_bytes{{namespace="vmks",pod=~"vmagent-{_VMKS_STACK_PREFIX}-.*"}})/1024/1024',
    "operator_mem": f'avg(pod_memory_working_set_bytes{{namespace="vmks",pod=~"{_OPERATOR_PREFIX}-.*"}})/1024/1024',
    "apiserver_rps": "sum(rate(apiserver_request_total[5m]))",
    # Исключаем бакет +Inf — иначе p99 может схлопнуться до наибольшей конечной границы (напр. 60s).
    "apiserver_p99": 'histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket{le!="+Inf"}[5m])) by (le))',
    "apiserver_cpu": 'sum(rate(process_cpu_seconds_total{job=~".*apiserver.*"}[5m]))*1000',
    "vmselect_rps": f'sum(rate(vm_http_requests_total{{job="vmselect-{_VMKS_STACK_PREFIX}"}}[5m]))',
    "vmstorage_rps": f'sum(rate(vm_http_requests_total{{job="vmstorage-{_VMKS_STACK_PREFIX}"}}[5m]))',
    "vminsert_rps": f'sum(rate(vm_http_requests_total{{job="vminsert-{_VMKS_STACK_PREFIX}"}}[5m]))',
    # Дополнительные метрики для таблицы "grew with load" в README.
    "vmalert_iter_max": f'max(vmalert_iteration_duration_seconds{{namespace="vmks",pod=~"vmalert-{_VMKS_STACK_PREFIX}-.*"}})',
    "vmselect_concurrent": f'max(vm_concurrent_select_current{{job="vmselect-{_VMKS_STACK_PREFIX}"}})',
    "vmagent_scrape_samples_vmalert": f'max(scrape_samples_scraped{{job="vmalert-{_VMKS_STACK_PREFIX}"}})',
}

# Дополнительные метрики для высококардинального профиля (PLAN-high-cardinality.md, 5.4).
# Собираются всегда — тяжёлая часть всегда активна.
QUERIES.update({
    # Сканирование рядов при тяжёлом PromQL.
    "vm_rows_scanned_total": f'sum(rate(vm_rows_scanned_total{{job="vmselect-{_VMKS_STACK_PREFIX}"}}[5m]))',
    "vm_rows_scanned_vmstorage": f'sum(rate(vm_rows_scanned_total{{job="vmstorage-{_VMKS_STACK_PREFIX}"}}[5m]))',
    # Cache miss по компонентам кэша.
    "vm_cache_misses_vmselect": f'sum(rate(vm_cache_misses_total{{job="vmselect-{_VMKS_STACK_PREFIX}"}}[5m]))',
    "vm_cache_misses_vmstorage": f'sum(rate(vm_cache_misses_total{{job="vmstorage-{_VMKS_STACK_PREFIX}"}}[5m]))',
    # Latency поиска по рядам.
    "vm_search_latency_max": f'max(vm_search_latency_seconds{{job=~"vmselect-{_VMKS_STACK_PREFIX}|vmstorage-{_VMKS_STACK_PREFIX}"}})',
    # Нагрузка на evaluation-движок vmalert по тяжёлой группе.
    "vm_evaluation_duration_max": f'max(vmalert_evaluation_duration_seconds{{job="vmalert-{_VMKS_STACK_PREFIX}"}})',
    "vm_evaluation_count_rate": f'sum(rate(vmalert_evaluations_total{{job="vmalert-{_VMKS_STACK_PREFIX}"}}[5m]))',
    # p99 длительности итерации vmalert.
    "vmalert_iter_p99": (
        f'histogram_quantile(0.99, '
        f'sum(rate(vmalert_iteration_duration_seconds_bucket{{job="vmalert-{_VMKS_STACK_PREFIX}"}}[5m])) by (le))'
    ),
})

# Метка финального снимка «после через 10 мин установки N app».
SETTLE_LABEL = f"после через 10 мин установки {TARGET_APPS} app"

# Мягкая обработка Ctrl-C: прекратить deploys/опрос, выгрузить собранные снимки.
_INTERRUPTED = {"flag": False}


def _sigint(signum, frame):
    _INTERRUPTED["flag"] = True


signal.signal(signal.SIGINT, _sigint)


# ---------------------------------------------------------------------------
# HTTP-запросы к vmselect (взято без изменений из fetch_capacity_snapshots.py).
# ---------------------------------------------------------------------------
def qval(data: dict) -> float | None:
    r = data.get("data", {}).get("result") or []
    if not r:
        return None
    v = r[0].get("value", [None, None])[1]
    if v is None:
        return None
    return float(v)


def fetch_one(t: int, query: str) -> float | None:
    params = urllib.parse.urlencode({"query": query, "time": str(t)})
    url = f"{BASE}?{params}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120, context=_SSL) as resp:
        return qval(json.load(resp))


def fetch_snapshot(t: int) -> dict:
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {k: ex.submit(fetch_one, t, q) for k, q in QUERIES.items()}
        return {k: futs[k].result() for k in QUERIES}


# ---------------------------------------------------------------------------
# Форматтеры (взято без изменений из fetch_capacity_snapshots.py).
# ---------------------------------------------------------------------------
def fmt_cpu_m(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{round(x)}m"


def fmt_mem_mi(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{round(x)}Mi"


def fmt_rps(x: float | None, nd: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x:.{nd}f}".rstrip("0").rstrip(".")


def fmt_p99_sec(x: float | None) -> str:
    if x is None:
        return "—"
    ms = x * 1000
    return f"{round(ms)} ms"


def fmt_sec(x: float | None, nd: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:.{nd}f}s".rstrip("0").rstrip(".")


def fmt_count(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{round(x)}"


def fmt_rel(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"T+{h:02d}:{m:02d}:{s:02d}"


def render_table(snapshots: list, rule_state: dict | None = None) -> str:
    lines = []
    lines.append(f"TARGET_APPS={TARGET_APPS} ALERTS_PER_APP={ALERTS_PER_APP} "
                 f"BASE_ALERTS_COUNT={BASE_ALERTS_COUNT} "
                 f"expected_max_alerts={EXPECTED_MAX_ALERTS}")
    if rule_state:
        lines.append(
            f"vmrule_count={rule_state.get('vmrule_count')} "
            f"rules_per_vmrule={rule_state.get('rules_per_vmrule')}"
        )
    lines.append("")
    for snap in snapshots:
        label = snap["threshold"]
        rel = fmt_rel(snap["rel_seconds"])
        ts = snap["timestamp"]
        measured = snap["alerts_count"]
        m = snap["metrics"]
        if isinstance(label, str):
            header = f"=== {label} (measured={measured}) {rel} ts={ts} ==="
        else:
            header = f"=== ALERTS~{label} (measured={measured}) {rel} ts={ts} ==="
        if snap.get("retrospective"):
            header += " [retrospective]"
        if snap.get("alerts_count_estimated"):
            header += " [estimated]"
        lines.append(header)
        lines.append(
            "CPU "
            + " ".join([
                fmt_cpu_m(m.get("vmalert_cpu")),
                fmt_cpu_m(m.get("vmstorage_cpu")),
                fmt_cpu_m(m.get("vmselect_cpu")),
                fmt_cpu_m(m.get("vminsert_cpu")),
                fmt_cpu_m(m.get("vmagent_cpu")),
                fmt_cpu_m(m.get("operator_cpu")),
            ])
        )
        lines.append(
            "MEM "
            + " ".join([
                fmt_mem_mi(m.get("vmalert_mem")),
                fmt_mem_mi(m.get("vmstorage_mem")),
                fmt_mem_mi(m.get("vmselect_mem")),
                fmt_mem_mi(m.get("vminsert_mem")),
                fmt_mem_mi(m.get("vmagent_mem")),
                fmt_mem_mi(m.get("operator_mem")),
            ])
        )
        p99 = m.get("apiserver_p99")
        lines.append(
            "RPS "
            + " ".join([
                fmt_rps(m.get("apiserver_rps")),
                fmt_p99_sec(p99),
                fmt_cpu_m(m.get("apiserver_cpu")),
                fmt_rps(m.get("vmselect_rps")),
                fmt_rps(m.get("vmstorage_rps"), 1),
                fmt_rps(m.get("vminsert_rps")),
            ])
        )
        lines.append(
            "GROWTH "
            + " ".join([
                fmt_sec(m.get("vmalert_iter_max")),
                fmt_count(m.get("vmselect_concurrent")),
                fmt_count(m.get("vmagent_scrape_samples_vmalert")),
            ])
        )
        # Метрики высококардинального профиля (PLAN-high-cardinality.md, 5.4) — всегда.
        lines.append(
            "HEAVY "
            + " ".join([
                fmt_count(m.get("vm_rows_scanned_total")),
                fmt_count(m.get("vm_rows_scanned_vmstorage")),
                fmt_count(m.get("vm_cache_misses_vmselect")),
                fmt_count(m.get("vm_cache_misses_vmstorage")),
                fmt_sec(m.get("vm_search_latency_max")),
                fmt_sec(m.get("vm_evaluation_duration_max")),
                fmt_count(m.get("vm_evaluation_count_rate")),
                fmt_sec(m.get("vmalert_iter_p99")),
            ])
        )
        lines.append("")
    return "\n".join(lines)


def write_outputs(snapshots: list, rule_state: dict | None = None) -> None:
    payload = {
        "params": {
            "TARGET_APPS": TARGET_APPS,
            "ALERTS_PER_APP": ALERTS_PER_APP,
            "BASE_ALERTS_COUNT": BASE_ALERTS_COUNT,
            "EXTRA_ALERTS_COUNT": EXTRA_ALERTS_COUNT,
            "START_INDEX": START_INDEX,
            "POLL_INTERVAL": POLL_INTERVAL,
            "MIN_SNAPSHOT_GAP": MIN_SNAPSHOT_GAP,
            "SETTLE_WAIT": SETTLE_WAIT,
            "RELEASE_NAME": RELEASE_NAME,
            "expected_max_alerts": EXPECTED_MAX_ALERTS,
        },
        "thresholds": TARGETS,
        "rule_count": rule_state,
        "snapshots": snapshots,
    }
    with open(JSON_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    table = render_table(snapshots, rule_state)
    with open(TXT_PATH, "w") as f:
        f.write(table + "\n")
    print(table)


# ---------------------------------------------------------------------------
# Deploys (адаптация deploy-apps.sh).
# ---------------------------------------------------------------------------
def build_set_args() -> list[str]:
    args = [
        "--set", f"image.repository={IMAGE_REPO}",
        "--set", f"image.tag={IMAGE_TAG}",
        "--set", f"alerts.extra.count={EXTRA_ALERTS_COUNT}",
    ]
    # Кардинальность передаётся только при явном задании через env.
    if APP_TENANTS:
        args += ["--set", f"app.cardinality.tenants={APP_TENANTS}"]
    if APP_ROUTES:
        args += ["--set", f"app.cardinality.routes={APP_ROUTES}"]
    if APP_HIST_BUCKETS:
        args += ["--set", f"app.cardinality.histBuckets={APP_HIST_BUCKETS}"]
    if APP_REGION:
        args += ["--set", f"app.cardinality.region={APP_REGION}"]
    if APP_VERSION:
        args += ["--set", f"app.cardinality.version={APP_VERSION}"]
    return args


def deploy_app(name: str, set_args: list[str]) -> bool:
    """Создаёт namespace и делает `helm upgrade --install --wait`.

    Возвращает True при успехе, False после 3 неудач. На SIGINT возвращает False
    и выставляет флаг прерывания.
    """
    subprocess.run(
        ["kubectl", "create", "namespace", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    cmd = [
        "helm", "upgrade", "--install", name, CHART_PATH,
        "--namespace", name,
        *set_args,
        "--wait",
        "--timeout", "2m",
    ]
    for attempt in range(1, 4):
        if _INTERRUPTED["flag"]:
            return False
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=150,
            )
        except subprocess.TimeoutExpired:
            out = None
        if out is not None and out.returncode == 0:
            tail = (out.stdout or "").strip().splitlines()
            if tail:
                print(tail[-1])
            return True
        err = ""
        if out is not None:
            err = (out.stderr or "").strip().splitlines()
            err = err[-1] if err else ""
        print(f"  helm install {name} failed (attempt {attempt}/3): {err}",
              file=sys.stderr)
        if attempt < 3 and not _INTERRUPTED["flag"]:
            time.sleep(POLL_INTERVAL)
    return False


# ---------------------------------------------------------------------------
# Подсчёт rules в VMRule.
# ---------------------------------------------------------------------------
def count_vmrule_alerts(rel_start: int, state: dict) -> int | None:
    """Считает rules во всех VMRule через Kubernetes API с тремя ретраями."""
    for attempt in range(1, 4):
        if _INTERRUPTED["flag"]:
            return None
        try:
            out = subprocess.run(
                ["kubectl", "get", "vmrule", "-A", "-o", "json"],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode != 0:
                raise RuntimeError((out.stderr or "kubectl failed").strip())
            items = json.loads(out.stdout).get("items", [])
            rules_per_vmrule = [
                sum(len(group.get("rules", []))
                    for group in item.get("spec", {}).get("groups", []))
                for item in items
            ]
            rules_per_vmrule_value = rules_per_vmrule[0] if rules_per_vmrule else 0
            total = (len(items) * rules_per_vmrule_value
                     if all(value == rules_per_vmrule_value for value in rules_per_vmrule)
                     else sum(rules_per_vmrule))
            state["vmrule_count"] = len(items)
            state["rules_per_vmrule"] = rules_per_vmrule_value
            if total:
                return total
            raise RuntimeError("VMRule objects contain no rules yet")
        except Exception as e:
            print(f"[{fmt_rel(rel_start)}] VMRule query failed "
                  f"(attempt {attempt}/3): {e}", file=sys.stderr)
        if attempt < 3 and not _INTERRUPTED["flag"]:
            time.sleep(POLL_INTERVAL)
    return None


# ---------------------------------------------------------------------------
# Снимки по порогам.
# ---------------------------------------------------------------------------
def capture_snapshot(target, installed: int, alerts_count: float,
                       rel_start: int, *,
                      retrospective: bool = False) -> dict:
    now = int(time.time())
    rel = now - rel_start
    try:
        metrics = fetch_snapshot(now)
    except Exception as e:
        print(f"[{fmt_rel(rel)}] snapshot fetch failed for target={target}: {e}",
              file=sys.stderr)
        metrics = {}
    snap = {
        "threshold": target,
        "timestamp": now,
        "rel_seconds": rel,
        "alerts_count": int(alerts_count),
        "alerts_count_estimated": False,
        "installed": installed,
        "metrics": metrics,
    }
    if retrospective:
        snap["retrospective"] = True
    print(f"[{fmt_rel(rel)}] captured snapshot for ALERTS~{target} "
          f"(measured={int(alerts_count)} installed={installed})"
          + (" [retrospective]" if retrospective else ""))
    return snap


def capture_settle_snapshot(installed: int, alerts_count: float,
                             rel_start: int) -> dict:
    now = int(time.time())
    rel = now - rel_start
    try:
        metrics = fetch_snapshot(now)
    except Exception as e:
        print(f"[{fmt_rel(rel)}] settle snapshot fetch failed: {e}",
              file=sys.stderr)
        metrics = {}
    snap = {
        "threshold": SETTLE_LABEL,
        "timestamp": now,
        "rel_seconds": rel,
        "alerts_count": int(alerts_count),
        "alerts_count_estimated": False,
        "installed": installed,
        "metrics": metrics,
    }
    print(f"[{fmt_rel(rel)}] captured settle snapshot «{SETTLE_LABEL}»")
    return snap


# ---------------------------------------------------------------------------
# Главный цикл.
# ---------------------------------------------------------------------------
def main() -> None:
    if ALERTS_PER_APP < BASE_ALERTS_COUNT:
        print(f"Ошибка: ALERTS_PER_APP ({ALERTS_PER_APP}) должен быть >= "
              f"BASE_ALERTS_COUNT ({BASE_ALERTS_COUNT}).", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(NAMES_FILE):
        print(f"Ошибка: файл {NAMES_FILE} не найден. Сначала выполните "
              f"generate-app-names.sh.", file=sys.stderr)
        sys.exit(1)

    with open(NAMES_FILE) as f:
        all_names = [ln.strip() for ln in f if ln.strip()]

    required = START_INDEX + TARGET_APPS - 1
    if len(all_names) < required:
        print(f"Ошибка: в {NAMES_FILE} только {len(all_names)} имён, "
              f"а нужно минимум {required}.", file=sys.stderr)
        sys.exit(1)

    start_offset = START_INDEX - 1
    apps = all_names[start_offset:start_offset + TARGET_APPS]

    snapshots: list = []
    rule_state: dict = {"vmrule_count": 0, "rules_per_vmrule": 0}

    print(f"Развёртывание app-{START_INDEX}..app-{required} ({TARGET_APPS} шт.) "
          f"через helm upgrade --install --wait --timeout 2m "
          f"(ретраи до 3)")
    print(f"VMSELECT_URL={BASE}")
    print(f"TARGETS={TARGETS} expected_max_alerts={EXPECTED_MAX_ALERTS} "
          f"(TARGET_APPS={TARGET_APPS} ALERTS_PER_APP={ALERTS_PER_APP})")
    print("Подсчёт порогов: количество VMRule × количество rules внутри VMRule")
    print("Нажмите Ctrl-C для досрочной остановки и выгрузки собранных снимков.")
    print()

    start_ts = time.time()
    set_args = build_set_args()

    next_idx = 0
    for i, name in enumerate(apps, start=1):
        if _INTERRUPTED["flag"]:
            break
        ok = deploy_app(name, set_args)
        if not ok:
            if _INTERRUPTED["flag"]:
                break
            # Ретраи исчерпаны: пропускаем app, не увеличиваем installed.
            print(f"[{fmt_rel(int(time.time() - start_ts))}] "
                  f"app {name} (#{i}) не установлен — пропускаю",
                  file=sys.stderr)
            continue
        installed = i

        alerts_count = count_vmrule_alerts(
            int(time.time() - start_ts), rule_state)
        if alerts_count is not None and not _INTERRUPTED["flag"]:
            print(f"[{fmt_rel(int(time.time() - start_ts))}] "
                  f"VMRule={rule_state['vmrule_count']} rules={alerts_count}")
            while (next_idx < len(TARGETS)
                   and alerts_count >= TARGETS[next_idx]
                   and not _INTERRUPTED["flag"]):
                target = TARGETS[next_idx]
                snap = capture_snapshot(
                    target, installed, alerts_count, int(start_ts))
                snapshots.append(snap)
                next_idx += 1

    if _INTERRUPTED["flag"]:
        print("\nInterrupted by user.", file=sys.stderr)

    reached = {s["threshold"] for s in snapshots}
    missed = [t for t in TARGETS if t not in reached and not isinstance(t, str)]
    if missed:
        print(f"Thresholds not reached: {missed}", file=sys.stderr)

    # Финальный снимок «после через 10 мин установки N app».
    all_targets_reached = not missed and not _INTERRUPTED["flag"]
    if all_targets_reached:
        print(f"\nВсе пороги достигнуты. "
              f"Жду {SETTLE_WAIT}s ({SETTLE_WAIT // 60} мин) перед финальным "
              f"снимком...")
        settle_start = time.time()
        while not _INTERRUPTED["flag"]:
            elapsed = int(time.time() - settle_start)
            remaining = SETTLE_WAIT - elapsed
            if remaining <= 0:
                break
            rel = int(time.time() - start_ts)
            print(f"[{fmt_rel(rel)}] settle wait: {elapsed}s/{SETTLE_WAIT}s "
                  f"(осталось {remaining}s)")
            # Печатаем прогресс не чаще раза в минуту.
            time.sleep(min(60, remaining))

        if not _INTERRUPTED["flag"]:
            alerts_count = count_vmrule_alerts(int(time.time() - start_ts), rule_state)
            if alerts_count is not None:
                snap = capture_settle_snapshot(TARGET_APPS, alerts_count, int(start_ts))
                snapshots.append(snap)

    write_outputs(snapshots, rule_state)
    print()
    print(f"JSON: {JSON_PATH}")
    print(f"TXT : {TXT_PATH}")


if __name__ == "__main__":
    main()
