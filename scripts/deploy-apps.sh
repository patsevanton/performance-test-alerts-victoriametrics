#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CHART_PATH="${CHART_PATH:-$REPO_ROOT/chart}"
NAMES_FILE="${NAMES_FILE:-$SCRIPT_DIR/app-names.txt}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/patsevanton/performance-test-alerts-victoriametrics}"
IMAGE_TAG="${IMAGE_TAG:-1.0.0}"
TARGET_APPS="${TARGET_APPS:-1000}"
ALERTS_PER_APP="${ALERTS_PER_APP:-50}"
BASE_ALERTS_COUNT="${BASE_ALERTS_COUNT:-10}"
STAGE_SIZES="${STAGE_SIZES:-400,300,200,100}"
# Пауза в секундах между последовательными установками app внутри стадии 1..4
STAGE_APP_DELAYS="${STAGE_APP_DELAYS:-20,40,70,90}"

if [ ! -f "$NAMES_FILE" ]; then
  echo "Ошибка: файл $NAMES_FILE не найден. Сначала выполните generate-app-names.sh."
  exit 1
fi

SYNTHETIC_ALERTS_COUNT="${SYNTHETIC_ALERTS_COUNT:-$((ALERTS_PER_APP - BASE_ALERTS_COUNT))}"

if [ "$ALERTS_PER_APP" -lt "$BASE_ALERTS_COUNT" ]; then
  echo "Ошибка: ALERTS_PER_APP ($ALERTS_PER_APP) должен быть >= BASE_ALERTS_COUNT ($BASE_ALERTS_COUNT)."
  exit 1
fi

mapfile -t all_names < "$NAMES_FILE"
available=${#all_names[@]}
if [ "$available" -lt "$TARGET_APPS" ]; then
  echo "Ошибка: в $NAMES_FILE только $available имён, а TARGET_APPS=$TARGET_APPS."
  exit 1
fi

apps=("${all_names[@]:0:TARGET_APPS}")
total_alerts=$((TARGET_APPS * ALERTS_PER_APP))

IFS=',' read -r -a stage_sizes <<< "$STAGE_SIZES"
IFS=',' read -r -a stage_app_delays <<< "$STAGE_APP_DELAYS"

if [ "${#stage_sizes[@]}" -ne 4 ] || [ "${#stage_app_delays[@]}" -ne 4 ]; then
  echo "Ошибка: STAGE_SIZES и STAGE_APP_DELAYS должны содержать ровно 4 значения через запятую."
  exit 1
fi

sum=0
for size in "${stage_sizes[@]}"; do
  sum=$((sum + size))
done
if [ "$sum" -ne "$TARGET_APPS" ]; then
  echo "Ошибка: сумма(STAGE_SIZES)=$sum должна равняться TARGET_APPS=$TARGET_APPS."
  exit 1
fi

echo "Развёртывание $TARGET_APPS приложений (последовательно, паузы между установками по стадиям: $STAGE_APP_DELAYS с)"
echo "Алертов на приложение: $ALERTS_PER_APP (базовых: $BASE_ALERTS_COUNT, синтетических: $SYNTHETIC_ALERTS_COUNT)"
echo "Запланировано всего алертов: $total_alerts"
echo "Режим namespace: на приложение (имя namespace = имя приложения)"

deploy_app() {
  local name="$1"
  local app_namespace="$name"
  echo "[$(date +%H:%M:%S)] Установка $name в namespace $app_namespace ..."
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

deploy_stage() {
  local stage_num="$1"
  local start="$2"
  local end="$3"
  local delay_between="$4"
  shift 4
  local names=("$@")
  local n=${#names[@]}
  local j=0

  echo ""
  echo "Этап $stage_num/4: последовательная установка приложений $start-$end ($n шт.), пауза между установками ${delay_between} с"

  for name in "${names[@]}"; do
    j=$((j + 1))
    deploy_app "$name"
    if [ "$j" -lt "$n" ]; then
      sleep "$delay_between"
    fi
  done
}

offset=0
for i in "${!stage_sizes[@]}"; do
  size=${stage_sizes[$i]}
  delay=${stage_app_delays[$i]}
  start=$((offset + 1))
  end=$((offset + size))
  stage_num=$((i + 1))
  stage_names=("${apps[@]:offset:size}")

  deploy_stage "$stage_num" "$start" "$end" "$delay" "${stage_names[@]}"
  offset=$end
done

echo ""
echo "Все $TARGET_APPS приложений развёрнуты за 4 этапа (по одному namespace на приложение)."
echo "Создано/использовано namespace: $TARGET_APPS"
