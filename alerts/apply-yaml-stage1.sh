#!/bin/bash
# Этап 1: файлы 1–400, интервал 5 с.
# При заданных GRAFANA_URL и GRAFANA_TOKEN создаёт аннотации в Grafana (старт/финиш).

STAGE=1
grafana_annotation() {
  local text="$1"
  [ -z "${GRAFANA_URL}" ] || [ -z "${GRAFANA_TOKEN}" ] && return 0
  local ms=$(($(date +%s) * 1000))
  local code
  code=$(curl ${CURL_OPTS} -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${GRAFANA_TOKEN}" -H "Content-Type: application/json" \
    "${GRAFANA_URL}/api/annotations" \
    -d "{\"time\":${ms},\"text\":\"${text}\",\"tags\":[\"apply-yaml-stage${STAGE}\"]}")
  [ "$code" = "200" ]
}

list=$(find vmrules -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V | sed -n '1,400p')
grafana_annotation "apply-yaml-stage${STAGE} started" && echo "Аннотация Grafana: старт этапа ${STAGE}."

echo "$list" | while read -r f; do
  kubectl apply -f "$f"
  echo "  Этап ${STAGE}"
  sleep 5
done

grafana_annotation "apply-yaml-stage${STAGE} finished" && echo "Аннотация Grafana: финиш этапа ${STAGE}."
echo "Этап ${STAGE} готов."
