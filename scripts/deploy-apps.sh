#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CHART_PATH="${CHART_PATH:-$REPO_ROOT/chart}"
NAMES_FILE="${NAMES_FILE:-$SCRIPT_DIR/app-names.txt}"
PARALLEL="${PARALLEL:-10}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/patsevanton/performance-test-alerts-victoriametrics}"
IMAGE_TAG="${IMAGE_TAG:-1.0.0}"
TARGET_APPS="${TARGET_APPS:-1000}"
ALERTS_PER_APP="${ALERTS_PER_APP:-50}"
BASE_ALERTS_COUNT="${BASE_ALERTS_COUNT:-10}"
STAGE_SIZES="${STAGE_SIZES:-400,300,200,100}"
STAGE_PAUSES="${STAGE_PAUSES:-15,30,45,60}"

if [ ! -f "$NAMES_FILE" ]; then
  echo "Error: $NAMES_FILE not found. Run generate-app-names.sh first."
  exit 1
fi

SYNTHETIC_ALERTS_COUNT="${SYNTHETIC_ALERTS_COUNT:-$((ALERTS_PER_APP - BASE_ALERTS_COUNT))}"

if [ "$ALERTS_PER_APP" -lt "$BASE_ALERTS_COUNT" ]; then
  echo "Error: ALERTS_PER_APP ($ALERTS_PER_APP) must be >= BASE_ALERTS_COUNT ($BASE_ALERTS_COUNT)."
  exit 1
fi

mapfile -t all_names < "$NAMES_FILE"
available=${#all_names[@]}
if [ "$available" -lt "$TARGET_APPS" ]; then
  echo "Error: $NAMES_FILE contains $available names, but TARGET_APPS=$TARGET_APPS."
  exit 1
fi

apps=("${all_names[@]:0:TARGET_APPS}")
total_alerts=$((TARGET_APPS * ALERTS_PER_APP))

IFS=',' read -r -a stage_sizes <<< "$STAGE_SIZES"
IFS=',' read -r -a stage_pauses <<< "$STAGE_PAUSES"

if [ "${#stage_sizes[@]}" -ne 4 ] || [ "${#stage_pauses[@]}" -ne 4 ]; then
  echo "Error: STAGE_SIZES and STAGE_PAUSES must each contain exactly 4 comma-separated values."
  exit 1
fi

sum=0
for size in "${stage_sizes[@]}"; do
  sum=$((sum + size))
done
if [ "$sum" -ne "$TARGET_APPS" ]; then
  echo "Error: sum(STAGE_SIZES)=$sum must equal TARGET_APPS=$TARGET_APPS."
  exit 1
fi

echo "Deploying $TARGET_APPS apps (parallelism: $PARALLEL)"
echo "Alerts per app: $ALERTS_PER_APP (base: $BASE_ALERTS_COUNT, synthetic: $SYNTHETIC_ALERTS_COUNT)"
echo "Planned total alerts: $total_alerts"
echo "Namespace mode: per-app (namespace name = app name)"

deploy_app() {
  local name="$1"
  local app_namespace="$name"
  echo "[$(date +%H:%M:%S)] Installing $name into namespace $app_namespace ..."
  kubectl create namespace "$app_namespace" 2>/dev/null || true
  helm upgrade --install "$name" "$CHART_PATH" \
    --namespace "$app_namespace" \
    --set image.repository="$IMAGE_REPO" \
    --set image.tag="$IMAGE_TAG" \
    --set alerts.synthetic.count="$SYNTHETIC_ALERTS_COUNT" \
    --wait=false \
    --timeout 2m \
    2>&1 | tail -1
}

export -f deploy_app
export CHART_PATH IMAGE_REPO IMAGE_TAG SYNTHETIC_ALERTS_COUNT

deploy_stage() {
  local stage_num="$1"
  local start="$2"
  local end="$3"
  local pause="$4"
  shift 4
  local names=("$@")

  echo ""
  echo "Stage $stage_num/4: deploying apps $start-$end (${#names[@]} items), next pause ${pause}s"
  printf '%s\n' "${names[@]}" | xargs -P "$PARALLEL" -I {} bash -c 'deploy_app "$@"' _ {}
}

offset=0
for i in "${!stage_sizes[@]}"; do
  size=${stage_sizes[$i]}
  pause=${stage_pauses[$i]}
  start=$((offset + 1))
  end=$((offset + size))
  stage_num=$((i + 1))
  stage_names=("${apps[@]:offset:size}")

  deploy_stage "$stage_num" "$start" "$end" "$pause" "${stage_names[@]}"
  offset=$end

  if [ "$stage_num" -lt 4 ]; then
    echo "Stage $stage_num complete. Sleeping ${pause}s before next stage..."
    sleep "$pause"
  fi
done

echo ""
echo "All $TARGET_APPS apps deployed in 4 stages (one namespace per app)."
echo "Namespaces created/used: $TARGET_APPS"
