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

# День 2: app-651..app-1000
START_INDEX=651
TARGET_APPS=350
STAGE_SIZES="150,100,60,40"
STAGE_APP_DELAYS="20,40,70,90"
MAX_RUNTIME_HOURS=8
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

IFS=',' read -r -a stage_sizes <<< "$STAGE_SIZES"
IFS=',' read -r -a stage_app_delays <<< "$STAGE_APP_DELAYS"

if [ "${#stage_sizes[@]}" -ne 4 ] || [ "${#stage_app_delays[@]}" -ne 4 ]; then
  echo "Ошибка: STAGE_SIZES и STAGE_APP_DELAYS должны содержать 4 значения."
  exit 1
fi

sum=0
estimated_delay_seconds=0
for i in "${!stage_sizes[@]}"; do
  size=${stage_sizes[$i]}
  delay=${stage_app_delays[$i]}
  sum=$((sum + size))
  if [ "$size" -gt 1 ]; then
    estimated_delay_seconds=$((estimated_delay_seconds + (size - 1) * delay))
  fi
done

if [ "$sum" -ne "$TARGET_APPS" ]; then
  echo "Ошибка: сумма STAGE_SIZES ($sum) должна быть равна TARGET_APPS ($TARGET_APPS)."
  exit 1
fi

estimated_runtime_seconds=$((estimated_delay_seconds + TARGET_APPS * INSTALL_SECONDS_ESTIMATE))
max_runtime_seconds=$((MAX_RUNTIME_HOURS * 3600))

echo "День 2: развёртывание app-$START_INDEX..app-$required ($TARGET_APPS шт.)"
echo "Оценка длительности: ${estimated_runtime_seconds}с (ориентир: ${max_runtime_seconds}с)"

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

offset=0
for i in "${!stage_sizes[@]}"; do
  size=${stage_sizes[$i]}
  delay=${stage_app_delays[$i]}
  stage_num=$((i + 1))
  stage_names=("${apps[@]:offset:size}")
  echo "Этап $stage_num/4: $size app, задержка ${delay}с"
  n=${#stage_names[@]}
  j=0
  for name in "${stage_names[@]}"; do
    j=$((j + 1))
    deploy_app "$name"
    if [ "$j" -lt "$n" ]; then
      sleep "$delay"
    fi
  done
  offset=$((offset + size))
done

echo "День 2 завершён: установлено $TARGET_APPS app."
