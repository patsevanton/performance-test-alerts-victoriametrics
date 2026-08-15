#!/usr/bin/env python3
"""Сбор снимков Capacity Planning из VictoriaMetrics во время живого теста.

Запускается ОДИН раз в начале теста (параллельно с scripts/deploy-apps.sh). Скрипт
опрашивает `count(ALERTS)` каждые POLL_INTERVAL секунд и, как только значение
пересекает очередной порог из TARGETS (500, 5000, ..., 50000), фиксирует снимок всех
метрик из QUERIES через instant-запрос с `time=now`.

Скрипт ожидает достижения всех порогов бесконечно; для досрочной остановки нажмите
Ctrl-C — будут выгружены снимки, собранные к этому моменту.

Результаты:
  - capacity_snapshots.json : сырые значения метрик по порогам
  - capacity_snapshots.txt  : форматированная таблица
  - stdout                  : та же таблица

Переменные окружения (значения по умолчанию взяты из scripts/deploy-apps.sh):
  TARGET_APPS       по умолчанию 1350 — число разворачиваемых приложений
  ALERTS_PER_APP    по умолчанию 50   — алертов на одно приложение
  BASE_ALERTS_COUNT по умолчанию 10   — базовых алертов на приложение (информационно)
  POLL_INTERVAL     по умолчанию 10   — секунды между опросами count(ALERTS)
  VMSELECT_URL      по умолчанию берётся из `terraform output -raw vmselect_url`
                    (FQDN формируется через sslip.io из публичного IP ingress-nginx).
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
TARGETS = [500, 5000, 10000, 15000, 20000, 25000, 30000, 35000, 40000, 45000, 50000]

# Параметры деплоя (значения по умолчанию из scripts/deploy-apps.sh).
TARGET_APPS = int(os.environ.get("TARGET_APPS", "1350"))
ALERTS_PER_APP = int(os.environ.get("ALERTS_PER_APP", "50"))
BASE_ALERTS_COUNT = int(os.environ.get("BASE_ALERTS_COUNT", "10"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))

EXPECTED_MAX_ALERTS = TARGET_APPS * ALERTS_PER_APP

QUERIES = {
    "vmalert_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmalert-vmks-victoria-metrics-k8s-stack-.*"}[2m]))*1000',
    "vmstorage_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmstorage-vmks-victoria-metrics-k8s-stack-.*"}[2m]))*1000',
    "vmselect_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmselect-vmks-victoria-metrics-k8s-stack-.*"}[2m]))*1000',
    "vminsert_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vminsert-vmks-victoria-metrics-k8s-stack-.*"}[2m]))*1000',
    "vmagent_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmagent-vmks-victoria-metrics-k8s-stack-.*"}[2m]))*1000',
    "operator_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmks-victoria-metrics-operator-.*"}[2m]))*1000',
    "vmalert_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmalert-vmks-victoria-metrics-k8s-stack-.*"})/1024/1024',
    "vmstorage_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmstorage-vmks-victoria-metrics-k8s-stack-.*"})/1024/1024',
    "vmselect_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmselect-vmks-victoria-metrics-k8s-stack-.*"})/1024/1024',
    "vminsert_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vminsert-vmks-victoria-metrics-k8s-stack-.*"})/1024/1024',
    "vmagent_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmagent-vmks-victoria-metrics-k8s-stack-.*"})/1024/1024',
    "operator_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmks-victoria-metrics-operator-.*"})/1024/1024',
    "apiserver_rps": "sum(rate(apiserver_request_total[5m]))",
    # Исключаем бакет +Inf — иначе p99 может схлопнуться до наибольшей конечной границы (напр. 60s).
    "apiserver_p99": 'histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket{le!="+Inf"}[5m])) by (le))',
    "apiserver_cpu": 'sum(rate(process_cpu_seconds_total{job=~".*apiserver.*"}[5m]))*1000',
    "vmselect_rps": 'sum(rate(vm_http_requests_total{job="vmselect-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vmstorage_rps": 'sum(rate(vm_http_requests_total{job="vmstorage-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vminsert_rps": 'sum(rate(vm_http_requests_total{job="vminsert-vmks-victoria-metrics-k8s-stack"}[5m]))',
    # Дополнительные метрики для таблицы "grew with load" в README.
    "vmalert_iter_max": 'max(vmalert_iteration_duration_seconds{namespace="vmks",pod=~"vmalert-vmks-victoria-metrics-k8s-stack-.*"})',
    "vmselect_concurrent": 'max(vm_concurrent_select_current{job="vmselect-vmks-victoria-metrics-k8s-stack"})',
    "vmagent_scrape_samples_vmalert": 'max(scrape_samples_scraped{job="vmalert-vmks-victoria-metrics-k8s-stack"})',
}

COUNT_QUERY = "count(ALERTS)"

# Куда писать результаты (рядом со скриптом).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(SCRIPT_DIR, "capacity_snapshots.json")
TXT_PATH = os.path.join(SCRIPT_DIR, "capacity_snapshots.txt")


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
        lines.append(f"=== ALERTS~{label} (measured={measured}) {rel} ts={ts} ===")
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
            continue

        time.sleep(POLL_INTERVAL)

    if interrupted["flag"]:
        print("\nInterrupted by user.", file=sys.stderr)

    missed = [t for t in TARGETS if t not in {s["threshold"] for s in snapshots}]
    if missed:
        print(f"Thresholds not reached: {missed}", file=sys.stderr)

    write_outputs(snapshots)
    print()
    print(f"JSON: {JSON_PATH}")
    print(f"TXT : {TXT_PATH}")


if __name__ == "__main__":
    main()
