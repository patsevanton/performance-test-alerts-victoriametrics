#!/bin/env python3
# -*- coding: utf-8 -*-

import yaml
import random
import os

random.seed(42)

SERVICES = [
    "api-gateway", "auth-service", "user-service", "payment-service",
    "order-service", "notification-service", "catalog-service",
    "search-service", "inventory-service", "shipping-service",
    "billing-service", "analytics-service", "recommendation-engine",
    "media-service", "cache-proxy", "messaging-broker",
    "scheduler-service", "config-server", "log-collector",
    "metric-aggregator", "audit-service", "report-generator",
    "email-dispatcher", "sms-gateway", "webhook-relay",
    "file-storage", "cdn-proxy", "rate-limiter",
    "feature-flags", "ab-testing-engine", "session-manager",
    "geo-service", "translation-service", "pdf-generator",
    "image-resizer", "video-transcoder", "chat-service",
    "feed-service", "social-graph", "content-moderation",
]

NAMESPACES = [
    "production", "staging", "platform", "infra", "monitoring",
    "data-pipeline", "ml-serving", "backend", "frontend",
    "shared-services", "payments", "logistics", "marketing",
]

TEAMS = [
    "platform-eng", "sre-team", "backend-core", "frontend-web",
    "data-eng", "ml-ops", "security-ops", "devops", "infra-team",
    "payments-team", "growth-eng", "database-team", "release-eng",
]

ENVIRONMENTS = ["production", "staging", "development", "canary"]

DB_INSTANCES = [
    "pg-main-01", "pg-main-02", "pg-replica-01", "pg-replica-02",
    "mysql-orders-01", "mysql-users-01", "mongo-catalog-01",
    "mongo-sessions-01", "redis-cache-01", "redis-cache-02",
    "redis-sessions-01", "es-logs-01", "es-logs-02",
    "clickhouse-analytics-01", "clickhouse-analytics-02",
]

KAFKA_TOPICS = [
    "user-events", "order-events", "payment-events",
    "notification-events", "audit-log", "click-stream",
    "page-views", "search-queries", "inventory-updates",
    "price-changes", "email-queue", "sms-queue", "push-notifications",
]

DASHBOARD_BASE = "https://grafana.internal/d"


def _to_camel(s):
    return "".join(w.capitalize() for w in s.replace("_", "-").split("-"))


def _rv(low, high, decimals=1):
    return round(random.uniform(low, high), decimals)


def _ri(low, high):
    return random.randint(low, high)


def _pick(*choices):
    return random.choice(choices)


# ---------------------------------------------------------------------------
# Alert generators — each returns a list of alert dicts
# ---------------------------------------------------------------------------

def node_alerts(svc, ns, team, uid):
    p = f"{_to_camel(svc)}N{uid}"
    alerts = []

    cpu_t = _pick(75, 80, 85, 90)
    alerts.append({
        "alert": f"{p}HighCpuUsage",
        "expr": f"vector({_rv(cpu_t+1, 99)}) > {cpu_t}",
        "for": _pick("5m", "10m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "node"},
        "annotations": {
            "summary": f"High CPU usage on {{{{ $labels.instance }}}} ({svc})",
            "description": f"CPU usage is above {cpu_t}%. Current value: {{{{ $value }}}}%",
        },
    })

    alerts.append({
        "alert": f"{p}CriticalCpuUsage",
        "expr": f"vector({_rv(96, 99.9)}) > 95",
        "for": _pick("2m", "5m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "node"},
        "annotations": {
            "summary": f"Critical CPU on {{{{ $labels.instance }}}} ({svc})",
            "description": "CPU usage exceeded 95%. Immediate action required.",
        },
    })

    mem_t = _pick(80, 85, 90)
    alerts.append({
        "alert": f"{p}HighMemoryUsage",
        "expr": f"vector({_rv(mem_t+1, 99)}) > {mem_t}",
        "for": _pick("5m", "10m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "node"},
        "annotations": {
            "summary": f"High memory usage on {{{{ $labels.instance }}}} ({svc})",
            "description": f"Memory usage above {mem_t}%. Current: {{{{ $value }}}}%",
        },
    })

    disk_t = _pick(80, 85, 90)
    alerts.append({
        "alert": f"{p}DiskSpaceLow",
        "expr": f"vector({_rv(disk_t+1, 98)}) > {disk_t}",
        "for": _pick("10m", "15m", "30m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "storage"},
        "annotations": {
            "summary": f"Disk space low on {{{{ $labels.instance }}}}",
            "description": f"Disk usage exceeds {disk_t}% on {{{{ $labels.mountpoint }}}}",
            "dashboard_url": f"{DASHBOARD_BASE}/node-disk",
        },
    })

    alerts.append({
        "alert": f"{p}DiskWillFillIn24h",
        "expr": f"vector({_rv(1, 20)}) > 0",
        "for": _pick("30m", "1h"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "storage"},
        "annotations": {
            "summary": "Disk predicted to fill within 24h on {{ $labels.instance }}",
            "description": "Based on growth rate, disk will be full in {{ $value }} hours",
        },
    })

    alerts.append({
        "alert": f"{p}NetworkErrors",
        "expr": f"vector({_rv(0.01, 0.5, 3)}) > 0.005",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "network"},
        "annotations": {
            "summary": "Network errors on {{ $labels.instance }}:{{ $labels.device }}",
            "description": "Network interface error rate {{ $value }}/s",
        },
    })

    alerts.append({
        "alert": f"{p}HighLoadAverage",
        "expr": f"vector({_rv(8, 32)}) > {_pick(4, 8, 16)}",
        "for": _pick("10m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "node"},
        "annotations": {
            "summary": "High load average on {{ $labels.instance }}",
            "description": "Load average is {{ $value }}, exceeding threshold",
        },
    })

    alerts.append({
        "alert": f"{p}ClockSkew",
        "expr": f"vector({_rv(0.06, 0.5, 3)}) > 0.05",
        "for": _pick("2m", "5m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "node"},
        "annotations": {
            "summary": "Clock skew detected on {{ $labels.instance }}",
            "description": "Clock offset is {{ $value }}s. NTP may be failing.",
        },
    })

    alerts.append({
        "alert": f"{p}FileDescriptorsHigh",
        "expr": f"vector({_rv(81, 95)}) > 80",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "node"},
        "annotations": {
            "summary": "File descriptor usage high on {{ $labels.instance }}",
            "description": "{{ $value }}% of file descriptors are in use",
        },
    })

    alerts.append({
        "alert": f"{p}ConntrackNearlyFull",
        "expr": f"vector({_rv(81, 99)}) > 80",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "network"},
        "annotations": {
            "summary": "Conntrack table nearly full on {{ $labels.instance }}",
            "description": "Conntrack table usage is at {{ $value }}%",
        },
    })

    return alerts


def kubernetes_alerts(svc, ns, team, uid):
    p = f"{_to_camel(svc)}K{uid}"
    alerts = []

    alerts.append({
        "alert": f"{p}PodCrashLooping",
        "expr": f"vector({_rv(1, 10, 2)}) > 0",
        "for": _pick("5m", "10m", "15m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "kubernetes"},
        "annotations": {
            "summary": f"Pod crash looping in {ns}/{{{{ $labels.pod }}}}",
            "description": f"Pod {{{{ $labels.pod }}}} in {ns} is restarting {{{{ $value }}}} times/10min",
        },
    })

    alerts.append({
        "alert": f"{p}PodNotReady",
        "expr": "vector(1) == 1",
        "for": _pick("5m", "15m", "30m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "kubernetes"},
        "annotations": {
            "summary": f"Pod not ready: {ns}/{{{{ $labels.pod }}}}",
            "description": "Pod {{ $labels.pod }} has been non-ready for extended time",
        },
    })

    alerts.append({
        "alert": f"{p}ContainerOOMKilled",
        "expr": f"vector({_ri(1, 5)}) > 0",
        "for": _pick("0s", "1m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "kubernetes"},
        "annotations": {
            "summary": f"OOM killed: {{{{ $labels.container }}}} in {ns}",
            "description": "Container {{ $labels.container }} OOM killed {{ $value }} times",
        },
    })

    alerts.append({
        "alert": f"{p}DeploymentReplicasMismatch",
        "expr": "vector(1) > 0",
        "for": _pick("10m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "kubernetes"},
        "annotations": {
            "summary": f"Deployment replica mismatch: {{{{ $labels.deployment }}}} in {ns}",
            "description": "{{ $labels.deployment }} has {{ $value }} unavailable replicas",
        },
    })

    alerts.append({
        "alert": f"{p}HPAMaxedOut",
        "expr": "vector(1) == 1",
        "for": _pick("15m", "30m", "1h"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "kubernetes"},
        "annotations": {
            "summary": f"HPA at max replicas: {{{{ $labels.horizontalpodautoscaler }}}} in {ns}",
            "description": "HPA at max capacity for extended period. Consider increasing limits.",
        },
    })

    pvc_t = _pick(80, 85, 90)
    alerts.append({
        "alert": f"{p}PVCNearlyFull",
        "expr": f"vector({_rv(pvc_t+1, 99)}) > {pvc_t}",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "storage"},
        "annotations": {
            "summary": f"PVC {{{{ $labels.persistentvolumeclaim }}}} in {ns} nearly full",
            "description": f"PVC is {{{{ $value }}}}% full (threshold: {pvc_t}%)",
        },
    })

    alerts.append({
        "alert": f"{p}ContainerCPUThrottled",
        "expr": f"vector({_rv(26, 80)}) > 25",
        "for": _pick("5m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "kubernetes"},
        "annotations": {
            "summary": f"CPU throttled: {{{{ $labels.container }}}} in {ns}",
            "description": "Container {{ $labels.container }} throttled {{ $value }}% of the time",
        },
    })

    alerts.append({
        "alert": f"{p}ContainerMemoryNearLimit",
        "expr": f"vector({_rv(86, 99)}) > 85",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "kubernetes"},
        "annotations": {
            "summary": f"Memory near limit: {{{{ $labels.container }}}} in {ns}",
            "description": "Container using {{ $value }}% of memory limit",
        },
    })

    alerts.append({
        "alert": f"{p}JobFailed",
        "expr": "vector(1) > 0",
        "for": _pick("1m", "5m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "kubernetes"},
        "annotations": {
            "summary": f"Job failed: {{{{ $labels.job_name }}}} in {ns}",
            "description": "Kubernetes job {{ $labels.job_name }} has failed",
        },
    })

    alerts.append({
        "alert": f"{p}NodeNotReady",
        "expr": "vector(1) == 1",
        "for": _pick("5m", "10m", "15m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "kubernetes"},
        "annotations": {
            "summary": "Node {{ $labels.node }} is not ready",
            "description": "Node has been unready for extended period",
        },
    })

    return alerts


def http_alerts(svc, ns, team, uid):
    p = f"{_to_camel(svc)}H{uid}"
    alerts = []

    err_t = _pick(1, 2, 5)
    alerts.append({
        "alert": f"{p}HighErrorRate5xx",
        "expr": f"vector({_rv(err_t+0.1, 15, 2)}) > {err_t}",
        "for": _pick("2m", "5m", "10m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "http"},
        "annotations": {
            "summary": f"High 5xx error rate for {svc}",
            "description": f"{svc} has {{{{ $value }}}}% 5xx error rate (threshold: {err_t}%)",
            "dashboard_url": f"{DASHBOARD_BASE}/{svc}-overview",
        },
    })

    alerts.append({
        "alert": f"{p}HighErrorRate4xx",
        "expr": f"vector({_rv(11, 30)}) > 10",
        "for": _pick("5m", "10m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "http"},
        "annotations": {
            "summary": f"High 4xx error rate for {svc}",
            "description": f"{svc} has {{{{ $value }}}}% 4xx error rate",
        },
    })

    lat_t = _pick(500, 1000, 2000, 5000)
    alerts.append({
        "alert": f"{p}HighLatencyP99",
        "expr": f"vector({round(_rv(lat_t+100, lat_t*3, 0))}) > {lat_t}",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "http"},
        "annotations": {
            "summary": f"High p99 latency for {svc}",
            "description": f"{svc} p99 latency is {{{{ $value }}}}ms (threshold: {lat_t}ms)",
            "dashboard_url": f"{DASHBOARD_BASE}/{svc}-latency",
        },
    })

    alerts.append({
        "alert": f"{p}HighLatencyP50",
        "expr": f"vector({round(_rv(201, 800, 0))}) > 200",
        "for": _pick("5m", "10m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "http"},
        "annotations": {
            "summary": f"High median latency for {svc}",
            "description": f"{svc} median latency is {{{{ $value }}}}ms",
        },
    })

    alerts.append({
        "alert": f"{p}LowRequestRate",
        "expr": f"vector({_rv(0.1, 5, 2)}) < 10",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "http"},
        "annotations": {
            "summary": f"Abnormally low request rate for {svc}",
            "description": f"{svc} receiving only {{{{ $value }}}} req/s (expected >10)",
        },
    })

    alerts.append({
        "alert": f"{p}ConnectionPoolExhausted",
        "expr": f"vector({_rv(91, 100)}) > 90",
        "for": _pick("2m", "5m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "http"},
        "annotations": {
            "summary": f"Connection pool near exhaustion for {svc}",
            "description": f"{svc} connection pool is {{{{ $value }}}}% utilized",
        },
    })

    alerts.append({
        "alert": f"{p}HighRequestDuration",
        "expr": f"vector({_rv(3.1, 10)}) > 3",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "http"},
        "annotations": {
            "summary": f"High request duration for {svc}",
            "description": f"Average request duration is {{{{ $value }}}}s for {svc}",
        },
    })

    alerts.append({
        "alert": f"{p}HighGoroutineCount",
        "expr": f"vector({_ri(10001, 50000)}) > 10000",
        "for": _pick("10m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "runtime"},
        "annotations": {
            "summary": f"High goroutine count for {svc}",
            "description": f"{svc} has {{{{ $value }}}} goroutines, possible leak",
        },
    })

    return alerts


def database_alerts(svc, ns, team, uid):
    p = f"{_to_camel(svc)}D{uid}"
    db = random.choice(DB_INSTANCES)
    alerts = []

    conn_t = _pick(80, 85, 90)
    alerts.append({
        "alert": f"{p}HighConnections",
        "expr": f"vector({_rv(conn_t+1, 100)}) > {conn_t}",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "db_instance": db, "namespace": ns, "component": "database"},
        "annotations": {
            "summary": f"High connection usage on {db}",
            "description": f"Database {db} connection pool is {{{{ $value }}}}% utilized",
        },
    })

    lag_t = _pick(5, 10, 30)
    alerts.append({
        "alert": f"{p}ReplicationLag",
        "expr": f"vector({_rv(lag_t+1, lag_t*5)}) > {lag_t}",
        "for": _pick("2m", "5m", "10m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "db_instance": db, "namespace": ns, "component": "database"},
        "annotations": {
            "summary": f"Replication lag on {db}",
            "description": f"Replication lag is {{{{ $value }}}}s on {db} (threshold: {lag_t}s)",
        },
    })

    alerts.append({
        "alert": f"{p}SlowQueries",
        "expr": f"vector({round(_rv(11, 100, 0))}) > 10",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "db_instance": db, "namespace": ns, "component": "database"},
        "annotations": {
            "summary": f"Slow queries on {db}",
            "description": f"{{{{ $value }}}} slow queries/min on {db}",
        },
    })

    alerts.append({
        "alert": f"{p}Deadlocks",
        "expr": f"vector({_ri(1, 5)}) > 0",
        "for": _pick("1m", "5m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "db_instance": db, "namespace": ns, "component": "database"},
        "annotations": {
            "summary": f"Deadlocks detected on {db}",
            "description": f"{{{{ $value }}}} deadlocks on {db}",
        },
    })

    alerts.append({
        "alert": f"{p}LowCacheHitRatio",
        "expr": f"vector({_rv(70, 89)}) < 90",
        "for": _pick("10m", "15m", "30m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "db_instance": db, "namespace": ns, "component": "database"},
        "annotations": {
            "summary": f"Low cache hit ratio on {db}",
            "description": f"Buffer cache hit ratio is {{{{ $value }}}}% on {db}",
        },
    })

    alerts.append({
        "alert": f"{p}LongRunningTx",
        "expr": f"vector({round(_rv(301, 3600, 0))}) > 300",
        "for": _pick("1m", "5m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "db_instance": db, "namespace": ns, "component": "database"},
        "annotations": {
            "summary": f"Long-running transaction on {db}",
            "description": f"Transaction running for {{{{ $value }}}}s on {db}",
        },
    })

    alerts.append({
        "alert": f"{p}TableBloat",
        "expr": f"vector({_rv(31, 70)}) > 30",
        "for": _pick("1h", "2h"),
        "labels": {"severity": "info", "team": team, "service": svc,
                   "db_instance": db, "namespace": ns, "component": "database"},
        "annotations": {
            "summary": f"Table bloat on {db}",
            "description": f"Table bloat is {{{{ $value }}}}% on {db}. Consider VACUUM.",
        },
    })

    alerts.append({
        "alert": f"{p}HighWALSize",
        "expr": f"vector({_rv(2.1, 10)}) > 2",
        "for": _pick("10m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "db_instance": db, "namespace": ns, "component": "database"},
        "annotations": {
            "summary": f"High WAL size on {db}",
            "description": f"WAL size is {{{{ $value }}}}GB on {db}",
        },
    })

    return alerts


def queue_alerts(svc, ns, team, uid):
    p = f"{_to_camel(svc)}Q{uid}"
    topic = random.choice(KAFKA_TOPICS)
    alerts = []

    lag_t = _pick(1000, 5000, 10000)
    alerts.append({
        "alert": f"{p}ConsumerLag",
        "expr": f"vector({_ri(lag_t+100, lag_t*5)}) > {lag_t}",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "topic": topic, "namespace": ns, "component": "messaging"},
        "annotations": {
            "summary": f"Kafka consumer lag high for {topic}",
            "description": f"Consumer group {{{{ $labels.consumergroup }}}} has lag {{{{ $value }}}} on {topic}",
        },
    })

    alerts.append({
        "alert": f"{p}UnderReplicatedPartitions",
        "expr": f"vector({_ri(1, 10)}) > 0",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "messaging"},
        "annotations": {
            "summary": "Kafka under-replicated partitions detected",
            "description": "{{ $value }} partitions are under-replicated",
        },
    })

    alerts.append({
        "alert": f"{p}QueueDepthHigh",
        "expr": f"vector({_ri(10001, 100000)}) > 10000",
        "for": _pick("5m", "10m", "15m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "queue": topic, "namespace": ns, "component": "messaging"},
        "annotations": {
            "summary": f"Queue depth high for {topic}",
            "description": f"Queue {topic} has {{{{ $value }}}} pending messages",
        },
    })

    alerts.append({
        "alert": f"{p}MessageProcessingErrors",
        "expr": f"vector({_rv(0.6, 5, 2)}) > 0.5",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "topic": topic, "namespace": ns, "component": "messaging"},
        "annotations": {
            "summary": f"Message processing errors for {topic}",
            "description": f"Error rate is {{{{ $value }}}}/s for topic {topic}",
        },
    })

    alerts.append({
        "alert": f"{p}BrokerOffline",
        "expr": "vector(1) == 1",
        "for": _pick("1m", "2m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "messaging"},
        "annotations": {
            "summary": "Kafka broker {{ $labels.broker_id }} is offline",
            "description": "Kafka broker has been unreachable",
        },
    })

    return alerts


def slo_alerts(svc, ns, team, uid):
    p = f"{_to_camel(svc)}S{uid}"
    alerts = []

    slo_t = _pick(99.9, 99.95, 99.99)
    alerts.append({
        "alert": f"{p}AvailabilityBreach",
        "expr": f"vector({round(slo_t - _rv(0.1, 1, 3), 3)}) < {slo_t}",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "slo": "availability", "component": "slo"},
        "annotations": {
            "summary": f"SLO availability breach for {svc}",
            "description": f"{svc} availability is {{{{ $value }}}}% (SLO: {slo_t}%)",
            "dashboard_url": f"{DASHBOARD_BASE}/slo-{svc}",
        },
    })

    lat_slo = _pick(200, 500, 1000)
    alerts.append({
        "alert": f"{p}LatencyBreach",
        "expr": f"vector({round(lat_slo * _rv(1.1, 3, 1), 0)}) > {lat_slo}",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "slo": "latency", "component": "slo"},
        "annotations": {
            "summary": f"SLO latency breach for {svc}",
            "description": f"{svc} p99 latency is {{{{ $value }}}}ms (SLO: {lat_slo}ms)",
        },
    })

    alerts.append({
        "alert": f"{p}ErrorBudgetBurnRate",
        "expr": f"vector({_rv(2, 10, 2)}) > 1",
        "for": _pick("5m", "1h"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "slo"},
        "annotations": {
            "summary": f"Error budget burn rate high for {svc}",
            "description": f"{svc} burning error budget at {{{{ $value }}}}x sustainable rate",
        },
    })

    alerts.append({
        "alert": f"{p}ThroughputBreach",
        "expr": f"vector({_rv(1, 50)}) < {_pick(100, 200, 500)}",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "slo": "throughput", "component": "slo"},
        "annotations": {
            "summary": f"SLO throughput breach for {svc}",
            "description": f"{svc} throughput is {{{{ $value }}}} req/s, below SLO",
        },
    })

    return alerts


def security_alerts(svc, ns, team, uid):
    p = f"{_to_camel(svc)}X{uid}"
    alerts = []

    alerts.append({
        "alert": f"{p}CertExpiringIn30Days",
        "expr": f"vector({_ri(5, 29)}) < 30",
        "for": _pick("1h", "6h"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "security"},
        "annotations": {
            "summary": "TLS certificate expiring soon for {{ $labels.host }}",
            "description": "Certificate for {{ $labels.host }} expires in {{ $value }} days",
        },
    })

    alerts.append({
        "alert": f"{p}CertExpiringIn7Days",
        "expr": f"vector({_ri(1, 6)}) < 7",
        "for": _pick("10m", "30m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "security"},
        "annotations": {
            "summary": "TLS certificate expiring very soon for {{ $labels.host }}",
            "description": "Certificate expires in {{ $value }} days. Immediate renewal required!",
        },
    })

    alerts.append({
        "alert": f"{p}HighFailedAuthAttempts",
        "expr": f"vector({round(_rv(11, 100, 0))}) > 10",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "security"},
        "annotations": {
            "summary": f"High failed authentication rate for {svc}",
            "description": f"{{{{ $value }}}} failed auth attempts/min for {svc}",
        },
    })

    alerts.append({
        "alert": f"{p}SuspiciousNetworkActivity",
        "expr": f"vector({_rv(1.1, 10, 1)}) > 1",
        "for": _pick("2m", "5m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "security"},
        "annotations": {
            "summary": f"Suspicious network activity for {svc}",
            "description": "{{ $value }} suspicious connections/min detected",
        },
    })

    return alerts


def application_alerts(svc, ns, team, uid):
    p = f"{_to_camel(svc)}A{uid}"
    alerts = []

    alerts.append({
        "alert": f"{p}UpstreamTimeout",
        "expr": f"vector({_rv(1.1, 10, 2)}) > 1",
        "for": _pick("2m", "5m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "application"},
        "annotations": {
            "summary": f"Upstream timeouts for {svc}",
            "description": f"{svc} experiencing {{{{ $value }}}} upstream timeouts/min",
        },
    })

    alerts.append({
        "alert": f"{p}CircuitBreakerOpen",
        "expr": "vector(1) == 1",
        "for": _pick("1m", "2m", "5m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "application"},
        "annotations": {
            "summary": f"Circuit breaker open for {svc}",
            "description": f"Circuit breaker to {{{{ $labels.upstream }}}} is open in {svc}",
        },
    })

    alerts.append({
        "alert": f"{p}HighGCPause",
        "expr": f"vector({_rv(0.51, 2, 2)}) > 0.5",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "runtime"},
        "annotations": {
            "summary": f"High GC pause time for {svc}",
            "description": f"{svc} GC pause is {{{{ $value }}}}s",
        },
    })

    alerts.append({
        "alert": f"{p}HighHeapUsage",
        "expr": f"vector({_rv(86, 99)}) > 85",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "runtime"},
        "annotations": {
            "summary": f"High heap usage for {svc}",
            "description": f"{svc} heap usage is {{{{ $value }}}}%",
        },
    })

    alerts.append({
        "alert": f"{p}ConfigReloadFailed",
        "expr": "vector(1) == 1",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "application"},
        "annotations": {
            "summary": f"Config reload failed for {svc}",
            "description": f"Last configuration reload failed for {svc}",
        },
    })

    alerts.append({
        "alert": f"{p}PanicDetected",
        "expr": f"vector({_ri(1, 3)}) > 0",
        "for": _pick("0s", "1m"),
        "labels": {"severity": "critical", "team": team, "service": svc,
                   "namespace": ns, "component": "application"},
        "annotations": {
            "summary": f"Panic/crash detected in {svc}",
            "description": f"{{{{ $value }}}} panics detected in {svc}",
        },
    })

    alerts.append({
        "alert": f"{p}ServiceSaturated",
        "expr": f"vector({_rv(81, 100)}) > 80",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "application"},
        "annotations": {
            "summary": f"{svc} is saturated",
            "description": f"{svc} saturation is at {{{{ $value }}}}%",
        },
    })

    alerts.append({
        "alert": f"{p}QueueBackpressure",
        "expr": f"vector({_rv(81, 100)}) > 80",
        "for": _pick("5m", "10m"),
        "labels": {"severity": "warning", "team": team, "service": svc,
                   "namespace": ns, "component": "application"},
        "annotations": {
            "summary": f"Queue backpressure for {svc}",
            "description": f"{svc} internal queue is {{{{ $value }}}}% full",
        },
    })

    return alerts


# ---------------------------------------------------------------------------
# Group and VMRule assembly
# ---------------------------------------------------------------------------

ALL_GENERATORS = [
    ("node-health", node_alerts),
    ("kubernetes", kubernetes_alerts),
    ("http-service", http_alerts),
    ("database", database_alerts),
    ("messaging", queue_alerts),
    ("slo", slo_alerts),
    ("security", security_alerts),
    ("application", application_alerts),
]


def fill_group(group_name, gen_func, count, vmrule_idx):
    alerts = []
    iteration = 0
    while len(alerts) < count:
        iteration += 1
        svc = random.choice(SERVICES)
        ns = random.choice(NAMESPACES)
        team = random.choice(TEAMS)
        uid = f"{vmrule_idx}x{iteration}"
        batch = gen_func(svc, ns, team, uid)
        alerts.extend(batch)
    return alerts[:count]


def generate_vmrule(vmrule_idx, num_alerts):
    num_groups = random.randint(4, 6)
    selected = random.sample(ALL_GENERATORS, min(num_groups, len(ALL_GENERATORS)))

    base = num_alerts // len(selected)
    remainder = num_alerts % len(selected)

    groups = []
    for i, (gname, gfunc) in enumerate(selected):
        cnt = base + (1 if i < remainder else 0)
        interval = random.choice(["30s", "1m", "2m"])
        rules = fill_group(gname, gfunc, cnt, vmrule_idx)
        groups.append({
            "name": f"{gname}-{vmrule_idx}",
            "interval": interval,
            "rules": rules,
        })

    team = random.choice(TEAMS)
    env = random.choice(ENVIRONMENTS)
    svc_scope = random.choice(SERVICES)

    return {
        "apiVersion": "operator.victoriametrics.com/v1beta1",
        "kind": "VMRule",
        "metadata": {
            "name": f"vmrule-{str(vmrule_idx).zfill(5)}",
            "labels": {
                "app.kubernetes.io/name": "monitoring-rules",
                "app.kubernetes.io/part-of": "observability",
                "monitoring/team": team,
                "monitoring/environment": env,
                "monitoring/scope": svc_scope,
            },
        },
        "spec": {
            "groups": groups,
        },
    }


def main():
    # 100 alerts/file → 5× fewer files than 20/file for the same total alert count.
    num_vmrules = 500
    alerts_per_vmrule = 100

    output_dir = "vmrules"
    os.makedirs(output_dir, exist_ok=True)

    print("Generating realistic VMRule YAML files...")
    for idx in range(1, num_vmrules + 1):
        vmrule = generate_vmrule(idx, alerts_per_vmrule)
        path = os.path.join(output_dir, f"vmrule-{str(idx).zfill(5)}.yaml")
        with open(path, "w") as f:
            yaml.dump(vmrule, f, sort_keys=False, default_flow_style=False,
                      allow_unicode=True)
        if idx % 100 == 0:
            print(f"  {idx}/{num_vmrules}")

    print(f"\nDone! {num_vmrules} files in '{output_dir}/'")
    print(f"Alerts per VMRule: {alerts_per_vmrule}")
    print(f"Total alerts: {num_vmrules * alerts_per_vmrule:,}")


if __name__ == "__main__":
    main()
