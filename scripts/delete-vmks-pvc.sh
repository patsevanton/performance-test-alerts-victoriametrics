#!/usr/bin/env bash
set -euo pipefail

# Удаление всех PVC в namespace vmks.
# PVC у statefulset (alertmanager, vmselect, vmstorage) защищены от удаления
# подами-владельцами, поэтому сначала удаляем их, затем PVC и PV.

NAMESPACE="${NAMESPACE:-vmks}"
DELETE_PV="${DELETE_PV:-true}"

echo "Namespace: $NAMESPACE"
echo "Delete PV: $DELETE_PV"

pvc_count=$(kubectl get pvc -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l)
if [ "$pvc_count" -eq 0 ]; then
  echo "No PVC found in namespace $NAMESPACE, nothing to delete."
  exit 0
fi

echo "Found $pvc_count PVC:"
kubectl get pvc -n "$NAMESPACE"

# Сначала удаляем владельцев PVC (statefulset-ы оператора VictoriaMetrics),
# иначе kubernetes не даст удалить PVC или они будут пересозданы.
kubectl delete statefulset -n "$NAMESPACE" --all

# Удаляем PV-объёмы, чтобы диски Yandex Cloud не остались висеть.
pvc_volumes=$(kubectl get pvc -n "$NAMESPACE" -o jsonpath='{range .items[*]}{.spec.volumeName}{"\n"}{end}')
pvc_uids=$(kubectl get pvc -n "$NAMESPACE" -o jsonpath='{range .items[*]}{.metadata.uid}{"\n"}{end}')

kubectl delete pvc -n "$NAMESPACE" --all

if [ "$DELETE_PV" = "true" ]; then
  echo "Deleting PVs..."
  for pv in $pvc_volumes; do
    kubectl patch pv "$pv" -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
    kubectl delete pv "$pv" --wait=false >/dev/null 2>&1 || true
  done
  echo "PV deletion requests sent."
fi

echo "Done."
