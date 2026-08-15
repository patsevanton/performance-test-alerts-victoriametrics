# TODO — Отказоустойчивость стенда

Список задач по повышению отказоустойчивости стенда VictoriaMetrics в Yandex Managed K8s.
Порядок — по приоритету реализации. Чекбокс `[x]` означает выполнено.

## Helm-values — vmks-values.yaml

- [x] **#5 PDB для vmstorage (3 реплики, minAvailable: 2).** Готово.
- [x] **#6 PDB для vmselect (3 реплики, minAvailable: 2).** Готово.
- [x] **#7 PDB для vminsert (2 реплики, minAvailable: 1).** Готово.
- [x] **#8 Persistence для Alertmanager.** Добавлен `storage.volumeClaimTemplate`
      (network-ssd, 1Gi) — состояние silences переживает рестарт подов.
- [x] **#9 Anti-affinity / topologySpreadConstraints для vmstorage, vmselect, vminsert.**
      Добавлены `topologySpreadConstraints` по `topology.kubernetes.io/zone`
      (`whenUnsatisfiable: ScheduleAnyway`) для vmstorage, vmselect, vminsert.
- [x] **#10 Persistence для vmstorage.** Добавлен
      `storage.volumeClaimTemplate` (storageClassName: `yc-network-ssd`, 20Gi).
      `storageDataPath` не переопределяется — используется дефолт chart'а (`/vm-data`).
- [x] **#11 vmagent — replicaCount и PDB.** Заданы `replicaCount: 2` и
      `podDisruptionBudget.minAvailable: 1`. Реплики — HA (не sharding), обе скрейпят
      одни цели → дубликаты семплов схлопываются через `dedup.minScrapeInterval: 20s`
      (= дефолт chart'а `vmagent.spec.scrapeInterval`) в `vmstorage` и `vmselect`.
- [x] **#12 `replicationFactor: 2` → 3.** Установлен `replicationFactor: 3` (равен числу vmstorage).
- [x] **#13 PDB для ingress-nginx controller (высокий приоритет).** Проверить, что `controller.replicaCount >= 2`
      и есть PDB (в `helm_release.ingress_nginx` set-параметры).

## chart/values.yaml (golden-signal-app)

- [ ] **#14 `replicaCount: 2` без anti-affinity.** Добавить `topologySpreadConstraints` по зоне,
      чтобы 2 реплики расходились по AZ.
