# Нагрузочное тестирование VictoriaMetrics: 67 500 алертов и поведение vmalert, vmselect и vmstorage на насыщении

Как 1350 приложений и 67 500 правил в `VMRule` нагружают кластер VictoriaMetrics, и какие ориентиры по CPU, памяти и параллелизму запросов это даёт для capacity planning.

## Зачем этот тест

VictoriaMetrics часто применяют как единый бэкенд для метрик и алертов: один `VMCluster` хранит ряды, `vmalert` оценивает правила и пишет состояние алертов обратно в TSDB, а `victoria-metrics-operator` синхронизирует `VMRule` из Kubernetes API в ConfigMap'ы и пересобирает Pod'ы `vmalert`. При росте числа правил до десятков тысяч появляется несколько конкретных вопросов:

- сохраняется ли оценка правил и состояние алертов (pending/firing) при рестартах `vmalert`, и какова цена восстановления;
- где физически возникают узкие места по CPU и памяти в data plane (`vmalert`, `vmselect`, `vmstorage`, `vminsert`, `vmagent`) и в control plane (`victoria-metrics-operator`, `kube-apiserver`);
- как растут RPS, задержки и CPU `kube-apiserver` при массовом применении `VMRule` и reconcile operator'а.

Ответы собраны в виде фиксированных снимков загрузки при прохождении порогов `count(ALERTS)` от ~500 до ~50 000. Измерения продолжают собираться в живом стенде; в статью входят только уже зафиксированные значения. Нагрузочные скрипты работают в фоне и не перезапускаются.

## Стенд: Yandex Managed K8s + vmks

Инфраструктура разворачивается Terraform в Yandex Cloud. Кластер Managed Kubernetes (`k8s.tf`, версия 1.33) состоит из master (управляемый, вне кластера) и группы из 32 узлов `standard-v3`, 4 vCPU / 8 ГБ каждый, preemptible, с распределением по трём зонам (`ru-central1-b/-d/-e`). Ноды не имеют публичных IP: `network_interface.nat = false`, исходящий трафик из приватных подсетей идёт через NAT-шлюз и Route Table (`net.tf`). Публичный адрес есть только у балансировщика `ingress-nginx`, из которого через `sslip.io` формируются FQDN Grafana и vmselect. Исходники инфраструктуры: [`k8s.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/k8s.tf), [`net.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/net.tf), [`ip-dns.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/ip-dns.tf), [`monitoring.tf`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/monitoring.tf).

Сам стек мониторинга установлен через Helm-чарт `victoria-metrics-k8s-stack` (версия 0.90.2) в namespace `vmks`:

```bash
helm upgrade --install vmks oci://ghcr.io/victoriametrics/helm-charts/victoria-metrics-k8s-stack \
  --namespace vmks --create-namespace \
  --version 0.90.2 \
  --wait --values vmks-values.yaml
```

Values генерируются из шаблона [`vmks-values.yaml.tftpl`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml.tftpl) через Terraform `templatefile` (`monitoring.tf`); FQDN Grafana формируется из публичного IP ingress-nginx (`terraform output grafana_url`). Пароль Grafana извлекается из секрета:

```bash
kubectl get secret vmks-grafana -n vmks -o jsonpath='{.data.admin-password}' | base64 --decode; echo
```

Все компоненты vmks (`vmstorage`, `vmselect`, `vminsert`, `vmalert`, `vmagent`, `alertmanager`, `victoria-metrics-operator`, `node-exporter`, `kube-state-metrics`) запускаются с `priorityClassName: vmks-critical`, чтобы не вытесняться apps-нагрузкой стенда ([`priority-class.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/priority-class.yaml)).

В Yandex Managed K8s control-plane компоненты (`kube-controller-manager`, `kube-scheduler`, `kube-etcd`) недоступны для скрейпинга — master управляемый и вне кластера. В `vmks-values.yaml.tftpl` отключены соответствующие scrape-job и recording-правила (`kubeControllerManager`, `kubeScheduler`, `kubeEtcd`, группы `etcd`, `kubernetes-system-scheduler`, `kubernetes-system-controller-manager`, `kube-scheduler.rules`), иначе `vmagent` плодит `ScrapePoolHasNoTargets`, а `vmalert` — `RecordingRulesNoData`.

Топология стека (из `vmks-values.yaml.tftpl`):

- `vmcluster` с `replicationFactor: 3` и тремя `vmstorage` (по 20 Gi `yc-network-ssd`, PDB `minAvailable: 2`, `topologySpreadConstraints` по зонам);
- три `vmselect` (PDB `minAvailable: 2`, каждый запрос расходится параллельно по `vmstorage` во всех зонах);
- два `vminsert` и два `vmagent` (HA-репликация скрейпинга, `dedup.minScrapeInterval: 20s`);
- два `vmalert` (`evaluationInterval: 1m`, PDB `minAvailable: 1`);
- два `alertmanager` в кластерном режиме с persistence для silences;
- два `victoria-metrics-operator` с leader election, PDB `minAvailable: 1`.

## Что генерирует нагрузку

Нагрузка — 1350 экземпляров приложения Golden Signal App, каждый в своём namespace как отдельный Helm release. Приложение ([`app/main.go`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/app/main.go)) экспонирует четыре метрики через `prometheus/client_golang`:

- `app_requests_total` (counter) — счётчик входящих HTTP-запросов;
- `app_errors_total` (counter) — счётчик ответов с ошибкой;
- `app_request_latency_seconds` (histogram, `prometheus.DefBuckets`) — латентность обработки;
- `app_goroutines` (gauge) — число горутин, обновляется раз в 5 с.

Каждые 2 секунды приложение само отправляет фоновый запрос на `/work`, который спит 100–500 мс и с вероятностью 20% отвечает `500` (растёт `app_errors_total`). Эндпоинт `/metrics` отдаёт метрики в формате Prometheus.

На каждый release через Helm chart ([`chart`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/tree/main/chart)) создаются `Service`, `VMServiceScrape` (автосбор метрик `vmagent`'ом) и `VMRule` с правилами. `VMRule` ([`chart/templates/vmrule.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/templates/vmrule.yaml)) содержит 10 базовых алертов (`HighErrorRate`, `CriticalErrorRate`, `HighLatency`, `HighLatencyP99`, `HighAverageLatency`, `HighGoroutineCount`, `CriticalGoroutineCount`, `LowTraffic`, `TrafficSpike`, `ErrorBurst`) и 40 дополнительных `ExtraAlert0xx`, итого 50 на приложение. Все выражения фильтруются по `job` label, равному имени release, поэтому каждый `VMRule` работает только со своими рядами и ряды реально существуют в TSDB.

**Итого при 1350 экземплярах:** 1350 Deployment, 1350 Service, 1350 `VMServiceScrape`, 1350 `VMRule`, 67 500 правил.

Развёртывание выполняется скриптами (смотри шаги ниже); здесь приводятся только зафиксированные результаты замеров, сами нагрузочные скрипты продолжают работать в стенде и не перезапускаются.

### Ресурсные требования генератора нагрузки

При 1350 экземплярах (requests/limits из [`chart/values.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/values.yaml)):


TODO: Надо уточнить
| Ресурс          | На 1 pod | На 1350 pods       |
| --------------- | -------- | ------------------ |
| CPU requests    | 1m       | 1 000m (1 core)    |
| CPU limits      | 10m      | 10 000m (10 cores) |
| Memory requests | 8Mi      | ~10.55 GiB         |
| Memory limits   | 20Mi     | ~26.37 GiB         |

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
scripts/generate-app-names.sh 1350
```

Шаг 2 — развёртывание. Каждый app устанавливается как отдельный Helm release в отдельный namespace (имя namespace = имя приложения):

```bash
scripts/deploy-apps.sh
```

Шаг 3 — проверка статуса (число Helm releases, статусы Pod'ов, потребление ресурсов, количество `VMRule` и `VMServiceScrape`):

```bash
scripts/status-apps.sh
```

Шаг 4 — удаление приложений (параметры `NAMES_FILE`, `TARGET_APPS`, `PARALLEL`, `DELETE_NAMESPACES`):

```bash
scripts/delete-apps.sh
```

### Сбор снимков Capacity Planning

Чтобы зафиксировать загрузку при прохождении порогов `count(ALERTS)` (~500, 5000, ... 50 000), снимки собираются скриптом [`scripts/fetch_capacity_snapshots.py`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/scripts/fetch_capacity_snapshots.py). Скрипт опрашивает `count(ALERTS)` каждые `POLL_INTERVAL` секунд, и при достижении очередного порога делает instant-запрос всех метрик из `QUERIES` через Prometheus API vmselect (CPU, память подов `vmks`, нагрузка на `kube-apiserver`, `vm_http_requests_total` компонент, `vmalert_iteration_duration_seconds`, `vm_concurrent_select_current`, `scrape_samples_scraped`). После каждого снимка выдерживается `MIN_SNAPSHOT_GAP` (по умолчанию 120 с), чтобы rate-метрики устаканились. Скрипт использует только стандартную библиотеку Python 3:

```bash
python3 scripts/fetch_capacity_snapshots.py
```

Результаты — `capacity_snapshots.json` (сырые значения) и `capacity_snapshots.txt` (форматированная таблица). Пороги скрипта: 500, 5000, 10000, 15000, 20000, 25000, 30000, 35000, 40000, 45000, 50000, 55000, 60000, 65000, 67500 (последний равен `TARGET_APPS * ALERTS_PER_APP`).

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

Ниже — зафиксированные снимки загрузки по порогам `count(ALERTS)`. Столбец `~50000 + 1h` — снимок, сделанный примерно через 1 час после прохождения порога ~50 000 `ALERTS`. Все значения — средние на pod (`avg(...)`), кроме явно отмеченных (`max(...)`).

### CPU (в среднем на pod)

| ALERTS        | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
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

| ALERTS        | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
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

| ALERTS        | API Server RPS | API Server p99 lat | API Server CPU | vmselect HTTP RPS | vmstorage HTTP RPS | vminsert HTTP RPS |
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

| ALERTS        | vmalert_iteration_duration (max) | vm_concurrent_select_current | vmagent scrape_samples (vmalert) |
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

| ALERTS        | vm_concurrent_select_current | vm_concurrent_select_limit_reached_total (increase[5m]) |
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

Все 67 500 правил построены по единому шаблону — прямое сравнение результата PromQL-выражения с порогом (`expr > N`). В шаблоне чарта ([`chart/templates/vmrule.yaml`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chart/templates/vmrule.yaml)) не используются ни `or vector(fallback)`, ни `absent(...)`, ни subqueries (`[5m:1m]`), ни joins (`group_left`/`group_right`), ни `label_join`/`label_replace`. Распределение функций по правилам:

| Характеристика                              | Значение                       |
| ------------------------------------------- | ------------------------------ |
| Всего правил                                | 67 500                         |
| Правил на одно приложение                   | 50 (10 базовых + 40 `ExtraAlert`) |
| `rate()`                                    | ~36 450 (54%)                  |
| `histogram_quantile`                        | 13 500 (20%)                   |
| `increase()`                                | 12 150 (18%)                   |
| `max_over_time` / `avg_over_time`           | 10 800 (16%)                   |
| Прямые сравнения gauge (`app_goroutines > N`) | 8 100 (12%)                  |
| `clamp_min` (обёртка делителя)              | 18 900 (28%)                   |
| Subqueries / joins / `label_*`              | 0                              |

> Проценты в сумме превышают 100%, так как одно правило может содержать несколько функций (например, `histogram_quantile(..., sum(rate(...)))`).

Тестовое приложение ([`app/main.go`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/app/main.go)) само генерирует метрики — `app_requests_total` (counter), `app_errors_total` (counter), `app_request_latency_seconds` (histogram с `prometheus.DefBuckets`), `app_goroutines` (gauge). Каждый алерт фильтруется по `job` label, привязанному к имени release, поэтому реальные ряды существуют и вычисление `rate()`/`histogram_quantile`/`increase()` обращается к TSDB.

Что это означает для интерпретации результатов:

- Измеренное потребление CPU и RAM компонентами `vmselect` и `vmstorage` отражает реальную стоимость выполнения ~67 500 алертов с базовыми функциями (`rate`, `histogram_quantile`, `increase`, `*_over_time`) на метриках низкой кардинальности: каждое приложение экспонирует лишь несколько рядов на метрику, а `job`-фильтр сужает выборку до одного release. Сложных агрегаций по тысячам серий нет.
- В production-окружении с высокой кардинальностью (тысячи pod'ов, контейнеров, сервисов в одном `job` или без фильтра) те же `rate()` и `histogram_quantile` будут сканировать значительно больше рядов, а добавление subqueries, joins и `label_*` дополнительно увеличит нагрузку на query engine.
- Тест фиксирует ориентир для профиля «много простых алертов на низкокардинальных метриках»; для тяжёлых PromQL-выражений результаты следует перепроверять отдельно.

## Выводы и линейные ориентиры

Отказоустойчивость цепочки `vmalert` → `victoria-metrics-operator` → `vmstorage` подтверждена: два `vmalert` с remoteRead/remoteWrite (`ALERTS`, `ALERTS_FOR_STATE`) и Alertmanager в кластерном режиме корректно восстанавливают состояние алертов после рестарта без потерь.

Приведённые ниже значения — рабочий baseline для capacity planning и раннего масштабирования, а не жёсткие универсальные лимиты для любого окружения. При планировании закладывайте запас по CPU, памяти, storage и параллелизму запросов заранее, ориентируясь на скорость прироста `ALERTS` и фактическую динамику `vmalert`/`vmselect`/`vmstorage` в своём стенде. Перед переносом ориентиров в production желательно повторить прогон на своей инфраструктуре и зафиксировать локальные коэффициенты роста.

- **Запас по циклу eval снизился, но укладывается в `interval`:** пик `max(vmalert_iteration_duration_seconds)` достигал ~15.1 с при `evaluationInterval` 1m.
- **Линейные ориентиры по `vmalert`:** в прогоне рост `ALERTS` давал примерно ~10 m CPU и ~27.9 MiB RAM на каждые 1000 `ALERTS`.
- **Линейные ориентиры по `vmstorage`:** на каждые 1000 `ALERTS` приходилось примерно ~57.8 MiB RAM (`container_memory_working_set_bytes`) и ~7.7 m CPU (`container_cpu_usage_seconds_total`).
- **Линейные ориентиры по `operator`:** в прогоне на каждые 1000 `ALERTS` приходилось примерно ~2.6 m CPU (`process_cpu_seconds_total`) и ~3.7 MiB RAM (`process_resident_memory_bytes`).
- **Control plane выдержал нагрузку:** при росте `count(ALERTS)` с ~500 до ~50 000 RPS API server оставался умеренным (~12.3–15.2 req/s), p99 задержки — 37–72 ms, CPU `kube-apiserver` — ~81–108m; в этом прогоне API server не стал главным узким местом относительно data plane.
- **Нагрузка на `vmselect`:** ориентир — около ~12.6 req/s на каждую 1000 `ALERTS`; при дальнейшем росте нужны ранний мониторинг таймаутов и масштабирование `vmselect` (при необходимости — `vmstorage`).
- **Косвенный ориентир нагрузки на `vmselect`:** около ~6.8 req/s на 1000 `ALERTS` по `vm_http_requests_total`; с ростом показателя ожидаемо увеличивается занятость параллелизма.

<!-- TODO: уточнить происхождение значения 6.8 req/s на 1000 ALERTS (отличается от 12.6 выше; возможно, рассчитан по другому срезу vm_http_requests_total или по снимку +1h) -->

- **Поведение `VMRule`/ConfigMap стабильно:** `operator` предсказуемо дробит правила около `ConfigDataBudgetBytes` (по умолчанию 512 KiB сжатых данных, при хард-лимите K8s 1 MiB); при росте количества правил число `vm-...-rulefiles-*` ConfigMap растёт ступенчато. При массовом поэтапном apply новые ConfigMap могут вызывать пересоздание Pod'а `vmalert`, поэтому для будущих изменений стоит использовать батчи или GitOps с контролируемым темпом.

## Итог

Тест показывает, что профиль «много простых алертов на низкокардинальных метриках» упирается прежде всего в `vmselect` и `vmstorage`: CPU и память растут линейно, а параллелизм запросов (`vm_concurrent_select_current`) насыщается на участке 45 000–50 000 `ALERTS`. Control plane (`kube-apiserver`, `operator`) остаётся в рамках. Полученные коэффициенты (~10 m CPU и ~27.9 MiB RAM на 1000 `ALERTS` для `vmalert`, ~57.8 MiB RAM на 1000 `ALERTS` для `vmstorage`) — отправная точка для планирования, которую нужно перепроверять на своей кардинальности и своём профиле PromQL.
