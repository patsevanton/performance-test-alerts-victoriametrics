#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CHART_PATH="${CHART_PATH:-$REPO_ROOT/chart}"
NAMES_FILE="${NAMES_FILE:-$SCRIPT_DIR/app-names.txt}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/patsevanton/performance-test-alerts-victoriametrics}"
IMAGE_TAG="${IMAGE_TAG:-1.2.0}"
ALERTS_PER_APP="${ALERTS_PER_APP:-50}"
BASE_ALERTS_COUNT="${BASE_ALERTS_COUNT:-10}"

START_INDEX="${START_INDEX:-1}"
TARGET_APPS="${TARGET_APPS:-1350}"

if [ ! -f "$NAMES_FILE" ]; then
  echo "Ошибка: файл $NAMES_FILE не найден. Сначала выполните generate-app-names.sh."
  exit 1
fi

EXTRA_ALERTS_COUNT="${EXTRA_ALERTS_COUNT:-$((ALERTS_PER_APP - BASE_ALERTS_COUNT))}"
if [ "$ALERTS_PER_APP" -lt "$BASE_ALERTS_COUNT" ]; then
  echo "Ошибка: ALERTS_PER_APP ($ALERTS_PER_APP) должен быть >= BASE_ALERTS_COUNT ($BASE_ALERTS_COUNT)."
  exit 1
fi

mapfile -t all_names < "$NAMES_FILE"
required=$((START_INDEX + TARGET_APPS - 1))
if [ "${#all_names[@]}" -lt "$required" ]; then
  echo "Ошибка: в $NAMES_FILE только ${#all_names[@]} имён, а нужно минимум $required."
  exit 1
fi

start_offset=$((START_INDEX - 1))
apps=("${all_names[@]:start_offset:TARGET_APPS}")

START_TIME=$(date +%s)

echo "Развёртывание app-$START_INDEX..app-$required ($TARGET_APPS шт.) без пауз"

deploy_app() {
  local name="$1"
  kubectl create namespace "$name" 2>/dev/null || true
  echo "[$(date +%H:%M:%S)] Установка $name ..."
  helm upgrade --install "$name" "$CHART_PATH" \
    --namespace "$name" \
    --set image.repository="$IMAGE_REPO" \
    --set image.tag="$IMAGE_TAG" \
    --set alerts.extra.count="$EXTRA_ALERTS_COUNT" \
    --wait=false \
    --timeout 2m \
    2>&1 | tail -1
}

n=${#apps[@]}
j=0
for name in "${apps[@]}"; do
  j=$((j + 1))
  deploy_app "$name"
done

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
HOURS=$((ELAPSED / 3600))
MINS=$(((ELAPSED % 3600) / 60))
SECS=$((ELAPSED % 60))
echo "Завершено: установлено $n app."
echo "Время выполнения скрипта: ${HOURS}ч ${MINS}м ${SECS}с (всего ${ELAPSED}с)"
