#!/usr/bin/env python3
"""Досбор пропущенных снимков Capacity Planning из исторических данных TSDB.

Прогон deploy_and_snapshot.py частично потерял значения метрик: начиная с порога
~15000 и до конца vmselect отдавал 429 (search.maxConcurrentRequests=32), из-за
чего fetch_snapshot возвращал пустой dict и в capacity_snapshots.json остались
null-значения. Сами ряды в vmstorage сохранились, поэтому недостающие значения
можно восстановить историческими запросами.

Скрипт работает по query_range: для каждой метрики из QUERIES выполняется ОДИН
запрос по всему окну теста (шаг 60 с), после чего для каждого снимка берётся
ближайший к timestamp снимка отсчёт (в пределах 120 с). Это резко снижает число
запросов к vmselect относительно instant-запросов по каждому порогу.

Результат: перезаписывает capacity_snapshots.json (заполняя null-метрики) и
capacity_snapshots.txt (форматированная таблица). Существующие не-null значения
не трогаются.

Запускать ПОСЛЕ завершения deploy_and_snapshot.py. Кластер продолжает жить,
поэтому выполнение возможно на фоне.
"""
import json
import os
import ssl
import time
import urllib.parse
import urllib.request

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
JSON_PATH = os.path.join(SCRIPT_DIR, "capacity_snapshots.json")
TXT_PATH = os.path.join(SCRIPT_DIR, "capacity_snapshots.txt")

BASE = os.environ.get("VMSELECT_URL", "").rstrip("/")
if not BASE:
    import subprocess
    try:
        out = subprocess.run(
            ["terraform", "output", "-raw", "vmselect_url"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        if out.returncode == 0:
            BASE = out.stdout.strip()
    except FileNotFoundError:
        pass
if not BASE:
    raise SystemExit("VMSELECT_URL не определён")
BASE = BASE + "/select/0/prometheus/api/v1/query_range"

STEP = 60
MAX_DIST = 120  # с — максимальное расстояние до отсчёта для зачисления

QUERIES = {
    "vmalert_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmalert-.*"}[2m]))*1000',
    "vmstorage_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmstorage-.*"}[2m]))*1000',
    "vmselect_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmselect-.*"}[2m]))*1000',
    "vminsert_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vminsert-.*"}[2m]))*1000',
    "vmagent_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmagent-.*"}[2m]))*1000',
    "operator_cpu": 'avg(rate(pod_cpu_usage_seconds_total{namespace="vmks",pod=~"vmks-victoria-metrics-operator-.*"}[2m]))*1000',
    "vmalert_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmalert-.*"})/1024/1024',
    "vmstorage_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmstorage-.*"})/1024/1024',
    "vmselect_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmselect-.*"})/1024/1024',
    "vminsert_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vminsert-.*"})/1024/1024',
    "vmagent_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmagent-.*"})/1024/1024',
    "operator_mem": 'avg(pod_memory_working_set_bytes{namespace="vmks",pod=~"vmks-victoria-metrics-operator-.*"})/1024/1024',
    "apiserver_rps": 'sum(rate(apiserver_request_total[5m]))',
    "apiserver_p99": 'histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket{le!="+Inf"}[5m])) by (le))',
    "apiserver_cpu": 'sum(rate(process_cpu_seconds_total{job=~".*apiserver.*"}[5m]))*1000',
    "vmselect_rps": 'sum(rate(vm_http_requests_total{job="vmselect-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vmstorage_rps": 'sum(rate(vm_http_requests_total{job="vmstorage-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vminsert_rps": 'sum(rate(vm_http_requests_total{job="vminsert-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vmalert_iter_max": 'max(vmalert_iteration_duration_seconds{namespace="vmks",pod=~"vmalert-.*"})',
    "vmselect_concurrent": 'max(vm_concurrent_select_current{job="vmselect-vmks-victoria-metrics-k8s-stack"})',
    "vmagent_scrape_samples_vmalert": 'max(scrape_samples_scraped{job="vmalert-vmks-victoria-metrics-k8s-stack"})',
    "vm_rows_scanned_total": 'sum(rate(vm_rows_scanned_per_query_sum{job="vmselect-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vm_rows_scanned_vmstorage": 'sum(rate(vm_rows_scanned_per_query_sum{job="vmstorage-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vm_cache_misses_vmselect": 'sum(rate(vm_cache_misses_total{job="vmselect-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vm_cache_misses_vmstorage": 'sum(rate(vm_cache_misses_total{job="vmstorage-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vm_search_latency_max": 'max(vm_request_duration_seconds{job=~"vmselect-vmks-victoria-metrics-k8s-stack|vmstorage-vmks-victoria-metrics-k8s-stack"})',
    "vm_evaluation_duration_max": 'max(vmalert_iteration_duration_seconds{job="vmalert-vmks-victoria-metrics-k8s-stack"})',
    "vm_evaluation_count_rate": 'sum(rate(vmalert_execution_total{job="vmalert-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vmalert_iter_p99": 'histogram_quantile(0.99, sum(rate(vmalert_iteration_duration_seconds_bucket{job="vmalert-vmks-victoria-metrics-k8s-stack"}[5m])) by (le))',
}


def fetch_series(query: str, start: int, end: int) -> list:
    """Возвращает [(ts, float), ...] для единственного результата (или пустой)."""
    params = urllib.parse.urlencode({
        "query": query, "start": str(start), "end": str(end), "step": str(STEP),
    })
    url = f"{BASE}?{params}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=180, context=_SSL) as resp:
        data = json.load(resp)
    r = data.get("data", {}).get("result") or []
    if not r:
        return []
    vals = r[0].get("values", [])
    out = []
    for ts, v in vals:
        if v == "NaN":
            continue
        try:
            out.append((int(float(ts)), float(v)))
        except (ValueError, TypeError):
            continue
    return out


def nearest(series: list, ts: int):
    best = None
    best_dist = None
    for s_ts, v in series:
        d = abs(s_ts - ts)
        if best_dist is None or d < best_dist:
            best_dist = d
            best = v
    if best_dist is not None and best_dist <= MAX_DIST:
        return best
    return None


def fmt_cpu_m(x):
    return "—" if x is None else f"{round(x)}m"


def fmt_mem_mi(x):
    return "—" if x is None else f"{round(x)}Mi"


def fmt_rps(x, nd=1):
    return "—" if x is None else f"{x:.{nd}f}".rstrip("0").rstrip(".")


def fmt_p99_sec(x):
    return "—" if x is None else f"{round(x * 1000)} ms"


def fmt_sec(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}s".rstrip("0").rstrip(".")


def fmt_count(x):
    return "—" if x is None else f"{round(x)}"


def fmt_rel(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"T+{h:02d}:{m:02d}:{s:02d}"


def render_table(snapshots, params, rule_state):
    lines = []
    lines.append(
        f"TARGET_APPS={params.get('TARGET_APPS')} "
        f"ALERTS_PER_APP={params.get('ALERTS_PER_APP')} "
        f"BASE_ALERTS_COUNT={params.get('BASE_ALERTS_COUNT')} "
        f"expected_max_alerts={params.get('expected_max_alerts')}")
    if rule_state:
        lines.append(
            f"vmrule_count={rule_state.get('vmrule_count')} "
            f"rules_per_vmrule={rule_state.get('rules_per_vmrule')}")
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
        lines.append("CPU " + " ".join([
            fmt_cpu_m(m.get("vmalert_cpu")), fmt_cpu_m(m.get("vmstorage_cpu")),
            fmt_cpu_m(m.get("vmselect_cpu")), fmt_cpu_m(m.get("vminsert_cpu")),
            fmt_cpu_m(m.get("vmagent_cpu")), fmt_cpu_m(m.get("operator_cpu")),
        ]))
        lines.append("MEM " + " ".join([
            fmt_mem_mi(m.get("vmalert_mem")), fmt_mem_mi(m.get("vmstorage_mem")),
            fmt_mem_mi(m.get("vmselect_mem")), fmt_mem_mi(m.get("vminsert_mem")),
            fmt_mem_mi(m.get("vmagent_mem")), fmt_mem_mi(m.get("operator_mem")),
        ]))
        lines.append("RPS " + " ".join([
            fmt_rps(m.get("apiserver_rps")), fmt_p99_sec(m.get("apiserver_p99")),
            fmt_cpu_m(m.get("apiserver_cpu")), fmt_rps(m.get("vmselect_rps")),
            fmt_rps(m.get("vmstorage_rps"), 1), fmt_rps(m.get("vminsert_rps")),
        ]))
        lines.append("GROWTH " + " ".join([
            fmt_sec(m.get("vmalert_iter_max")),
            fmt_count(m.get("vmselect_concurrent")),
            fmt_count(m.get("vmagent_scrape_samples_vmalert")),
        ]))
        lines.append("HEAVY " + " ".join([
            fmt_count(m.get("vm_rows_scanned_total")),
            fmt_count(m.get("vm_rows_scanned_vmstorage")),
            fmt_count(m.get("vm_cache_misses_vmselect")),
            fmt_count(m.get("vm_cache_misses_vmstorage")),
            fmt_sec(m.get("vm_search_latency_max")),
            fmt_sec(m.get("vm_evaluation_duration_max")),
            fmt_count(m.get("vm_evaluation_count_rate")),
            fmt_sec(m.get("vmalert_iter_p99")),
        ]))
        lines.append("")
    return "\n".join(lines)


def main():
    with open(JSON_PATH) as f:
        payload = json.load(f)

    snapshots = payload["snapshots"]
    params = payload.get("params", {})
    rule_state = payload.get("rule_count")

    ts_min = min(s["timestamp"] for s in snapshots)
    ts_max = max(s["timestamp"] for s in snapshots)

    # Какие метрики вообще нужно дособрать (есть хотя бы один null/отсутствие).
    needed = set()
    for snap in snapshots:
        m = snap.get("metrics", {})
        for k in QUERIES:
            if k not in m or m[k] is None:
                needed.add(k)

    if not needed:
        print("Все метрики уже заполнены — backfill не требуется.")
        return

    print(f"Окно теста: {ts_min}..{ts_max} ({ts_max - ts_min}s)")
    print(f"Нужно дособрать {len(needed)} метрик: {sorted(needed)}")
    print()

    series_cache = {}
    for name in sorted(needed):
        q = QUERIES[name]
        series = None
        for attempt in range(5):
            try:
                series = fetch_series(q, ts_min - 60, ts_max + 60)
                break
            except Exception as e:
                print(f"  [{name}] попытка {attempt + 1}/5: {e}", flush=True)
                if attempt < 4:
                    time.sleep(20)
        if series is None:
            print(f"  [{name}] НЕ УДАЛОСЬ — пропускаю", flush=True)
            continue
        series_cache[name] = series
        filled = 0
        for snap in snapshots:
            m = snap.setdefault("metrics", {})
            if m.get(name) is None:
                v = nearest(series, snap["timestamp"])
                if v is not None:
                    m[name] = v
                    filled += 1
        print(f"  [{name}] точек={len(series)} заполнено снимков={filled}",
              flush=True)

    # Перезапись JSON и TXT.
    with open(JSON_PATH, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    table = render_table(snapshots, params, rule_state)
    with open(TXT_PATH, "w") as f:
        f.write(table + "\n")
    print()
    print(table)
    print()
    print(f"JSON: {JSON_PATH}")
    print(f"TXT : {TXT_PATH}")


if __name__ == "__main__":
    main()
