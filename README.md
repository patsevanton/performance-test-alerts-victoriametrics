# Нагрузочное тестирование VictoriaMetrics: 85 000 алертов и поведение vmalert, vmselect и vmstorage на насыщении

## Цель статьи

Как 1700 приложений и 85 000 правил в `VMRule` нагружают кластер VictoriaMetrics, и какие ориентиры по CPU, памяти и параллелизму запросов это даёт для capacity planning.

VictoriaMetrics часто применяют как единый бэкенд для метрик и алертов: один `VMCluster` хранит ряды, `vmalert` оценивает правила и пишет состояние алертов обратно в TSDB, а `victoria-metrics-operator` синхронизирует `VMRule` из Kubernetes API в ConfigMap'ы и пересобирает Pod'ы `vmalert`. При росте числа правил до десятков тысяч появляются конкретные вопросы: сохраняется ли оценка правил и состояние алертов (pending/firing) при рестартах `vmalert` и какова цена восстановления; где физически возникают узкие места по CPU и памяти в data plane (`vmalert`, `vmselect`, `vmstorage`, `vminsert`, `vmagent`) и в control plane (`victoria-metrics-operator`, `kube-apiserver`); как растут RPS, задержки и CPU `kube-apiserver` при массовом применении `VMRule` и reconcile operator'а. Снимки загрузки фиксируются при прохождении порогов расчётного числа настроенных правил от ~500 до ~85 000 на живом стенде из 1700 приложений. Для порога используется количество объектов `VMRule`, умноженное на количество rules внутри них.

## Стенд: Yandex Managed K8s + vmks

Инфраструктура разворачивается Terraform в Yandex Cloud. Кластер Managed Kubernetes (`k8s.tf`, версия 1.33) состоит из master (управляемый, вне кластера) и группы из 32 узлов `standard-v3`, 4 vCPU / 8 ГБ каждый, preemptible, с распределением по трём зонам (`ru-central1-b/-d/-e`). Ноды не имеют публичных IP: `network_interface.nat = false`, исходящий трафик из приватных подсетей идёт через NAT-шлюз и Route Table (`net.tf`). Публичный адрес есть только у балансировщика Traefik, из которого через `sslip.io` формируются FQDN Grafana и vmselect. Исходники инфраструктуры: [`k8s.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/k8s.tf), [`net.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/net.tf), [`ip-dns.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/ip-dns.tf), [`monitoring.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/monitoring.tf).

Сам стек мониторинга установлен через Helm-чарт `victoria-metrics-k8s-stack` (версия 0.90.2) в namespace `vmks`:

```bash
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

Нагрузка — 1700 экземпляров приложения Golden Signal App, каждый в своём namespace как отдельный Helm release. Приложение ([`app/main.go`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/app/main.go)) экспонирует метрики через `prometheus/client_golang`. Тяжёлые алерты на кардинальные метрики PromQL всегда активны, параметризуются через `app.cardinality.*` и `alerts.extra.count` в [`chart/values.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/values.yaml).

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

При `APP_TENANTS=50`, `APP_ROUTES=10`, 5 method, 5 endpoint, 5 status_code, 3 region, 1 version число рядов на `app_requests_total` ≈ 50 × 10 × 5 × 5 × 5 × 3 × 1 = **187 500 на приложение**. При 1700 приложениях — ~319 млн рядов только по одному counter'у. Это «очень высокая» ступень; для отладки предусмотрена «средняя» (`APP_TENANTS=5`, `APP_ROUTES=5`).

Передавать параметры кардинальности при деплое можно через env в `scripts/deploy_and_snapshot.py` (см. 5.1) или напрямую `--set app.cardinality.tenants=5,app.cardinality.routes=5`.

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

**Итого при 1700 экземплярах:** 1700 Deployment, 1700 Service, 1700 `VMServiceScrape`, 1700 `VMRule`, 85 000 правил (34 000 тяжёлых + 51 000 простых), ~319 млн рядов по `app_requests_total`.

Развёртывание выполняется скриптами (смотри шаги ниже); здесь приводятся только зафиксированные результаты замеров, сами нагрузочные скрипты продолжают работать в стенде и не перезапускаются.

### Ресурсные требования генератора нагрузки

При 1700 экземплярах (requests/limits из [`chart/values.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/values.yaml)):


TODO: Надо уточнить (лимиты памяти подняты до 50 Mi под высококардинальные метрики; замер RSS генератора при `APP_TENANTS=50`, `APP_ROUTES=10` — отдельная задача по PLAN-high-cardinality.md этап 4).
| Ресурс          | На 1 pod | На 1700 pods       |
| --------------- | -------- | ------------------ |
| CPU requests    | 10m      | 17 000m (17 cores) |
| CPU limits      | 20m      | 34 000m (34 cores) |
| Memory requests | 16Mi     | ~26.6 GiB          |
| Memory limits   | 50Mi     | ~83.0 GiB          |

## Как разворачивался стенд

### PriorityClass

```bash
kubectl apply -f priority-class.yaml
```

### victoria-metrics-k8s-stack

Команда установки vmks приведена в разделе «Стенд» выше.

### Генерация имён и развёртывание приложений

Шаг 1 — генерация случайных имён вида `app-{adjective}-{noun}-{number}` в `app-names.txt`:

```bash
scripts/generate-app-names.sh 1700
```

Шаг 2 — развёртывание и сбор снимков Capacity Planning одним скриптом [`scripts/deploy_and_snapshot.py`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/scripts/deploy_and_snapshot.py). Каждый app устанавливается как отдельный Helm release в отдельный namespace (имя namespace = имя приложения) через `helm upgrade --install --wait --timeout 2m` с ретраями до 3 раз при ошибке. Скрипт совмещает деплой и снятие метрик:

```bash
python3 scripts/deploy_and_snapshot.py
```

Логика работы:

- Читает `app-names.txt`, формирует список `apps[START_INDEX-1 : START_INDEX-1+TARGET_APPS]`.
- Устанавливает app по одному; счётчик `installed` — это номер завершённого релиза в цикле (детерминирован, не зависит от `kubectl get pod`).
- После каждого успешного `helm install` получает VMRule через Kubernetes API и считает число алертов как `количество VMRule × количество rules внутри VMRule`. Для неодинаковых VMRule используется точная сумма rules по объектам.
- Важно различать количество настроенных rules и фактически созданных временных рядов `ALERTS`: один rule может создать несколько series в зависимости от результата его выражения и набора labels. Для порогов используется только количество настроенных rules.
- При `alerts_count >= TARGETS[next_idx]` делает instant-запрос всех метрик из `QUERIES` через Prometheus API vmselect (CPU, память pod'ов `vmks`, нагрузка на `kube-apiserver`, `vm_http_requests_total` компонент, `vmalert_iteration_duration_seconds`, `vm_concurrent_select_current`, `scrape_samples_scraped`) через `ThreadPoolExecutor(max_workers=16)`. В снимок записываются расчётное число правил, `alerts_count_estimated=False`, `installed`.
- После последнего порога выжидает `SETTLE_WAIT` (по умолчанию 600 с) и делает финальный снимок «после через 10 мин установки N app».
- `MIN_SNAPSHOT_GAP` сохранён как env для совместимости, но не применяется при последовательном деплое.

Результаты — `capacity_snapshots.json` (сырые значения + число `VMRule` и rules) и `capacity_snapshots.txt` (форматированная таблица). Пороги скрипта: 500, 5000, 10000, 15000, 20000, 25000, 30000, 35000, 40000, 45000, 50000, 55000, 60000, 65000, 70000, 75000, 80000, 85000 (последний равен `TARGET_APPS * ALERTS_PER_APP`). Скрипт использует только стандартную библиотеку Python 3. Для досрочной остановки — Ctrl-C, будут выгружены собранные снимки.

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

> Числовые таблицы ниже относятся к старому прогону и не являются результатом текущей версии скриптов. Новый прогон следует запускать после удаления старых `capacity_snapshots.json` и `capacity_snapshots.txt`; пороги в нём считаются по количеству настроенных rules в `VMRule`.

Ниже приведены исторические значения для ориентира. Столбец `~50000 + 1h` — снимок, сделанный примерно через 1 час после прохождения порога ~50 000 настроенных rules.

### CPU (в среднем на pod)

| RULES         | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------------- | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500          | 14m     | 77m       | 13m      | 15m      | 28m     | 6m       |
| ~5000         | 33m     | 58m       | 55m      | 11m      | 33m     | 17m      |
| ~10000        | 82m     | 87m       | 81m      | 15m      | 51m     | 21m      |
| ~15000        | 133m    | 179m      | 87m      | 20m      | 69m     | 26m      |
| ~20000        | 215m    | 218m      | 95m      | 23m      | 73m     | 33m      |
| ~25000        | 179m    | 226m      | 140m     | 24m      | 82m     | 38m      |
| ~30000        | 207m    | 170m      | 146m     | 23m      | 89m     | 42m      |
| ~35000        | 216m    | 198m      | 187m     | 32m      | 124m    | 67m      |
| ~40000        | 260m    | 208m      | 201m     | 33m      | 147m    | 77m      |
| ~45000        | 314m    | 285m      | 255m     | 38m      | 174m    | 78m      |
| ~50000        | 509m    | 458m      | 288m     | 48m      | 250m    | 135m     |
| ~50000 + 1h   | 277m    | 272m      | 196m     | 44m      | 240m    | 32m      |

Рост CPU сосредоточен в `vmalert` и `vmstorage` — именно они выполняют оценку правил и порождают множество параллельных подзапросов к `vmstorage` при restore/eval. `vminsert` остаётся низким: трафик remote-write — это преимущественно запись `ALERTS`/`ALERTS_FOR_STATE`, и он не масштабируется линейно с числом правил. `operator` растёт умеренно, его вклад — reconcile `VMRule` и пересборка ConfigMap.

### Memory (в среднем на pod)

| RULES         | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------------- | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500          | 45Mi    | 243Mi     | 32Mi     | 116Mi    | 55Mi    | 42Mi     |
| ~5000         | 33Mi    | 370Mi     | 72Mi     | 57Mi     | 72Mi    | 63Mi     |
| ~10000        | 227Mi   | 833Mi     | 193Mi    | 87Mi     | 99Mi    | 86Mi     |
| ~15000        | 324Mi   | 1065Mi    | 119Mi    | 103Mi    | 122Mi   | 116Mi    |
| ~20000        | 336Mi   | 1387Mi    | 135Mi    | 115Mi    | 118Mi   | 87Mi     |
| ~25000        | 508Mi   | 1635Mi    | 390Mi    | 122Mi    | 151Mi   | 152Mi    |
| ~30000        | 575Mi   | 1648Mi    | 341Mi    | 142Mi    | 148Mi   | 138Mi    |
| ~35000        | 768Mi   | 1978Mi    | 474Mi    | 146Mi    | 191Mi   | 220Mi    |
| ~40000        | 946Mi   | 2308Mi    | 250Mi    | 126Mi    | 222Mi   | 271Mi    |
| ~45000        | 1036Mi  | 2713Mi    | 607Mi    | 134Mi    | 267Mi   | 167Mi    |
| ~50000        | 1426Mi  | 3106Mi    | 380Mi    | 186Mi    | 339Mi   | 225Mi    |
| ~50000 + 1h   | 1929Mi  | 2400Mi    | 293Mi    | 163Mi    | 185Mi   | 323Mi    |

Память `vmstorage` растёт линейно и является основным «жёстким» лимитом — при 50 000 `ALERTS` пик `container_memory_working_set_bytes` достигает ~3106 Mi. В `vmks-values.yaml.tftpl` лимит `vmstorage` поднят до 5 Gi без production-запаса (4 Gi покрывали пик с небольшим остатком, но при росте за 50 000 `ALERTS` исчерпывается). Память `vmalert` через 1 час после пика продолжает расти (1426 → 1929 Mi) — это связано с восстановлением тысяч состояний алертов после рестартов и накоплением рядов.

### RPS и операционные метрики

| RULES         | API Server RPS | API Server p99 lat | API Server CPU | vmselect HTTP RPS | vmstorage HTTP RPS | vminsert HTTP RPS |
| ------------- | -------------- | ------------------ | -------------- | ----------------- | ------------------ | ----------------- |
| ~500          | 15.2           | 37 ms              | 94m            | 22.9              | 0.1                | 5                 |
| ~5000         | 12.7           | 37 ms              | 82m            | 42.1              | 0.1                | 5.2               |
| ~10000        | 12.3           | 37 ms              | 81m            | 123.4             | 0.1                | 5.7               |
| ~15000        | 12.5           | 44 ms              | 84m            | 107.5             | 0.1                | 5.9               |
| ~20000        | 13             | 47 ms              | 88m            | 92.7              | 0.1                | 6.1               |
| ~25000        | 13.3           | 48 ms              | 93m            | 183.1             | 0.1                | 6.3               |
| ~30000        | 12.9           | 50 ms              | 89m            | 250               | 0.1                | 6.4               |
| ~35000        | 12.9           | 46 ms              | 90m            | 346.3             | 0.1                | 6.7               |
| ~40000        | 13             | 45 ms              | 92m            | 396.8             | 0.1                | 7.1               |
| ~45000        | 13.2           | 48 ms              | 97m            | 476.2             | 0.1                | 7.6               |
| ~50000        | 14.3           | 72 ms              | 108m           | 644.2             | 0.1                | 8.6               |
| ~50000 + 1h   | 12.1           | 44 ms              | 85m            | 350.1             | 0.1                | 8.5               |

RPS API server практически не чувствителен к числу `ALERTS` (~12–15 req/s), p99 задержек остаётся в пределах 37–72 ms, CPU `kube-apiserver` — ~81–108m. Control plane не стал главным узким местом относительно data plane. В то же время HTTP RPS vmselect (`sum(rate(vm_http_requests_total{job="vmselect-..."}[5m]))`) растёт с 22.9 до 644.2 — именно `vmselect` принимает на себя поток запросов от `vmalert` (restore `ALERTS_FOR_STATE` + eval) и Grafana.

### Метрики компонентов, выросшие при нагрузке

| RULES         | vmalert_iteration_duration (max) | vm_concurrent_select_current | vmagent scrape_samples (vmalert) |
| ------------- | -------------------------------- | ---------------------------- | -------------------------------- |
| ~500          | 0.97s                            | 2                            | 3463                             |
| ~5000         | 0.85s                            | 1                            | 21025                            |
| ~10000        | 1.68s                            | 2                            | 46263                            |
| ~15000        | 1.94s                            | 0                            | 57763                            |
| ~20000        | 1.75s                            | 0                            | 75535                            |
| ~25000        | 2.66s                            | 1                            | 91995                            |
| ~30000        | 4.27s                            | 0                            | 102209                           |
| ~35000        | 6.56s                            | 3                            | 139711                           |
| ~40000        | 5.04s                            | 1                            | 160979                           |
| ~45000        | 8.38s                            | 0                            | 192273                           |
| ~50000        | 15.1s                            | 16                           | 274941                           |
| ~50000 + 1h   | 7.73s                            | 5                            | 276801                          |

`max(vmalert_iteration_duration_seconds)` — длительность одного цикла оценки всех правил в `vmalert`. При `evaluationInterval: 1m` и пике ~15.1 с запас по циклу eval ещё остаётся, но он снижается. `scrape_samples_scraped` для scrape-job `vmalert` растёт вместе с числом правил/целей.

### Насыщение vmselect: concurrent select и limit reached

| RULES         | vm_concurrent_select_current | vm_concurrent_select_limit_reached_total (increase[5m]) |
| ------------- | ---------------------------- | ------------------------------------------------------- |
| ~500          | 2                            | 0                                                       |
| ~5000         | 1                            | 0                                                       |
| ~10000        | 2                            | 0                                                       |
| ~15000        | 0                            | 0                                                       |
| ~20000        | 0                            | 0                                                       |
| ~25000        | 1                            | 0                                                       |
| ~30000        | 0                            | 0                                                       |
| ~35000        | 3                            | 0                                                       |
| ~40000        | 1                            | 1                                                       |
| ~45000        | 0                            | 106                                                     |
| ~50000        | 16                           | 5347                                                    |
| ~50000 + 1h   | 5                            | 0                                                       |

`vm_concurrent_select_current` — текущее число одновременно выполняемых select-запросов в `vmselect`. На насыщении (участок ~45 000 → ~50 000 `ALERTS`) значение выросло с 0 до 16. `vm_concurrent_select_limit_reached_total (increase[5m])` — сколько раз за 5 минут запросы упирались в лимит параллельности; ненулевые значения означают насыщение и риск ошибок/очередей. На том же участке показатель вырос со 106 до 5347, а примерно через 1 час после прохождения порога ~50 000 `ALERTS` вернулся к 0.

### Параметры очередей и таймаутов

Поиск выполняется и на `vmstorage`, и на `vmselect`. Дефолт `search.maxConcurrentRequests = 2` даёт на параллельной нагрузке от `vmalert` ответ 429 (vmselect) / 503 (vmstorage) и сообщение `couldn't start executing in 10s` (таймаут ожидания в очереди). В стенде лимиты подняты (смотри [`vmks-values.yaml.tftpl`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml.tftpl)):

| Параметр                                    | Значение | Комментарий                                                                                                                              |
| ------------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `search.maxConcurrentRequests` (дефолт)     | 2        | При параллельной нагрузке — 429 (vmselect) / 503 (vmstorage) и сообщение `couldn't start executing in 10s` (таймаут ожидания в очереди)  |
| `search.maxConcurrentRequests` (стенд)      | 32       | Поднято вместе с очередью и лимитом серий, чтобы снизить отказы при eval `vmalert`                                                       |
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

Тяжёлая часть (высококардинальные метрики и 20 тяжёлых `ExtraAlert0xx`) **всегда активна** — отдельного «второго режима» нет. Параметризация кардинальности через `app.cardinality.*` в [`chart/values.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/values.yaml) или env в `scripts/deploy_and_snapshot.py` (`APP_TENANTS`, `APP_ROUTES`, `APP_HIST_BUCKETS`, `APP_REGION`, `APP_VERSION`); число тяжёлых правил — `alerts.extra.count` (по умолчанию 40, из них 20 тяжёлых). Для отладки на малом числе приложений используйте «среднюю» ступень: `--set app.cardinality.tenants=5,app.cardinality.routes=5`.

Все 85 000 правил построены по единому шаблону — прямое сравнение результата PromQL-выражения с порогом (`expr > N`). В шаблоне чарта ([`chart/templates/vmrule.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/templates/vmrule.yaml)) 20 из 40 `ExtraAlert0xx` используют тяжёлые классы PromQL: subqueries (`[5m:1m]`), joins (`group_left`/`on(...)`), `label_join`/`label_replace`, `histogram_quantile` по высококардинальной оси (`sum by (le, tenant_id, route[, status_code])`), `quantile_over_time`/`stddev_over_time`/`predict_linear`. Распределение функций по правилам:

| Характеристика                              | Значение                       |
| ------------------------------------------- | ------------------------------ |
| Всего правил                                | 85 000                         |
| Правил на одно приложение                   | 50 (10 базовых + 40 `ExtraAlert`) |
| из них тяжёлых `ExtraAlert` на приложение   | 20 (subqueries/joins/`label_*`/histogram_quantile high-card/`*_over_time`+`predict_linear`) |
| `rate()`                                    | ~52 700 (62%)                  |
| `histogram_quantile`                        | 17 000 (20%)                   |
| `increase()`                                | 15 300 (18%)                   |
| `max_over_time` / `avg_over_time`           | 13 600 (16%)                   |
| Прямые сравнения gauge (`app_goroutines > N`) | 8 500 (10%)                  |
| `clamp_min` (обёртка делителя)              | 23 800 (28%)                   |
| Subqueries (`[5m:1m]`)                      | ~10 200 (12%, тяжёлая часть)   |
| Joins (`group_left`/`on(...)`)              | ~8 500 (10%, тяжёлая часть)    |
| `label_replace`/`label_join`                | ~5 100 (6%, тяжёлая часть)     |
| `histogram_quantile` по high-card оси       | ~5 100 (6%, тяжёлая часть)     |
| `quantile_over_time`/`stddev_over_time`/`predict_linear` | ~5 100 (6%, тяжёлая часть) |

> Проценты в сумме превышают 100%, так как одно правило может содержать несколько функций (например, `histogram_quantile(..., sum(rate(...)))`).

Тестовое приложение ([`app/main.go`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/app/main.go)) само генерирует метрики — `app_requests_total` (counter), `app_errors_total` (counter), `app_request_latency_seconds` (histogram с `prometheus.DefBuckets`), `app_goroutines` (gauge). Каждый алерт фильтруется по `job` label, привязанному к имени release, поэтому реальные ряды существуют и вычисление `rate()`/`histogram_quantile`/`increase()` обращается к TSDB.

Что это означает для интерпретации результатов:

- Измеренное потребление CPU и RAM компонентами `vmselect` и `vmstorage` отражает реальную стоимость выполнения ~85 000 алертов с базовыми функциями (`rate`, `histogram_quantile`, `increase`, `*_over_time`) на метриках низкой кардинальности: каждое приложение экспонирует лишь несколько рядов на метрику, а `job`-фильтр сужает выборку до одного release. Сложных агрегаций по тысячам серий нет.
- В production-окружении с высокой кардинальностью (тысячи pod'ов, контейнеров, сервисов в одном `job` или без фильтра) те же `rate()` и `histogram_quantile` будут сканировать значительно больше рядов, а добавление subqueries, joins и `label_*` дополнительно увеличит нагрузку на query engine.
- Тест фиксирует ориентир для профиля «много простых алертов на низкокардинальных метриках»; для тяжёлых PromQL-выражений результаты следует перепроверять отдельно.

## Выводы и линейные ориентиры

Отказоустойчивость цепочки `vmalert` → `victoria-metrics-operator` → `vmstorage` подтверждена: два `vmalert` с remoteRead/remoteWrite (`ALERTS`, `ALERTS_FOR_STATE`) и Alertmanager в кластерном режиме корректно восстанавливают состояние алертов после рестарта без потерь.

Приведённые ниже значения — рабочий baseline для capacity planning и раннего масштабирования, а не жёсткие универсальные лимиты для любого окружения. При планировании закладывайте запас по CPU, памяти, storage и параллелизму запросов заранее, ориентируясь на скорость прироста `ALERTS` и фактическую динамику `vmalert`/`vmselect`/`vmstorage` в своём стенде. Перед переносом ориентиров в production желательно повторить прогон на своей инфраструктуре и зафиксировать локальные коэффициенты роста.

- **Запас по циклу eval снизился, но укладывается в `interval`:** пик `max(vmalert_iteration_duration_seconds)` достигал ~15.1 с при `evaluationInterval` 1m.
- **Линейные ориентиры по `vmalert`:** в прогоне рост `ALERTS` давал примерно ~10 m CPU и ~27.9 MiB RAM на каждые 1000 `ALERTS`.
- **Линейные ориентиры по `vmstorage`:** на каждые 1000 `ALERTS` приходилось примерно ~57.8 MiB RAM (`container_memory_working_set_bytes`) и ~7.7 m CPU (`container_cpu_usage_seconds_total`).
- **Линейные ориентиры по `operator`:** в прогоне на каждые 1000 `ALERTS` приходилось примерно ~2.6 m CPU (`process_cpu_seconds_total`) и ~3.7 MiB RAM (`process_resident_memory_bytes`).
- **Control plane выдержал нагрузку:** при росте расчётного числа правил с ~500 до ~50 000 RPS API server оставался умеренным (~12.3–15.2 req/s), p99 задержки — 37–72 ms, CPU `kube-apiserver` — ~81–108m; в этом прогоне API server не стал главным узким местом относительно data plane.
- **Нагрузка на `vmselect`:** ориентир — около ~12.6 req/s на каждую 1000 `ALERTS`; при дальнейшем росте нужны ранний мониторинг таймаутов и масштабирование `vmselect` (при необходимости — `vmstorage`).
- **Косвенный ориентир нагрузки на `vmselect`:** около ~6.8 req/s на 1000 `ALERTS` по `vm_http_requests_total`; с ростом показателя ожидаемо увеличивается занятость параллелизма.

<!-- TODO: уточнить происхождение значения 6.8 req/s на 1000 ALERTS (отличается от 12.6 выше; возможно, рассчитан по другому срезу vm_http_requests_total или по снимку +1h) -->

- **Поведение `VMRule`/ConfigMap стабильно:** `operator` предсказуемо дробит правила около `ConfigDataBudgetBytes` (по умолчанию 512 KiB сжатых данных, при хард-лимите K8s 1 MiB); при росте количества правил число `vm-...-rulefiles-*` ConfigMap растёт ступенчато. При массовом поэтапном apply новые ConfigMap могут вызывать пересоздание Pod'а `vmalert`, поэтому для будущих изменений стоит использовать батчи или GitOps с контролируемым темпом.

## Итог

Тест показывает, что профиль «много простых алертов на низкокардинальных метриках» упирается прежде всего в `vmselect` и `vmstorage`: CPU и память растут линейно, а параллелизм запросов (`vm_concurrent_select_current`) насыщается на участке 45 000–50 000 `ALERTS`. Control plane (`kube-apiserver`, `operator`) остаётся в рамках. Полученные коэффициенты (~10 m CPU и ~27.9 MiB RAM на 1000 `ALERTS` для `vmalert`, ~57.8 MiB RAM на 1000 `ALERTS` для `vmstorage`) — отправная точка для планирования, которую нужно перепроверять на своей кардинальности и своём профиле PromQL.
