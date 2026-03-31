#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CHART_PATH="${CHART_PATH:-$REPO_ROOT/chart}"
NAMES_FILE="${NAMES_FILE:-$SCRIPT_DIR/app-names.txt}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/patsevanton/performance-test-alerts-victoriametrics}"
IMAGE_TAG="${IMAGE_TAG:-1.0.0}"
ALERTS_PER_APP="${ALERTS_PER_APP:-50}"
BASE_ALERTS_COUNT="${BASE_ALERTS_COUNT:-10}"

# День 4: 10% (app-901..app-1000) за ~9 часов
START_INDEX=901
TARGET_APPS=100
APP_DELAY_SECONDS=317
MAX_RUNTIME_HOURS=9
INSTALL_SECONDS_ESTIMATE=10

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

estimated_delay_seconds=$(((TARGET_APPS - 1) * APP_DELAY_SECONDS))
estimated_runtime_seconds=$((estimated_delay_seconds + TARGET_APPS * INSTALL_SECONDS_ESTIMATE))
max_runtime_seconds=$((MAX_RUNTIME_HOURS * 3600))

echo "День 4: развёртывание app-$START_INDEX..app-$required ($TARGET_APPS шт.)"
echo "Целевая доля: 10%"
echo "Оценка длительности: ${estimated_runtime_seconds}с (ориентир: ${max_runtime_seconds}с)"
echo "Задержка между app: ${APP_DELAY_SECONDS}с"

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
  if [ "$j" -lt "$n" ]; then
    sleep "$APP_DELAY_SECONDS"
  fi
done

echo "День 4 завершён: установлено $TARGET_APPS app."
