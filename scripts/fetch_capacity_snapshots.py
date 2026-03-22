#!/usr/bin/env python3
"""Fetch Capacity Planning table rows from VictoriaMetrics instant queries at chosen times."""
import json
import ssl
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Match curl -sk (ingress may use a self-signed cert)
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

BASE = "https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query"

# Unix time: ближайший срез к count(ALERTS)≈N (|Δ|≤150), шаг query_range 15s, прогон 2026-03-22 UTC.
TARGETS = {
    5000: 1774168440,
    10000: 1774169520,
    15000: 1774171290,
    20000: 1774172640,
    25000: 1774174170,
    30000: 1774174830,
    35000: 1774176015,
    40000: 1774178280,
    45000: 1774179990,
    # count(ALERTS)≈50k; момент согласован с ростом vmalert/vmselect (см. README Capacity Planning)
    50000: 1774181250,
}

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
    # Exclude +Inf bucket — otherwise p99 can collapse to the largest finite bound (e.g. 60s).
    "apiserver_p99": 'histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket{le!="+Inf"}[5m])) by (le))',
    "apiserver_cpu": 'sum(rate(process_cpu_seconds_total{job=~".*apiserver.*"}[5m]))*1000',
    "vmselect_rps": 'sum(rate(vm_http_requests_total{job="vmselect-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vmstorage_rps": 'sum(rate(vm_http_requests_total{job="vmstorage-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vminsert_rps": 'sum(rate(vm_http_requests_total{job="vminsert-vmks-victoria-metrics-k8s-stack"}[5m]))',
    "vmalert_iter_max": "max(vmalert_iteration_duration_seconds)",
    "vmalert_exec_err": "sum(vmalert_execution_errors_total)",
    "vmalert_iter_miss": "sum(vmalert_iteration_missed_total)",
    "vmalert_rw_rps": "sum(rate(vmalert_remotewrite_total[5m]))",
}


def qval(data: dict) -> float | None:
    r = data.get("data", {}).get("result") or []
    if not r:
        return None
    v = r[0].get("value", [None, None])[1]
    if v is None:
        return None
    return float(v)


def fetch(t: int, query: str) -> float | None:
    params = urllib.parse.urlencode({"query": query, "time": str(t)})
    url = f"{BASE}?{params}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120, context=_SSL) as resp:
        return qval(json.load(resp))


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


def fmt_iter(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:.2f} сек"


def main() -> None:
    rows = []
    for label in sorted(TARGETS.keys()):
        t = TARGETS[label]
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = {k: ex.submit(fetch, t, q) for k, q in QUERIES.items()}
            m = {k: futs[k].result() for k in QUERIES}
        rows.append((label, t, m))

    for label, t, m in rows:
        print(f"=== ALERTS~{label} t={t} ===")
        print(
            "CPU",
            fmt_cpu_m(m["vmalert_cpu"]),
            fmt_cpu_m(m["vmstorage_cpu"]),
            fmt_cpu_m(m["vmselect_cpu"]),
            fmt_cpu_m(m["vminsert_cpu"]),
            fmt_cpu_m(m["vmagent_cpu"]),
            fmt_cpu_m(m["operator_cpu"]),
        )
        print(
            "MEM",
            fmt_mem_mi(m["vmalert_mem"]),
            fmt_mem_mi(m["vmstorage_mem"]),
            fmt_mem_mi(m["vmselect_mem"]),
            fmt_mem_mi(m["vminsert_mem"]),
            fmt_mem_mi(m["vmagent_mem"]),
            fmt_mem_mi(m["operator_mem"]),
        )
        p99 = m["apiserver_p99"]
        print(
            "RPS",
            fmt_rps(m["apiserver_rps"]),
            fmt_p99_sec(p99),
            fmt_cpu_m(m["apiserver_cpu"]),
            fmt_rps(m["vmselect_rps"]),
            fmt_rps(m["vmstorage_rps"], 1),
            fmt_rps(m["vminsert_rps"]),
            fmt_iter(m["vmalert_iter_max"]),
            f"{int(m['vmalert_exec_err']) if m['vmalert_exec_err'] is not None else '—'}",
            f"{int(m['vmalert_iter_miss']) if m['vmalert_iter_miss'] is not None else '—'}",
            fmt_rps(m["vmalert_rw_rps"], 1),
        )
        print()


if __name__ == "__main__":
    main()
