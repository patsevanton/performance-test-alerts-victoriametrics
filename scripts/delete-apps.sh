#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAMES_FILE="${NAMES_FILE:-$SCRIPT_DIR/app-names.txt}"
PARALLEL="${PARALLEL:-10}"
TARGET_APPS="${TARGET_APPS:-800}"
DELETE_NAMESPACES="${DELETE_NAMESPACES:-true}"

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
echo "Deleting $TARGET_APPS apps (parallelism: $PARALLEL)"
echo "Namespace mode: per-app (namespace name = app name)"
echo "Delete namespaces: $DELETE_NAMESPACES"

delete_app() {
  local name="$1"
  local app_namespace="$name"
  echo "[$(date +%H:%M:%S)] Deleting $name from namespace $app_namespace ..."

  if kubectl get namespace "$app_namespace" >/dev/null 2>&1; then
    helm uninstall "$name" --namespace "$app_namespace" >/dev/null 2>&1 || true
    if [ "$DELETE_NAMESPACES" = "true" ]; then
      kubectl delete namespace "$app_namespace" --wait=false >/dev/null 2>&1 || true
    fi
  else
    echo "  namespace $app_namespace not found, skip"
  fi
}

export -f delete_app
export DELETE_NAMESPACES

printf '%s\n' "${apps[@]}" | xargs -P "$PARALLEL" -I {} bash -c 'delete_app "$@"' _ {}

echo ""
echo "Deletion requests sent for $TARGET_APPS apps."
