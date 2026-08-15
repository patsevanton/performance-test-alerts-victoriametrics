# TODO — Отказоустойчивость стенда

Список задач по повышению отказоустойчивости стенда VictoriaMetrics в Yandex Managed K8s.
Порядок — по приоритету реализации. Чекбокс `[x]` означает выполнено.

## Инфраструктура (Terraform)

- [ ] **#1 Региональный master (multi-zone).** Сейчас `master.zonal` в одной зоне `ru-central1-e` (`k8s.tf:32-35`).
      Падение зоны = потеря API server. Перевести на региональный кластер (master в 3 зонах).
- [ ] **#2 Убрать preemptible-ноды.** `scheduling_policy.preemptible = true` (`k8s.tf:77-79`).
      Preemptible-ноды отзываются в любой момент — прямой риск для устойчивости data plane.
      Заменить на обычные ноды (компромисс по стоимости).
- [ ] **#3 NAT-шлюз в одной зоне (SPOF egress).** `yandex_vpc_address.nat` привязан к `ru-central1-e` (`net.tf:34`).
      Падение зоны = потеря исходящего трафика. Рассмотреть multi-AZ NAT или принять как компромисс.
- [ ] **#4 `network-ssd` для boot disk.** Сейчас `network-hdd` (`k8s.tf:98`).
      network-ssd даст предсказуемый I/O под storage-подами vmstorage.

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
- [ ] **#13 PDB для ingress-nginx controller.** Проверить, что `controller.replicaCount >= 2`
      и есть PDB (в `helm_release.ingress_nginx` set-параметры).

## chart/values.yaml (golden-signal-app)

- [ ] **#14 `replicaCount: 2` без anti-affinity.** Добавить `topologySpreadConstraints` по зоне,
      чтобы 2 реплики расходились по AZ.

## Замечания (компромиссы)

- `victoria-metrics-operator` (1 реплика) — SPOF, но допустимо: при падении не влияет
  на уже запущенные поды, только на reconcile новых изменений.
- victoria-logs-cluster (vlselect/vlinsert/vlstorage) и collector — `replicaCount` не задан
  в values. Нужны PDB/anti-affinity, если дефолты chart'а разворачивают ≥2 реплик.
