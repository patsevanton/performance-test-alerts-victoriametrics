#!/usr/bin/env python3
"""Сбор снимков Capacity Planning из VictoriaMetrics во время живого теста.

Запускается ОДИН раз в начале теста (параллельно с scripts/deploy-apps.sh). Скрипт
опрашивает `count(ALERTS)` каждые POLL_INTERVAL секунд и, как только значение
пересекает очередной порог из TARGETS (500, 5000, ..., 67500), фиксирует снимок всех
метрик из QUERIES через instant-запрос с `time=now`. Последний порог равен
TARGET_APPS*ALERTS_PER_APP, поэтому скрипт собирает снимки вплоть до завершения
деплоя всех приложений, а не выходит раньше на промежуточном пороге. После каждого
снимка скрипт выдерживает MIN_SNAPSHOT_GAP секунд, чтобы rate-метрики устаканились.

После достижения последнего порога (67500) скрипт:
  1) дожидается установки TARGET_APPS приложений (через `kubectl get pod -A | grep app | wc -l`);
  2) выжидает SETTLE_WAIT секунд (по умолчанию 600 = 10 минут);
  3) делает дополнительный снимок с меткой
     «после через 10 мин установки 1700 app».

Скрипт ожидает достижения всех порогов бесконечно; для досрочной остановки нажмите
Ctrl-C — будут выгружены снимки, собранные к этому моменту.

Результаты:
  - capacity_snapshots.json : сырые значения метрик по порогам
  - capacity_snapshots.txt  : форматированная таблица
  - stdout                  : та же таблица

Переменные окружения (значения по умолчанию взяты из scripts/deploy-apps.sh):
  TARGET_APPS       по умолчанию 1700 — число разворачиваемых приложений
  ALERTS_PER_APP    по умолчанию 50   — алертов на одно приложение
  BASE_ALERTS_COUNT по умолчанию 10   — базовых алертов на приложение (информационно)
  POLL_INTERVAL     по умолчанию 10   — секунды между опросами count(ALERTS)
  MIN_SNAPSHOT_GAP  по умолчанию 120  — минимальная пауза (с) между снимками,
                    чтобы rate-метрики успели устаканиться после фиксации порога
  SETTLE_WAIT       по умолчанию 600  — пауза (с) после установки TARGET_APPS
                    app перед финальным снимком «после через 10 мин».
  VMSELECT_URL      по умолчанию берётся из `terraform output -raw vmselect_url`
                    (FQDN формируется через sslip.io из публичного IP ingress-nginx).
  RELEASE_NAME      по умолчанию vmks — имя release, которым установлен чарт
                    victoria-metrics-k8s-stack (helm install <RELEASE_NAME> ...).
                    От него зависят имена подов и scrape-job'ов компонент vmks.
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

# Базовый URL vmselect. Порядок разрешения:
#   1) переменная окружения VMSELECT_URL;
#   2) `terraform output -raw vmselect_url` (FQDN через sslip.io из публичного IP ingress);
#   3) ошибка с подсказкой.
# Ожидается URL вида http://vmselect.<IP>.sslip.io — путь /select/0/prometheus/api/v1/query
# добавляется автоматически.
def _resolve_base() -> str:
    url = os.environ.get("VMSELECT_URL")
    if not url:
        # Terraform-конфигурация лежит в корне проекта (на уровень выше scripts/).
        tf_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
        try:
            out = subprocess.run(
                ["terraform", "output", "-raw", "vmselect_url"],
                capture_output=True, text=True, cwd=tf_dir,
            )
        except FileNotFoundError:
            out = None
        if out is not None and out.returncode == 0:
            url = out.stdout.strip()
    if not url:
        print("Не удалось определить VMSELECT_URL. Задайте переменную окружения VMSELECT_URL "
              "или выполните `terraform output -raw vmselect_url`.", file=sys.stderr)
        sys.exit(2)
    return url.rstrip("/") + "/select/0/prometheus/api/v1/query"

BASE = _resolve_base()

# Пороги (count(ALERTS)), при достижении которых фиксируется снимок.
# Последний порог равен ожидаемому максимуму алертов (TARGET_APPS*ALERTS_PER_APP),
# чтобы скрипт продолжал сбор до фактического завершения деплоя всех приложений,
# а не выходил раньше на промежуточном пороге.
TARGETS = [500, 5000, 10000, 15000, 20000, 25000, 30000, 35000,
           40000, 45000, 50000, 55000, 60000, 65000, 67500]

# Параметры деплоя (значения по умолчанию из scripts/deploy-apps.sh).
TARGET_APPS = int(os.environ.get("TARGET_APPS", "1700"))
ALERTS_PER_APP = int(os.environ.get("ALERTS_PER_APP", "50"))
BASE_ALERTS_COUNT = int(os.environ.get("BASE_ALERTS_COUNT", "10"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
# Минимальная пауза (с) между фиксацией снимков. После снятия снимка скрипт
# выжидает это время перед продолжением опроса, чтобы rate-метрики (CPU, RPS,
# p99, vmalert_iteration_duration) успели устаканиться и не отражать скачок
# от отставания vmalert. Иначе при быстром прохождении подряд идущих порогов
# (например 35k→50k за минуту) снимки дублируют друг друга по значениям.
MIN_SNAPSHOT_GAP = int(os.environ.get("MIN_SNAPSHOT_GAP", "120"))

# Пауза (с) между подтверждением установки TARGET_APPS app и финальным снимком
# «после через 10 мин установки 1700 app». По умолчанию 600 = 10 минут.
SETTLE_WAIT = int(os.environ.get("SETTLE_WAIT", "600"))

EXPECTED_MAX_ALERTS = TARGET_APPS * ALERTS_PER_APP

# Имя release чарта victoria-metrics-k8s-stack (helm install <RELEASE_NAME> ...).
# От него зависят имена подов (vmalert-<RELEASE_NAME>-victoria-metrics-k8s-stack-.*)
# и scrape-job'ов (vmalert-<RELEASE_NAME>-victoria-metrics-k8s-stack).
# Namespace жёстко vmks согласно инфраструктурным правилам (AGENTS.md).
RELEASE_NAME = os.environ.get("RELEASE_NAME", "vmks")

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

COUNT_QUERY = "count(ALERTS)"

# Куда писать результаты (рядом со скриптом).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(SCRIPT_DIR, "capacity_snapshots.json")
TXT_PATH = os.path.join(SCRIPT_DIR, "capacity_snapshots.txt")

# Метка финального снимка «после через 10 мин установки N app».
SETTLE_LABEL = f"после через 10 мин установки {TARGET_APPS} app"


def count_app_pods() -> int:
    """Число установленных приложений через подсчёт app-подов.

    deploy-apps.sh создаёт один helm release на приложение в одноимённом
    namespace. Каждый app-под имеет в имени «app» (app-1, app-2, ...), поэтому
    для подсчёта числа установленных приложений используется
    `kubectl get pod -A | grep app | wc -l`.
    """
    try:
        out = subprocess.run(
            "kubectl get pod -A | grep app | wc -l",
            shell=True, capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1
    if out.returncode != 0:
        return -1
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


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


def render_table(snapshots: list) -> str:
    lines = []
    lines.append(f"TARGET_APPS={TARGET_APPS} ALERTS_PER_APP={ALERTS_PER_APP} "
                 f"BASE_ALERTS_COUNT={BASE_ALERTS_COUNT} "
                 f"expected_max_alerts={EXPECTED_MAX_ALERTS}")
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
        lines.append(header)
        lines.append(
            "CPU "
            + " ".join([
                fmt_cpu_m(m["vmalert_cpu"]),
                fmt_cpu_m(m["vmstorage_cpu"]),
                fmt_cpu_m(m["vmselect_cpu"]),
                fmt_cpu_m(m["vminsert_cpu"]),
                fmt_cpu_m(m["vmagent_cpu"]),
                fmt_cpu_m(m["operator_cpu"]),
            ])
        )
        lines.append(
            "MEM "
            + " ".join([
                fmt_mem_mi(m["vmalert_mem"]),
                fmt_mem_mi(m["vmstorage_mem"]),
                fmt_mem_mi(m["vmselect_mem"]),
                fmt_mem_mi(m["vminsert_mem"]),
                fmt_mem_mi(m["vmagent_mem"]),
                fmt_mem_mi(m["operator_mem"]),
            ])
        )
        p99 = m["apiserver_p99"]
        lines.append(
            "RPS "
            + " ".join([
                fmt_rps(m["apiserver_rps"]),
                fmt_p99_sec(p99),
                fmt_cpu_m(m["apiserver_cpu"]),
                fmt_rps(m["vmselect_rps"]),
                fmt_rps(m["vmstorage_rps"], 1),
                fmt_rps(m["vminsert_rps"]),
            ])
        )
        lines.append(
            "GROWTH "
            + " ".join([
                fmt_sec(m["vmalert_iter_max"]),
                fmt_count(m["vmselect_concurrent"]),
                fmt_count(m["vmagent_scrape_samples_vmalert"]),
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


def write_outputs(snapshots: list) -> None:
    table = render_table(snapshots)
    with open(JSON_PATH, "w") as f:
        json.dump(
            {
                "params": {
                    "TARGET_APPS": TARGET_APPS,
                    "ALERTS_PER_APP": ALERTS_PER_APP,
                    "BASE_ALERTS_COUNT": BASE_ALERTS_COUNT,
                    "POLL_INTERVAL": POLL_INTERVAL,
                    "expected_max_alerts": EXPECTED_MAX_ALERTS,
                },
                "thresholds": TARGETS,
                "snapshots": snapshots,
            },
            f,
            indent=2,
        )
    with open(TXT_PATH, "w") as f:
        f.write(table + "\n")
    print(table)


def main() -> None:
    print(f"Опрашиваю count(ALERTS) каждые {POLL_INTERVAL}s. "
          f"TARGETS={TARGETS} "
          f"expected_max_alerts={EXPECTED_MAX_ALERTS} "
          f"(TARGET_APPS={TARGET_APPS} ALERTS_PER_APP={ALERTS_PER_APP})")
    print("Нажмите Ctrl-C для досрочной остановки и выгрузки собранных снимков.")
    print()

    snapshots: list = []
    next_idx = 0  # индекс в TARGETS для следующего порога, который нужно поймать
    start_ts = time.time()

    # Мягкая обработка Ctrl-C: прекратить опрос, выгрузить то, что успели.
    interrupted = {"flag": False}

    def _sigint(signum, frame):
        interrupted["flag"] = True

    signal.signal(signal.SIGINT, _sigint)

    while next_idx < len(TARGETS) and not interrupted["flag"]:
        now = int(time.time())
        try:
            count_val = fetch_one(now, COUNT_QUERY)
        except Exception as e:
            print(f"[{fmt_rel(int(now - start_ts))}] count(ALERTS) query failed: {e}", file=sys.stderr)
            time.sleep(POLL_INTERVAL)
            continue

        rel = int(now - start_ts)
        if count_val is None:
            print(f"[{fmt_rel(rel)}] count(ALERTS)=<нет данных>")
            time.sleep(POLL_INTERVAL)
            continue

        target = TARGETS[next_idx]
        print(f"[{fmt_rel(rel)}] count(ALERTS)={int(count_val)} (next target={target})")

        if count_val >= target:
            # Фиксируем снимок в текущий момент.
            try:
                metrics = fetch_snapshot(now)
            except Exception as e:
                print(f"[{fmt_rel(rel)}] snapshot fetch failed for target={target}: {e}", file=sys.stderr)
                time.sleep(POLL_INTERVAL)
                continue
            snap = {
                "threshold": target,
                "timestamp": now,
                "rel_seconds": rel,
                "alerts_count": int(count_val),
                "metrics": metrics,
            }
            snapshots.append(snap)
            print(f"[{fmt_rel(rel)}] captured snapshot for ALERTS~{target} (measured={int(count_val)})")
            next_idx += 1
            # Даём rate-метрикам устаканиться после снятия снимка, чтобы
            # следующий порог не зафиксировал «скачок» vmalert.
            if next_idx < len(TARGETS) and MIN_SNAPSHOT_GAP > 0:
                print(f"[{fmt_rel(rel)}] waiting {MIN_SNAPSHOT_GAP}s before next target "
                      f"({TARGETS[next_idx]}) to let rate metrics settle")
                time.sleep(MIN_SNAPSHOT_GAP)
            continue

        time.sleep(POLL_INTERVAL)

    if interrupted["flag"]:
        print("\nInterrupted by user.", file=sys.stderr)

    missed = [t for t in TARGETS if t not in {s["threshold"] for s in snapshots}]
    if missed:
        print(f"Thresholds not reached: {missed}", file=sys.stderr)

    # После достижения последнего порога (67500):
    # 1) ждём установки TARGET_APPS приложений (kubectl get pod -A | grep app | wc -l);
    # 2) выжидаем SETTLE_WAIT секунд (по умолчанию 10 минут);
    # 3) делаем финальный снимок «после через 10 мин установки N app».
    all_targets_reached = not missed and not interrupted["flag"]
    if all_targets_reached:
        print(f"\nВсе пороги достигнуты. Ожидаю установки {TARGET_APPS} app "
              f"(проверяю kubectl get pod -A | grep app | wc -l каждые {POLL_INTERVAL}s)...")
        wait_start = time.time()
        while not interrupted["flag"]:
            installed = count_app_pods()
            rel = int(time.time() - start_ts)
            if installed < 0:
                print(f"[{fmt_rel(rel)}] kubectl get pod недоступен — продолжаю ждать",
                      file=sys.stderr)
            else:
                print(f"[{fmt_rel(rel)}] app pods={installed}/{TARGET_APPS}")
                if installed >= TARGET_APPS:
                    break
            time.sleep(POLL_INTERVAL)

        if not interrupted["flag"]:
            print(f"\nУстановлено {TARGET_APPS} app. "
                  f"Жду {SETTLE_WAIT}s ({SETTLE_WAIT // 60} мин) перед финальным снимком...")
            settle_start = time.time()
            while not interrupted["flag"]:
                elapsed = int(time.time() - settle_start)
                remaining = SETTLE_WAIT - elapsed
                if remaining <= 0:
                    break
                rel = int(time.time() - start_ts)
                print(f"[{fmt_rel(rel)}] settle wait: {elapsed}s/{SETTLE_WAIT}s "
                      f"(осталось {remaining}s)")
                # Печатаем прогресс не чаще раза в минуту.
                time.sleep(min(60, remaining))

        if not interrupted["flag"]:
            now = int(time.time())
            rel = int(now - start_ts)
            try:
                count_val = fetch_one(now, COUNT_QUERY)
            except Exception as e:
                print(f"[{fmt_rel(rel)}] count(ALERTS) query failed before settle snapshot: {e}",
                      file=sys.stderr)
                count_val = None
            try:
                metrics = fetch_snapshot(now)
            except Exception as e:
                print(f"[{fmt_rel(rel)}] settle snapshot fetch failed: {e}", file=sys.stderr)
                metrics = {}
            snap = {
                "threshold": SETTLE_LABEL,
                "timestamp": now,
                "rel_seconds": rel,
                "alerts_count": int(count_val) if count_val is not None else None,
                "metrics": metrics,
            }
            snapshots.append(snap)
            print(f"[{fmt_rel(rel)}] captured settle snapshot «{SETTLE_LABEL}»")

    write_outputs(snapshots)
    print()
    print(f"JSON: {JSON_PATH}")
    print(f"TXT : {TXT_PATH}")


if __name__ == "__main__":
    main()
