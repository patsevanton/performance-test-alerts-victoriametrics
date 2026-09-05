# Нагрузочное тестирование VictoriaMetrics: рост числа алертов и поведение vmalert, vmselect и vmstorage

## Цель статьи

Как рост числа правил в `VMRule` нагружает кластер VictoriaMetrics, и какие ориентиры по CPU, памяти и параллелизму запросов это даёт для capacity planning.

VictoriaMetrics часто применяют как единый бэкенд для метрик и алертов: один `VMCluster` хранит ряды, `vmalert` оценивает правила и пишет состояние алертов обратно в TSDB, а `victoria-metrics-operator` синхронизирует `VMRule` из Kubernetes API в ConfigMap'ы и пересобирает Pod'ы `vmalert`. При росте числа правил до десятков тысяч появляются конкретные вопросы: сохраняется ли оценка правил и состояние алертов (pending/firing) при рестартах `vmalert` и какова цена восстановления; где физически возникают узкие места по CPU и памяти в data plane (`vmalert`, `vmselect`, `vmstorage`, `vminsert`, `vmagent`) и в control plane (`victoria-metrics-operator`, `kube-apiserver`); как растут RPS, задержки и CPU `kube-apiserver` при массовом применении `VMRule` и reconcile operator'а.

Стенд изначально проектировался под 1700 приложений и 85 000 правил (1700 × 50), однако в фактическом прогоне достоверные снимки загрузки удалось получить только до порога ~15 000 правил: выше `vmselect` упирался в лимит параллельности и отдавал 429, из-за чего скрипт сбора перестал получать метрики (подробности — в разделе «Результаты замеров»). Поэтому статья фиксирует честные, проверяемые результаты на участке ~500–15 000 правил, а для повторного прогона целевой уровень снижен до **40 000 алертов** (800 приложений × 50) — с поднятым лимитом параллелизма и увеличенными ресурсами `vmstorage`.

## Стенд: Yandex Managed K8s + vmks

Инфраструктура разворачивается Terraform в Yandex Cloud. Кластер Managed Kubernetes (`k8s.tf`, версия 1.33) состоит из master (управляемый, вне кластера) и группы из 32 узлов `standard-v3`, 4 vCPU / 8 ГБ каждый, preemptible, с распределением по трём зонам (`ru-central1-b/-d/-e`). Ноды не имеют публичных IP: `network_interface.nat = false`, исходящий трафик из приватных подсетей идёт через NAT-шлюз и Route Table (`net.tf`). Публичный адрес есть только у балансировщика Traefik, из которого через `sslip.io` формируются FQDN Grafana и vmselect. Исходники инфраструктуры: [`k8s.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/k8s.tf), [`net.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/net.tf), [`ip-dns.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/ip-dns.tf), [`monitoring.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/monitoring.tf).

Перед установкой применяется PriorityClass [`priority-class.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/priority-class.yaml), затем сам стек мониторинга устанавливается через Helm-чарт `victoria-metrics-k8s-stack` (версия 0.90.2) в namespace `vmks`:

```bash
kubectl apply -f priority-class.yaml

helm upgrade --install vmks oci://ghcr.io/victoriametrics/helm-charts/victoria-metrics-k8s-stack \
  --namespace vmks --create-namespace \
  --version 0.90.2 \
  --wait --values vmks-values.yaml
```

Values генерируются из шаблона [`vmks-values.yaml.tftpl`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml.tftpl) через Terraform `templatefile` (`monitoring.tf`); FQDN Grafana формируется из публичного IP Traefik (`terraform output grafana_url`). Пароль Grafana извлекается из секрета:

```bash
kubectl get secret vmks-grafana -n vmks -o jsonpath='{.data.admin-password}' | base64 --decode; echo
```

Все компоненты vmks (`vmstorage`, `vmselect`, `vminsert`, `vmalert`, `vmagent`, `alertmanager`, `victoria-metrics-operator`, `node-exporter`, `kube-state-metrics`) запускаются с `priorityClassName: vmks-critical`, чтобы не вытесняться apps-нагрузкой стенда ([`priority-class.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/priority-class.yaml)).

В Yandex Managed K8s control-plane компоненты (`kube-controller-manager`, `kube-scheduler`, `kube-etcd`) недоступны для скрейпинга — master управляемый и вне кластера. В `vmks-values.yaml.tftpl` отключены соответствующие scrape-job и recording-правила (`kubeControllerManager`, `kubeScheduler`, `kubeEtcd`, группы `etcd`, `kubernetes-system-scheduler`, `kubernetes-system-controller-manager`, `kube-scheduler.rules`), иначе `vmagent` плодит `ScrapePoolHasNoTargets`, а `vmalert` — `RecordingRulesNoData`.

Топология стека (из `vmks-values.yaml.tftpl`):

- `vmcluster` с `replicationFactor: 3` и тремя `vmstorage` (по 20 Gi `yc-network-ssd`, PDB `minAvailable: 2`, `topologySpreadConstraints` по зонам);
- шесть `vmselect` (PDB `minAvailable: 4`, каждый запрос расходится параллельно по `vmstorage` во всех зонах);
- два `vminsert` и два `vmagent` (HA-репликация скрейпинга, `dedup.minScrapeInterval: 20s`);
- два `vmalert` (`evaluationInterval: 1m`, PDB `minAvailable: 1`);
- два `alertmanager` в кластерном режиме с persistence для silences;
- два `victoria-metrics-operator` с leader election, PDB `minAvailable: 1`.

## Что генерирует нагрузку

Нагрузка — 800 экземпляров приложения Golden Signal App (целевой уровень повторного прогона, 40 000 алертов), каждый в своём namespace как отдельный Helm release. Приложение ([`app/main.go`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/app/main.go)) экспонирует метрики через `prometheus/client_golang`. Тяжёлые алерты на кардинальные метрики PromQL всегда активны, параметризуются через `app.cardinality.*` и `alerts.extra.count` в [`chart/values.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/values.yaml).

### Метрики и лейбл-пространства

Базовые метрики переведены в высококардинальные варианты с полным набором лейблов:

- `app_requests_total{method,endpoint,status_code,route,tenant_id,region,version}` (CounterVec);
- `app_errors_total{method,endpoint,status_code,route,tenant_id,region,version}` (CounterVec);
- `app_request_latency_seconds` (HistogramVec, кастомные `ExponentialBuckets(0.005, 1.15, APP_HIST_BUCKETS)`, по умолчанию 5 бакетов);
- `app_request_duration_seconds` (HistogramVec, альтернатива с тем же набором бакетов — для тяжёлых алертов по высококардинальной оси);
- `app_goroutines` (gauge без лейблов — служебная);
- `app_inflight_requests{route,tenant_id}` (GaugeVec);
- `app_cache_operations_total{cache_hit,route,tenant_id}` (CounterVec, `cache_hit` = `hit`/`miss`);
- `app_queue_size{queue,tenant_id}` (GaugeVec).

Лейбл-пространства ([`app/main.go`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/app/main.go), PLAN-high-cardinality.md 1.1):

- `method` — `GET`, `POST`, `PUT`, `DELETE`, `PATCH` (5);
- `endpoint` — `/work`, `/healthz`, `/metrics`, `/api/v1`, `/api/v2` (5);
- `status_code` — `200`, `429`, `500`, `503`, `504` (5); вероятности: `500` — 20 %, `429`/`503`/`504` — по 5 % каждая, иначе `200`;
- `route` — `route-0` … `route-(APP_ROUTES-1)` (по умолчанию 10);
- `tenant_id` — `tenant-0` … `tenant-(APP_TENANTS-1)` (по умолчанию 50);
- `region` — из env `APP_REGION`, который прокидывается через Kubernetes Downward API из лейбла ноды `topology.kubernetes.io/zone` (фактическая зона, где запущен pod);
- `version` — из env `APP_VERSION` (по умолчанию совпадает с `appVersion` чарта).

Каждые 2 секунды приложение само отправляет фоновый запрос на `/work`, который выбирает случайные `tenant`/`route`/`method` и инкрементит все высококардинальные метрики с полным набором лейблов. Эндпоинт `/metrics` отдаёт метрики в формате Prometheus.

### Оценка кардинальности

Формула числа рядов на `app_requests_total`: `APP_TENANTS × APP_ROUTES × 5 method × 5 endpoint × 5 status_code × 3 region × 1 version`.

- «Высокая» ступень (`APP_TENANTS=50`, `APP_ROUTES=10`) даёт ≈ 50 × 10 × 5 × 5 × 5 × 3 × 1 = **187 500 рядов на приложение** (~319 млн рядов на 1700 app по одному counter'у).
- «Средняя» ступень (`APP_TENANTS=5`, `APP_ROUTES=10`) даёт ≈ 5 × 10 × 5 × 5 × 5 × 3 × 1 = **18 750 рядов на приложение** (~31.9 млн рядов на 1700 app).

**Фактический прогон, результаты которого приведены ниже, выполнялся на «средней» ступени**: дефолтные `app.cardinality.tenants=5`, `app.cardinality.routes=10` из [`chart/values.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/values.yaml) не переопределялись через env при запуске `deploy_and_snapshot.py` (проверено по env реальных подов: `APP_TENANTS=5`, `APP_ROUTES=10`, `APP_HIST_BUCKETS=5`). То есть на приложение приходилось ~18 750 рядов по `app_requests_total`, а не ~187 500.

Передавать параметры кардинальности при деплое можно через env в `scripts/deploy_and_snapshot.py` или напрямую `--set app.cardinality.tenants=50,app.cardinality.routes=10`.

### Профиль правил

На каждый release через Helm chart ([`chart`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/tree/main/chart)) создаются `Service`, `VMServiceScrape` (автосбор метрик `vmagent`'ом) и `VMRule` с правилами. `VMRule` ([`chart/templates/vmrule.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/templates/vmrule.yaml)) содержит 10 базовых алертов (`HighErrorRate`, `CriticalErrorRate`, `HighLatency`, `HighLatencyP99`, `HighAverageLatency`, `HighGoroutineCount`, `CriticalGoroutineCount`, `LowTraffic`, `TrafficSpike`, `ErrorBurst`) и 40 дополнительных `ExtraAlert0xx`, итого 50 на приложение. Все выражения фильтруются по `job` label, равному имени release, поэтому каждый `VMRule` работает только со своими рядами и ряды реально существуют в TSDB.

Из 40 `ExtraAlert0xx` **20 правил — тяжёлый PromQL** (помечены `heavy: "true"`), 20 — простой профиль. Распределение классов по 20 тяжёлым (при `alerts.extra.count=40`):

| Класс                          | Правил | Шаблоны (ExtraAlert0xx) |
| ------------------------------ | ------ | ----------------------- |
| Subqueries                     | 6      | 001–006 (`max_over_time(rate(...)[5m:1m])`, `changes(...[10m])`, `deriv`) |
| Joins                          | 5      | 007–011 (`* on(route,tenant_id) group_left(region) ...`, `or on(...)`, `and on(...)`) |
| `label_replace`/`label_join`   | 3      | 012–014 (склейка `tenant_id` и `route` → `tenant_route`) |
| `histogram_quantile` по high-card оси | 3 | 015–017 (`sum by (le, tenant_id, route[, status_code])`) |
| `*_over_time` + `predict_linear` | 3    | 018–020 (`quantile_over_time`, `stddev_over_time`, `predict_linear`) |

Часть тяжёлых правил ссылается на новые высококардинальные метрики `app_inflight_requests`, `app_cache_operations_total`, `app_queue_size`, `app_request_duration_seconds`. Все тяжёлые правила фильтруются по `job="{{ appName }}"` для сопоставимости с профилем 1 по числу правил на приложение.

**Итого при 800 экземплярах:** 800 Deployment, 800 Service, 800 `VMServiceScrape`, 800 `VMRule`, 40 000 правил (16 000 тяжёлых + 24 000 простых). При фактической «средней» кардинальности прогона — ~15 млн рядов по `app_requests_total` (при «высокой» было бы ~150 млн).

Развёртывание выполняется скриптами (смотри шаги ниже); здесь приводятся только зафиксированные результаты замеров, сами нагрузочные скрипты продолжают работать в стенде и не перезапускаются.

### Ресурсные требования генератора нагрузки

При 800 экземплярах (requests/limits из [`chart/values.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/values.yaml)):


TODO: Надо уточнить (лимиты памяти подняты до 50 Mi под высококардинальные метрики; замер RSS генератора при `APP_TENANTS=50`, `APP_ROUTES=10` — отдельная задача по PLAN-high-cardinality.md этап 4).
| Ресурс          | На 1 pod | На 800 pods        |
| --------------- | -------- | ------------------ |
| CPU requests    | 10m      | 8 000m (8 cores)   |
| CPU limits      | 20m      | 16 000m (16 cores) |
| Memory requests | 16Mi     | ~12.5 GiB          |
| Memory limits   | 50Mi     | ~39.1 GiB          |

## Как разворачивался стенд

### PriorityClass

```bash
kubectl apply -f priority-class.yaml
```

### victoria-metrics-k8s-stack

Команда установки vmks (вместе с применением PriorityClass) приведена в разделе «Стенд» выше.

### Генерация имён и развёртывание приложений

Шаг 1 — генерация случайных имён вида `app-{adjective}-{noun}-{number}` в `app-names.txt`:

```bash
scripts/generate-app-names.sh 800
```

Шаг 2 — развёртывание и сбор снимков Capacity Planning одним скриптом [`scripts/deploy_and_snapshot.py`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/scripts/deploy_and_snapshot.py). Каждый app устанавливается как отдельный Helm release в отдельный namespace (имя namespace = имя приложения) через `helm upgrade --install --wait --timeout 2m` с ретраями до 3 раз при ошибке. Скрипт совмещает деплой и снятие метрик:

```bash
python3 scripts/deploy_and_snapshot.py
```

Логика работы:

- Читает `app-names.txt`, формирует список `apps[START_INDEX-1 : START_INDEX-1+TARGET_APPS]`.
- Устанавливает app по одному; счётчик `installed` — это номер завершённого релиза в цикле (детерминирован, не зависит от `kubectl get pod`).
- После каждого успешного `helm install` получает VMRule через Kubernetes API и считает число алертов как `количество VMRule × количество rules внутри VMRule`. Считаются только `VMRule` в namespace'ах `app-*` (системные `VMRule` самого стека vmks не относятся к нагрузке и иначе давали бы постоянное смещение +243). Для неодинаковых VMRule используется точная сумма rules по объектам.
- Важно различать количество настроенных rules и фактически созданных временных рядов `ALERTS`: один rule может создать несколько series в зависимости от результата его выражения и набора labels. Для порогов используется только количество настроенных rules.
- При `alerts_count >= TARGETS[next_idx]` делает instant-запрос всех метрик из `QUERIES` через Prometheus API vmselect (CPU, память pod'ов `vmks`, нагрузка на `kube-apiserver`, `vm_http_requests_total` компонент, `vmalert_iteration_duration_seconds`, `vm_concurrent_select_current`, `scrape_samples_scraped`) через `ThreadPoolExecutor(max_workers=16)`. В снимок записываются расчётное число правил, `alerts_count_estimated=False`, `installed`. **Ограничение:** при десятках тысяч правил `vmselect` упирается в `search.maxConcurrentRequests` и отвечает 429, из-за чего снимки с ~15 000 теряются (подробнее — в разделе «Результаты замеров»).
- После последнего порога выжидает `SETTLE_WAIT` (по умолчанию 600 с) и делает финальный снимок «после через 10 мин установки N app».
- `MIN_SNAPSHOT_GAP` сохранён как env для совместимости, но не применяется при последовательном деплое.

Результаты — `capacity_snapshots.json` (сырые значения + число `VMRule` и rules) и `capacity_snapshots.txt` (форматированная таблица). Пороги скрипта: 500, 5000, 10000, 15000, 20000, 25000, 30000, 35000, 40000 (последний равен `TARGET_APPS * ALERTS_PER_APP` = 800 × 50). Скрипт использует только стандартную библиотеку Python 3. Для досрочной остановки — Ctrl-C, будут выгружены собранные снимки.

После прогона можно дособрать пропущенные значения историческими запросами через [`scripts/backfill_snapshots.py`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/scripts/backfill_snapshots.py) (по одному `query_range` на метрику по всему окну теста). Он перезаписывает `capacity_snapshots.json`/`.txt`, заполняя `null`-ячейки ближайшим отсчётом. На практике это восстанавливает данные только там, где метрики реально скрейпились: выше ~15 000 значения недостоверны из-за пропусков скрейпа и перезапусков подов.

Шаг 3 — проверка статуса (число Helm releases, статусы Pod'ов, потребление ресурсов, количество `VMRule` и `VMServiceScrape`):

```bash
scripts/status-apps.sh
```

Шаг 4 — удаление приложений (параметры `NAMES_FILE`, `TARGET_APPS`, `PARALLEL`, `DELETE_NAMESPACES`):

```bash
scripts/delete-apps.sh
```

## Как устроены оценка правил и восстановление состояния

Понимание результатов требует представления о том, кто и когда перемещает правила и состояние алертов.

### Хранение правил в ConfigMap'ах


`victoria-metrics-operator` собирает все `VMRule` из подходящих namespace'ов и упаковывает в ConfigMap'ы вида `vm-<vmalert-name>-rulefiles-<i>`:

```
vm-vmks-victoria-metrics-k8s-stack-rulefiles-0
vm-vmks-victoria-metrics-k8s-stack-rulefiles-1
...
```

Размер одного объекта жёстко ограничен Kubernetes — 1 MiB (`The data stored in a ConfigMap cannot exceed 1 MiB`). Operator не заполняет бакеты под самый лимит: он хранит правила gzip-сжатыми (ключ `rules.yaml` в `binaryData`) и режет их по внутреннему бюджету `ConfigDataBudgetBytes`, который по умолчанию равен 524 288 байт (512 KiB) — это даёт ~50% запас под JSON-метаданные объекта и возможные инъекции меток/аннотаций (например, от Kyverno). Бюджет можно менять через env `VM_CONFIG_DATA_BUDGET_BYTES`. При превышении бюджета `build.PackItems` рекурсивно делит группы на бакеты, дополнительно `packRuleGroups` гарантирует, что внутри одного бакета нет групп с одинаковым именем (требование vmalert):

```
vm-vmks-victoria-metrics-k8s-stack-rulefiles-0
vm-vmks-victoria-metrics-k8s-stack-rulefiles-1
...
```

Reconcile-цикл operator'а (порядка 60 с) собирает все `VMRule`, упаковывает в `vm-...-rulefiles-0`, при превышении `ConfigDataBudgetBytes` (по умолчанию 512 KiB сжатых данных) разбивает на несколько ConfigMap. `vmalert` монтирует эти ConfigMap как `volume`/`volumeMount`. Появление нового ConfigMap с правилами меняет спецификацию Pod'а `vmalert`, и operator пересоздаёт Pod (rolling restart). Если число ConfigMap'ов не меняется, обновления правил подхватываются через SIGHUP без рестарта. Таким образом, при массовом поэтапном apply новые ConfigMap могут вызывать пересоздание Pod'а `vmalert`, и для будущих изменений стоит использовать батчи или GitOps с контролируемым темпом.

### Сохранение состояния: remoteRead/remoteWrite и ALERTS_FOR_STATE

`vmalert` настроен на запись и чтение состояния из `VMCluster`:

- `-remoteWrite.url` — при каждой оценке `vmalert` пишет ряды `ALERTS` и `ALERTS_FOR_STATE` в `VMCluster` (через `vminsert`);
- `-remoteRead.url` — при старте процесса `vmalert` однократно восстанавливает состояние, запрашивая ряды `ALERTS_FOR_STATE` (через `vmselect`).

`ALERTS_FOR_STATE` содержит полную информацию о состоянии каждого алерта (`ActiveAt`, `for` duration и т.д.), необходимую для восстановления после рестарта. Конфигурация с двумя репликами `vmalert`, remoteRead/remoteWrite и Alertmanager в кластерном режиме корректно восстанавливает состояние без потерь: после рестарта `vmalert` читает `ALERTS_FOR_STATE` один раз и продолжает оценку.

<!-- TODO: уточнить, включён ли в тесте явный remoteRead/remoteWrite в extraArgs vmalert, если требуется процитировать значения флагов из values -->

## Результаты замеров

### Что удалось измерить, а что — нет

Стенд дошёл до 1699 развёрнутых приложений (из 1700 запланированных — один релиз `app-1074` не встал после трёх ретраев `helm` и был пропущен) и 84 950 настроенных правил в app-`VMRule` (1699 × 50). Однако **достоверные снимки загрузки есть только до порога ~15 000 правил**. Дальше сбор ломался по трём причинам:

1. **429 от vmselect на снимках.** Скрипт снимает ~30 метрик через `ThreadPoolExecutor(max_workers=16)` параллельно. Когда `vmalert` начинает оценивать десятки тысяч правил, `vmselect` упирается в `search.maxConcurrentRequests=32` и отвечает 429/таймаутами (`couldn't start executing in 60s`). Одна упавшая ошибка в `fetch_snapshot` роняет весь снимок — с порога ~20 000 в JSON остались `null` (в логе: `snapshot fetch failed: HTTP Error 429: Too Many Requests`).
2. **Пропуски скрейпа kubelet-метрик.** С порога ~30 000 `vmagent` не успевал доставлять kubelet-метрики (`pod_cpu_usage_seconds_total`, `pod_memory_working_set_bytes`): raw-запросы по историческим timestamp возвращают пустой вектор. `rate()` при пропуске даёт 0.
3. **Перезапуски подов.** Ближе к концу прогона (после 8-го часа) `vmstorage-*` и `vmalert` были пересозданы: `vmstorage` — из-за eviction по memory pressure на preemptible-нодах (`The node was low on resource: memory`), `vmalert` — при изменении набора ConfigMap с правилами. Счётчики `rate()` после рестарта обнуляются, что делает поздние CPU-значения бессмысленными.

Попытка досбора пропущенных значений через backfill по сохранённым в TSDB данным (`scripts/backfill_snapshots.py`) заполнила часть ячеек, но выше ~15 000 они недостоверны: метрики `vm_rows_scanned_total`, `vmalert_evaluation_duration_seconds`, `vmalert_evaluations_total`, `vm_search_latency_seconds` **вообще не экспортируются** используемой версией VictoriaMetrics (instant-запросы всегда пусты), поэтому колонка `HEAVY` осталась частично пустой.

Вывод: **приведённые ниже таблицы достоверны для ~500–~15 000 правил** и должны трактоваться как «рост на старте», а не как полная кривая до 85 000. Для полного прогона нужно сначала починить сбор снимков (см. раздел «Выводы»).

### CPU (в среднем на pod, millicores)

| RULES  | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500   | 10m     | 72m       | 11m      | 38m      | 76m     | 4m       |
| ~5000  | 415m    | 629m      | 295m     | 88m      | 275m    | 25m      |
| ~10000 | 1835m   | 2481m     | 805m     | 186m     | 676m    | 52m      |
| ~15000 | 1177m   | 3354m     | 361m     | 137m     | 1327m   | 73m      |

Рост CPU сосредоточен в `vmalert`, `vmstorage` и `vmagent`: `vmalert` выполняет оценку правил и порождает параллельные подзапросы к `vmstorage`, а `vmagent` скрейпит `vmalert` (self-scrape метрик `vmalert_*` и состояния). Скачок на ~10 000 (`vmalert` 1835m, `vmstorage` 2481m) совпадает с первым массовым пересбором ConfigMap и перезапуском `vmalert` при росте числа бакетов правил, после чего значения частично откатываются. `vmselect` и `vminsert` остаются заметно ниже.

### Memory (в среднем на pod, MiB working set)

| RULES  | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500   | 54Mi    | 651Mi     | 62Mi     | 75Mi     | 157Mi   | 42Mi     |
| ~5000  | 420Mi   | 1594Mi    | 89Mi     | 110Mi    | 217Mi   | 92Mi     |
| ~10000 | 1521Mi  | 4541Mi    | 859Mi    | 170Mi    | 334Mi   | 178Mi    |
| ~15000 | 2353Mi  | 5348Mi    | 1081Mi   | 451Mi    | 401Mi   | 238Mi    |

`vmstorage` растёт быстрее всех: на участке ~500–~15 000 `working_set` вырос с ~651 MiB до ~5348 MiB, при этом `vmstorage` уже на ~15000 приблизился к лимиту 6 Gi (см. [`vmks-values.yaml.tftpl`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml.tftpl)). Память `vmalert` также растёт заметно (~54 → ~2353 MiB) — за счёт состояния тысяч алертов в памяти и рядов `ALERTS`/`ALERTS_FOR_STATE`.

### RPS и операционные метрики

| RULES  | API Server RPS | API Server p99 lat | API Server CPU | vmselect HTTP RPS | vmstorage HTTP RPS | vminsert HTTP RPS |
| ------ | -------------- | ------------------ | -------------- | ----------------- | ------------------ | ----------------- |
| ~500   | 17.8           | 45 ms              | 88m            | 6.9               | 0.3                | 13.6              |
| ~5000  | 20.5           | 50 ms              | 103m           | 148.9             | 0.3                | 17.6              |
| ~10000 | 19.6           | 51 ms              | 105m           | 288.0             | 0.3                | 27.2              |
| ~15000 | 22.1           | 96 ms              | 119m           | 94.6              | 0.3                | 25.8              |

RPS API server почти не зависит от числа правил (~18–22 req/s), p99 остаётся в пределах 45–96 ms, CPU `kube-apiserver` — 88–119m: control plane не является узким местом относительно data plane. HTTP RPS `vmselect` растёт с ~6.9 до ~288 на участке ~500–~10 000 — поток запросов от `vmalert` (restore `ALERTS_FOR_STATE` + eval). Падение `vmselect` RPS на ~15 000 (94.6) — артефакт: в этот момент начинаются пропуски скрейпа и частичная деградация доставки метрик `vm_http_requests_total`.

### Метрики компонентов, выросшие при нагрузке

| RULES  | vmalert_iteration_duration (max) | vm_concurrent_select_current | vmagent scrape_samples (vmalert) |
| ------ | -------------------------------- | ---------------------------- | -------------------------------- |
| ~500   | 0.96s                            | 0                            | 2987                             |
| ~5000  | 2.15s                            | 1                            | 26700                            |
| ~10000 | 52.08s                           | 32                           | 52712                            |
| ~15000 | 298.83s                          | 32                           | 76469                            |

`max(vmalert_iteration_duration_seconds)` — длительность одного цикла оценки всех правил. Ключевой сигнал: уже на ~10 000 правил цикл eval достиг 52.08 с, а на ~15 000 — 298.83 с, то есть **впятеро превысил `evaluationInterval: 1m`**. Это означает, что `vmalert` перестал успевать оценивать правила в срок ещё задолго до целевых 85 000 — параллелизм `vm_concurrent_select_current` упирается в лимит 32 (`search.maxConcurrentRequests`), а `scrape_samples_scraped` (self-scrape `vmalert`) растёт вместе с числом правил.

### Параметры очередей и таймаутов

Поиск выполняется и на `vmstorage`, и на `vmselect`. Дефолт `search.maxConcurrentRequests = 2` даёт на параллельной нагрузке от `vmalert` ответ 429 (vmselect) / 503 (vmstorage) и сообщение `couldn't start executing in 10s` (таймаут ожидания в очереди). В стенде лимиты подняты (смотри [`vmks-values.yaml.tftpl`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml.tftpl)):

| Параметр                                    | Значение | Комментарий                                                                                                                              |
| ------------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `search.maxConcurrentRequests` (дефолт)     | 2        | При параллельной нагрузке — 429 (vmselect) / 503 (vmstorage) и сообщение `couldn't start executing in 10s` (таймаут ожидания в очереди)  |
| `search.maxConcurrentRequests` (стенд)      | 32       | Поднято вместе с очередью и лимитом серий, чтобы снизить отказы при eval `vmalert`; в этом прогоне стало лимитом при ~15 000 правил      |
| `search.maxQueueDuration` (стенд)           | 60s      | Максимальное ожидание в очереди перед стартом выполнения запроса                                                                         |
| `search.maxQueryDuration` (vmselect, стенд) | 300s     | Согласовано с `queryTimeout` datasource Grafana (300s), иначе обрезка раньше клиента                                                     |

## Dashboard в Grafana

Grafana доступна по адресу `http://grafana.<ingress_public_ip>.sslip.io/` (логин/пароль — из секрета `vmks-grafana`, см. раздел «Стенд»). Общий dashboard по результатам теста будет добавлен отдельно.

<!-- DASHBOARD: вставить ссылку/скриншот dashboard здесь -->

## Скриншоты Grafana

Ниже — панели, где наблюдалась заметная динамика. Графики без тренда (ровная линия) не включены.

<!-- TODO: добавить файлы в каталог screenshots/ и раскомментировать ссылки -->

- Kubernetes API dashboard (RPS / p99 latency / CPU).

<!-- ![Kubernetes API dashboard](screenshots/kubernetes-api-dashboard.png) -->

- `vmalert`: iteration duration и динамика активных алертов.

<!-- ![vmalert dashboard](screenshots/vmalert-dashboard.png) -->

- `vmselect`: HTTP RPS, concurrent select, saturation/limit reached.

<!-- ![vmselect dashboard](screenshots/vmselect-dashboard.png) -->

- `vmstorage`: CPU/Memory (рост на верхних стадиях нагрузки).

<!-- ![vmstorage dashboard](screenshots/vmstorage-dashboard.png) -->

- `vmagent`: scrape samples (рост с числом правил/целей).

<!-- ![vmagent dashboard](screenshots/vmagent-dashboard.png) -->

## Важная оговорка: структура выражений и интерпретация результатов

Тяжёлая часть (высококардинальные метрики и 20 тяжёлых `ExtraAlert0xx`) **всегда активна** — отдельного «второго режима» нет. Параметризация кардинальности через `app.cardinality.*` в [`chart/values.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/values.yaml) или env в `scripts/deploy_and_snapshot.py` (`APP_TENANTS`, `APP_ROUTES`, `APP_HIST_BUCKETS`, `APP_REGION`, `APP_VERSION`); число тяжёлых правил — `alerts.extra.count` (по умолчанию 40, из них 20 тяжёлых).

Фактический прогон шёл на «средней» кардинальности `APP_TENANTS=5`, `APP_ROUTES=10` (~18 750 рядов/app по `app_requests_total`), поэтому измеренное потребление CPU/RAM отражает стоимость оценки правил на **низкокардинальных** метриках с `job`-фильтром до одного release. Для «высокой» ступени (`APP_TENANTS=50`) результаты следует перепроверять отдельно — нагрузка на query engine будет существенно выше.

Все 40 000 правил построены по единому шаблону — прямое сравнение результата PromQL-выражения с порогом (`expr > N`). В шаблоне чарта ([`chart/templates/vmrule.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/templates/vmrule.yaml)) 20 из 40 `ExtraAlert0xx` используют тяжёлые классы PromQL: subqueries (`[5m:1m]`), joins (`group_left`/`on(...)`), `label_join`/`label_replace`, `histogram_quantile` по высококардинальной оси (`sum by (le, tenant_id, route[, status_code])`), `quantile_over_time`/`stddev_over_time`/`predict_linear`. Распределение функций по правилам:

| Характеристика                              | Значение                       |
| ------------------------------------------- | ------------------------------ |
| Всего правил                                | 40 000                         |
| Правил на одно приложение                   | 50 (10 базовых + 40 `ExtraAlert`) |
| из них тяжёлых `ExtraAlert` на приложение   | 20 (subqueries/joins/`label_*`/histogram_quantile high-card/`*_over_time`+`predict_linear`) |
| `rate()`                                    | ~24 800 (62%)                  |
| `histogram_quantile`                        | 8 000 (20%)                    |
| `increase()`                                | 7 200 (18%)                    |
| `max_over_time` / `avg_over_time`           | 6 400 (16%)                    |
| Прямые сравнения gauge (`app_goroutines > N`) | 4 000 (10%)                  |
| `clamp_min` (обёртка делителя)              | 11 200 (28%)                   |
| Subqueries (`[5m:1m]`)                      | ~4 800 (12%, тяжёлая часть)    |
| Joins (`group_left`/`on(...)`)              | ~4 000 (10%, тяжёлая часть)    |
| `label_replace`/`label_join`                | ~2 400 (6%, тяжёлая часть)     |
| `histogram_quantile` по high-card оси       | ~2 400 (6%, тяжёлая часть)     |
| `quantile_over_time`/`stddev_over_time`/`predict_linear` | ~2 400 (6%, тяжёлая часть) |

> Проценты в сумме превышают 100%, так как одно правило может содержать несколько функций (например, `histogram_quantile(..., sum(rate(...)))`).

Тестовое приложение ([`app/main.go`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/app/main.go)) само генерирует метрики — `app_requests_total` (counter), `app_errors_total` (counter), `app_request_latency_seconds` (histogram с `prometheus.DefBuckets`), `app_goroutines` (gauge). Каждый алерт фильтруется по `job` label, привязанному к имени release, поэтому реальные ряды существуют и вычисление `rate()`/`histogram_quantile`/`increase()` обращается к TSDB.

Что это означает для интерпретации результатов:

- Измеренное потребление CPU и RAM компонентами `vmselect` и `vmstorage` отражает стоимость оценки правил с базовыми функциями (`rate`, `histogram_quantile`, `increase`, `*_over_time`) на низкокардинальных метриках: каждое приложение экспонирует ограниченное число рядов на метрику, а `job`-фильтр сужает выборку до одного release.
- В production-окружении с высокой кардинальностью (тысячи pod'ов, контейнеров, сервисов в одном `job` или без фильтра) те же `rate()` и `histogram_quantile` будут сканировать значительно больше рядов, а добавление subqueries, joins и `label_*` дополнительно увеличит нагрузку на query engine.
- Тест фиксирует ориентир для профиля «много алертов на низкокардинальных метриках»; для тяжёлых PromQL-выражений и высокой кардинальности результаты следует перепроверять отдельно.

## Выводы

Приведённые ниже выводы основаны на **достоверном участке ~500–~15 000 правил**. Это рабочий сигнал для capacity planning на старте, а не жёсткие универсальные лимиты: выше 15 000 прогон не дал достоверных данных по причинам из раздела «Результаты замеров».

- **`vmalert` перестал успевать оценивать правила уже на ~15 000:** `max(vmalert_iteration_duration_seconds)` достиг ~298.8 с при `evaluationInterval: 1m` — то есть цикл eval впятеро превысил интервал. Параллелизм `vmselect` (`vm_concurrent_select_current`) при этом упирался в лимит 32 (`search.maxConcurrentRequests`).
- **Основная точка насыщения — `vmselect` (параллелизм запросов), а не CPU:** 429 `Too Many Requests` при сборе снимков и рост `vm_concurrent_select_current` до 32 указывают, что лимит параллельности 32 стал узким местом раньше, чем упёрся CPU компонентов.
- **Память `vmstorage` растёт быстро:** с ~651 MiB (500) до ~5348 MiB (15000) working set — это ~324 MiB на каждые 1000 правил на данном профиле (рост включает и накопление рядов, не только число правил). `vmstorage` первым приближается к своему лимиту (6 Gi в стенде), затем `vmalert`.
- **Control plane не является узким местом:** RPS API server стабилен (~18–22 req/s), p99 в пределах 45–96 ms, CPU `kube-apiserver` 88–119m — при росте правил до ~15 000 управляющая плоскость не насыщается.
- **Метрики тяжёлого профиля не экспортируются — заменены доступными аналогами:** `vm_rows_scanned_total`, `vmalert_evaluation_duration_seconds`, `vmalert_evaluations_total`, `vm_search_latency_seconds` **не существуют** в используемой версии VictoriaMetrics (instant-запросы по ним всегда пусты). Исследование `/metrics` компонентов и исходников vmalert показало, какие ряды реально экспортируются и чем их заменить (подробности — в подразделе «Куда делись метрики»). В `QUERIES` скриптов замены внесены.
- **Поведение `VMRule`/ConfigMap стабильно:** `operator` предсказуемо дробит правила около `ConfigDataBudgetBytes` (по умолчанию 512 KiB сжатых данных, при хард-лимите K8s 1 MiB); при росте количества правил число `vm-...-rulefiles-*` ConfigMap растёт ступенчато. При массовом поэтапном apply новые ConfigMap могут вызывать пересоздание Pod'а `vmalert`, поэтому для будущих изменений стоит использовать батчи или GitOps с контролируемым темпом (механизм — в подразделе «Почему пересоздавался vmalert»).

### Куда делись метрики (`vm_rows_scanned_total`, `vmalert_evaluation_duration_seconds`, `vmalert_evaluations_total`)

Эти ряды отсутствовали не из-за пропусков скрейпа, а потому что их **нет в экспорте** используемой версии VictoriaMetrics — instant-запросы по ним всегда возвращают пустой вектор. Проверка списка экспортируемых метрик и исходников `vmalert` (`app/vmalert/rule/group.go`, `alerting.go`) показывает фактическую картину:

- `vm_rows_scanned_total` — такого названия нет. VictoriaMetrics экспортирует счётчики сканирования как histogram `vm_rows_scanned_per_query{_sum,_count,_bucket}` (по одному ряду на запрос/серию); есть также `vm_rows_read_per_query`, `vm_rows_read_per_series`, `vm_rows_merged_total`, `vm_rows_added_to_storage_total`, `vm_rows_inserted_total`. Замена: `sum(rate(vm_rows_scanned_per_query_sum{...}[5m]))`.
- `vmalert_evaluation_duration_seconds` и `vmalert_evaluations_total` — таких метрик нет. Длительность и число оценок экспортируются **на уровне группы правил**, а не отдельного правила: `vmalert_iteration_duration_seconds` (Summary: `_sum`/`_count`, кванткли), `vmalert_iteration_total`, `vmalert_iteration_missed_total`, `vmalert_iteration_reset_total`; на уровне процесса — `vmalert_execution_total` и `vmalert_execution_errors_total` (счётчики выполненных/упавших оценок правил). Замена: `max(vmalert_iteration_duration_seconds{...})` и `sum(rate(vmalert_execution_total{...}[5m]))`.
- `vm_search_latency_seconds` — такого названия нет; длительность запросов экспортируется как Summary `vm_request_duration_seconds` (кванткли, `_sum`/`_count`) на vmselect/vmstorage. Замена: `max(vm_request_duration_seconds{...})`.

Дополнительно для диагностики насыщения vmselect доступны `vm_concurrent_select_current`, `vm_concurrent_select_capacity`, `vm_concurrent_select_limit_reached_total`, `vm_concurrent_select_limit_timeout_total`, а счётчики кэшей — `vm_cache_requests_total`/`vm_cache_misses_total`.

### Почему пересоздавался vmalert

`vmalert` пересоздавался не сам по себе, а по вине `victoria-metrics-operator`. Reconcile-цикл operator'а собирает все `VMRule` и упаковывает их в ConfigMap'ы `vm-<vmalert-name>-rulefiles-<i>`; при росте количества правил объём сжатых правил превышает `ConfigDataBudgetBytes` (по умолчанию 512 KiB), и operator **добавляет новый ConfigMap**. Эти ConfigMap монтируются в Pod `vmalert` как `volume`/`volumeMount`, поэтому появление нового ConfigMap меняет спецификацию Pod'а — operator делает rolling restart `vmalert`. Если же число ConfigMap'ов не меняется (правила только редактируются внутри существующих бакетов), обновления подхватываются через SIGHUP без рестарта.

То есть в прогоне на росте числа правил рестарты `vmalert` — штатное следствие ступенчатого добавления `rulefiles-*`, а не сбой. Это же объясняет сброс счётчиков `rate()` и временные всплески CPU/memory `vmalert` (после рестарта он заново читает `ALERTS_FOR_STATE` и восстанавливает состояние). Митигация — контролируемый темп apply (батчи) или GitOps, а также увеличение `ConfigDataBudgetBytes`, чтобы бакеты менялись реже.

### Что исправлено для повторного прогона

- **Сбор снимков стал устойчивым к 429:** `fetch_snapshot` больше не роняет весь снимок — каждая метрика собирается независимо с ретраями и backoff против 429/5xx (`fetch_one_retry`), неудачная метрика даёт `None`, а не исключение.
- **Лимит `search.maxConcurrentRequests` поднят с 32 до 64** на `vmselect` и `vmstorage` (в `vmks-values.yaml.tftpl`): 32 оказалось узким местом уже на ~15 000 правил.
- **Ресурсы `vmstorage` увеличены** (limits cpu 6→8, memory 6Gi→8Gi) против eviction по memory pressure на preemptible-нодах и под merge частей при насыщении вставки.
- **Число алертов снижено до 40 000** (`TARGET_APPS` 1700→800, пороги `TARGETS` до 40000) — целевой уровень из TODO.
- **Заменены несуществующие метрики** в `QUERIES` (`vm_rows_scanned_total`→`vm_rows_scanned_per_query_sum`, `vmalert_evaluation_duration_seconds`→`vmalert_iteration_duration_seconds`, `vmalert_evaluations_total`→`vmalert_execution_total`, `vm_search_latency_seconds`→`vm_request_duration_seconds`).

## Итог

На достоверном участке (~500–~15 000 правил) тест показывает: профиль «много алертов на низкокардинальных метриках» упирается прежде всего в параллелизм `vmselect` и быстро растущую память `vmstorage`; `vmalert` перестаёт успевать оценивать правила в срок уже при ~15 000 правил. Control plane (`kube-apiserver`, `operator`) остаётся в рамках. До целевых 85 000 правил довести измерение не удалось — сломался сбор снимков (429 от vmselect, пропуски скрейпа, eviction подов). Перед повторным прогоном починены сбор метрик и лимиты параллелизма, число алертов снижено до 40 000, ресурсы `vmstorage` увеличены; полученные ориентиры следует перепроверять на своей кардинальности и своём профиле PromQL.
