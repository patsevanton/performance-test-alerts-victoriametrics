#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-load-test}"
CHART_PATH="${CHART_PATH:-./chart}"
NAMES_FILE="${NAMES_FILE:-app-names.txt}"
PARALLEL="${PARALLEL:-10}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/patsevanton/alert-templates-helm-vmalert-impulse}"
IMAGE_TAG="${IMAGE_TAG:-1.3.0}"

if [ ! -f "$NAMES_FILE" ]; then
  echo "Error: $NAMES_FILE not found. Run generate-app-names.sh first."
  exit 1
fi

TOTAL=$(wc -l < "$NAMES_FILE")
echo "Deploying $TOTAL apps to namespace '$NAMESPACE' (parallelism: $PARALLEL)"

kubectl create namespace "$NAMESPACE" 2>/dev/null || true

deploy_app() {
  local name="$1"
  echo "[$(date +%H:%M:%S)] Installing $name ..."
  helm upgrade --install "$name" "$CHART_PATH" \
    --namespace "$NAMESPACE" \
    --set image.repository="$IMAGE_REPO" \
    --set image.tag="$IMAGE_TAG" \
    --wait=false \
    --timeout 2m \
    2>&1 | tail -1
}

export -f deploy_app
export NAMESPACE CHART_PATH IMAGE_REPO IMAGE_TAG

cat "$NAMES_FILE" | xargs -P "$PARALLEL" -I {} bash -c 'deploy_app "$@"' _ {}

echo ""
echo "All $TOTAL apps deployed. Checking status:"
kubectl get pods -n "$NAMESPACE" --no-headers | wc -l
echo "pods in namespace $NAMESPACE"
