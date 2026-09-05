#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMES_FILE="${NAMES_FILE:-$SCRIPT_DIR/app-names.txt}"
TARGET_APPS="${TARGET_APPS:-800}"

if [ ! -f "$NAMES_FILE" ]; then
  echo "Error: $NAMES_FILE not found."
  exit 1
fi

mapfile -t all_names < "$NAMES_FILE"
available=${#all_names[@]}
if [ "$available" -lt "$TARGET_APPS" ]; then
  echo "Error: $NAMES_FILE contains $available names, but TARGET_APPS=$TARGET_APPS."
  exit 1
fi

apps=("${all_names[@]:0:TARGET_APPS}")
apps_tmp="$(mktemp)"
trap 'rm -f "$apps_tmp"' EXIT
printf '%s\n' "${apps[@]}" > "$apps_tmp"

echo "Namespace mode: per-app (namespace name = app name)"
echo "Target apps: $TARGET_APPS"

echo "=== Helm releases ==="
helm list -A --no-headers | awk 'NR==FNR { ns[$1]=1; next } ns[$2] { c++ } END { print c+0 }' "$apps_tmp" -
echo "total helm releases"

echo ""
echo "=== Pod status ==="
kubectl get pods -A --no-headers | awk '
NR==FNR { ns[$1]=1; next }
ns[$1] { status[$4]++ }
END {
  for (s in status) printf "%8d %s\n", status[s], s
}
' "$apps_tmp" - | sort -rn

echo ""
echo "=== Resource usage ==="
kubectl top pods -A --no-headers 2>/dev/null | awk '
function cpu_to_m(v,  n) {
  n=v
  sub(/m$/, "", n)
  return n + 0
}
function mem_to_mi(v,  n) {
  n=v
  if (n ~ /Ki$/) { sub(/Ki$/, "", n); return (n + 0) / 1024 }
  if (n ~ /Mi$/) { sub(/Mi$/, "", n); return n + 0 }
  if (n ~ /Gi$/) { sub(/Gi$/, "", n); return (n + 0) * 1024 }
  return 0
}
NR==FNR { ns[$1]=1; next }
ns[$1] {
  cpu_m += cpu_to_m($3)
  mem_mi += mem_to_mi($4)
  count++
}
END {
  printf "Pods: %d, Total CPU: %.0fm, Total Memory: %.1fMi\n", count, cpu_m, mem_mi
}
' "$apps_tmp" - || echo "(metrics-server not available)"

echo ""
echo "=== VMRule count ==="
kubectl get vmrule -A --no-headers 2>/dev/null | awk 'NR==FNR { ns[$1]=1; next } ns[$1] { c++ } END { print c+0 }' "$apps_tmp" -
echo "VMRule objects"

echo ""
echo "=== VMServiceScrape count ==="
kubectl get vmservicescrape -A --no-headers 2>/dev/null | awk 'NR==FNR { ns[$1]=1; next } ns[$1] { c++ } END { print c+0 }' "$apps_tmp" -
echo "VMServiceScrape objects"
