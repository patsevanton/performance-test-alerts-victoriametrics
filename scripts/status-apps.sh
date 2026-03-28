#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-load-test}"

echo "=== Helm releases ==="
helm list -n "$NAMESPACE" --no-headers | wc -l
echo "total helm releases"

echo ""
echo "=== Pod status ==="
kubectl get pods -n "$NAMESPACE" --no-headers | awk '{print $3}' | sort | uniq -c | sort -rn

echo ""
echo "=== Resource usage ==="
kubectl top pods -n "$NAMESPACE" --no-headers 2>/dev/null | awk '
  { cpu+=$2; mem+=$3; count++ }
  END { printf "Pods: %d, Total CPU: %s, Total Memory: %s\n", count, cpu, mem }
' || echo "(metrics-server not available)"

echo ""
echo "=== VMRule count ==="
kubectl get vmrule -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l
echo "VMRule objects"

echo ""
echo "=== VMServiceScrape count ==="
kubectl get vmservicescrape -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l
echo "VMServiceScrape objects"
