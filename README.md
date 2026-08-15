# Нагрузочное тестирование VictoriaMetrics большим количеством алертов

> План доработок по отказоустойчивости стенда — см. [TODO.md](TODO.md).

## Цели

1. **Отказоустойчивость и непрерывность:** как при десятках тысяч активных `VMRule` сохраняются оценка правил, состояние алертов (`for`, pending/firing), восстановление после рестартов `vmalert`, и какие риски для этой цепочки возникают у оператора, `vmalert` и хранилища.
2. **Потребление ресурсов:** где узкие места по CPU и памяти (`vmalert`, VMCluster, Operator) и как растут затраты при росте числа алертов.
3. **Нагрузка на API server:** как растут RPS, задержки и CPU `kube-apiserver` при массовом применении `VMRule` и reconcile Operator'а.

Дополнительно — практическая картина стека VictoriaMetrics в Kubernetes при такой нагрузке и набор метрик для контроля.

## Архитектура и стенд

### Infrastructure

**Кластер:** 13 ноды Kubernetes v1.32.1 на Yandex Cloud (Ubuntu 22.04.5 LTS, containerd 1.7.27).

## Установка

### victoria-metrics-k8s-stack

```bash
helm upgrade --install vmks oci://ghcr.io/victoriametrics/helm-charts/victoria-metrics-k8s-stack \
  --namespace vmks --create-namespace \
  --version 0.90.1 \
  --wait --values vmks-values.yaml
```

Исходный код файла [vmks-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml). Включает Grafana с ingress на [grafana.apatsev.org.ru](http://grafana.apatsev.org.ru).

Получение пароля Grafana:

```bash
kubectl get secret vmks-grafana -n vmks -o jsonpath='{.data.admin-password}' | base64 --decode; echo
```

### PriorityClass для VictoriaMetrics

Компоненты vmks (vmstorage, vmselect, vminsert, vmalert, vmagent, alertmanager, operator) запускаются с `priorityClassName: vmks-critical`, чтобы не вытесняться apps-нагрузкой стенда. Применить вручную до установки чарта:

```bash
kubectl apply -f priority-class.yaml
```

Исходный код файла [priority-class.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/priority-class.yaml).

### VictoriaLogs

[VictoriaLogs](https://docs.victoriametrics.com/victorialogs/) — хранилище логов с поддержкой LogsQL.

**Требование:** VictoriaMetrics K8s Stack установлен первым (CRD VMServiceScrape).

**1. VictoriaLogs cluster (vlselect, vlinsert, vlstorage):**

```bash
helm upgrade --install victoria-logs-cluster oci://ghcr.io/victoriametrics/helm-charts/victoria-logs-cluster \
  --namespace victoria-logs-cluster \
  --create-namespace \
  --version 0.2.6 \
  --wait \
  --timeout 15m \
  -f victoria-logs-cluster-values.yaml
```

Исходный код файла [victoria-logs-cluster-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/victoria-logs-cluster-values.yaml).

Проверка: `kubectl get pods -n victoria-logs-cluster`. Ingress для vlselect: `victorialogs.apatsev.org.ru` (из values).

**2. Victoria-logs-collector (сбор логов с подов кластера):**

```bash
helm upgrade --install victoria-logs-collector oci://ghcr.io/victoriametrics/helm-charts/victoria-logs-collector \
  --namespace victoria-logs-collector \
  --create-namespace \
  --version 0.3.5 \
  --wait \
  --timeout 15m \
  -f victoria-logs-collector-values.yaml
```

Исходный код файла [victoria-logs-collector-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/victoria-logs-collector-values.yaml).

### Развёртывание 1000 приложений для генерации метрик и алертов

Для нагрузочного тестирования VictoriaMetrics можно развернуть 1000 экземпляров приложения Golden Signal App. Каждый экземпляр:
- генерирует метрики (`app_requests_total`, `app_errors_total`, `app_request_latency_seconds`, `app_goroutines`);
- создаёт `VMServiceScrape` для автоматического сбора метрик;
- создаёт `VMRule` с 50 алертами по умолчанию (10 базовых + 40 дополнительных; общее число задаётся `ALERTS_PER_APP` в `deploy-apps-day*.sh`, число дополнительных — `alerts.extra.count` в chart).

**Итого при 1000 экземплярах (по умолчанию):** 1000 Deployment, 1000 Service, 1000 VMServiceScrape, 1000 VMRule (50 000 алертов).

Исходный код приложения — [app/](https://github.com/patsevanton/performance-test-alerts-victoriametrics/tree/main/app), Helm chart — [chart/](https://github.com/patsevanton/performance-test-alerts-victoriametrics/tree/main/chart).

#### Требования

- Kubernetes-кластер с установленным VictoriaMetrics Operator (входит в `victoria-metrics-k8s-stack`)
- `helm` >= 3.x
- `kubectl` с доступом к кластеру
- `xargs` (стандартная утилита Linux)

#### Шаг 1: Генерация случайных имён

Скрипт создаёт файл `app-names.txt` со случайными уникальными именами вида `app-{adjective}-{noun}-{number}`:

```bash
cd scripts
./generate-app-names.sh 1350
```

#### Шаг 2: Развёртывание приложений

Каждый app устанавливается как отдельный Helm release в отдельный namespace (имя namespace = имя приложения).

Запуск (без пауз между установками):

```bash
./deploy-apps.sh
```

По умолчанию скрипт развёртывает 1000 приложений (`app-1..app-1000`, переменные `START_INDEX` и `TARGET_APPS`).

**Снимки Capacity Planning:** чтобы собрать замеры загрузки при ~500, 5000, … 50000 активных `ALERTS`, запустите **до начала** `deploy-apps.sh` в отдельном терминале скрипт:

```bash
python3 scripts/fetch_capacity_snapshots.py
```
Он опрашивает `count(ALERTS)` каждые `POLL_INTERVAL` секунд и фиксирует снимок при достижении очередного порога. Если запустить его после деплоя, первые пороги будут пропущены. Подробности — в разделе [Capacity Planning](#capacity-planning).

#### Шаг 3: Проверка статуса

```bash
./status-apps.sh
```

Скрипт показывает: количество Helm releases, статусы Pod'ов, потребление ресурсов, количество VMRule и VMServiceScrape.

#### Шаг 4: Удаление приложений

```bash
./delete-apps.sh
```

Параметры `NAMES_FILE`, `TARGET_APPS`, `PARALLEL` аналогичны скрипту `deploy-apps.sh`.
Дополнительно `DELETE_NAMESPACES=true|false` (по умолчанию `true`) управляет удалением namespace после `helm uninstall`.

#### Ресурсные требования

При 1000 экземплярах (requests из `values.yaml`):

| Ресурс | На 1 pod | На 1000 pods |
| ------ | -------- | ------------ |
| CPU requests | 1m | 1 000m (1 core) |
| CPU limits | 10m | 10 000m (10 cores) |
| Memory requests | 8Mi | ~7.8 GiB |
| Memory limits | 32Mi | ~31.3 GiB |

Рекомендуется кластер с достаточным запасом ресурсов. Ресурсы можно уменьшить, отредактировав `chart/values.yaml`.

#### Структура нагрузки

Каждое приложение:
- каждые 2 секунды генерирует фоновый HTTP-запрос к `/work`;
- с вероятностью 20% возвращает ошибку 500 → растёт `app_errors_total`;
- латентность 100-500 мс → гистограмма `app_request_latency_seconds`;
- считает горутины → gauge `app_goroutines`.

Алерты в каждом VMRule фильтруются по `job` label, привязанному к имени release, что обеспечивает уникальность на каждый экземпляр.

### Механизм распределения алертов, перезапуски vmalert и состояние

Если посмотреть на `vmalert` (статус Pod'ов, счётчик рестартов), видно, что он перезапускается: при появлении **нового** ConfigMap с правилами под пересобирается с новыми `volume`/`volumeMount`, из‑за чего происходит rolling restart. Если же число ConfigMap'ов не меняется, правила могут подхватываться через SIGHUP без рестарта Pod'а. Восстановление состояния алертов из VictoriaMetrics выполняется **один раз при старте** процесса `vmalert`.

#### Хранение правил в ConfigMap'ах

VictoriaMetrics Operator хранит все правила оповещений (`VMRule`) в ConfigMap'ах с префиксом `rulefiles`. Из-за ограничения Kubernetes на размер ConfigMap (~1 MiB) при росте количества правил Operator дробит их на несколько ConfigMap'ов.

Процесс работы:

1. **Reconcile-цикл Operator'a** (~каждые 60 сек) собирает **все** `VMRule` из всех namespace'ов, подходящих под selector.
2. Operator пытается упаковать правила в ConfigMap `rulefiles-0`.
3. При превышении лимита — разбивает на несколько ConfigMap'ов:
  ```
   vm-vmks-victoria-metrics-k8s-stack-rulefiles-0
   vm-vmks-victoria-metrics-k8s-stack-rulefiles-1
   ...
  ```

#### Сохранение состояния (State Persistence)

vmalert настроен на запись и чтение состояния из VictoriaMetrics:

- **-remoteWrite.url** — при каждой оценке vmalert записывает ряды `ALERTS` и `ALERTS_FOR_STATE` в VMCluster (через vminsert);
- **-remoteRead.url** — при **старте** процесса vmalert восстанавливает состояние, запрашивая ряды `ALERTS_FOR_STATE` (через vmselect).

**ALERTS_FOR_STATE** содержит полную информацию о состоянии каждого алерта (`ActiveAt`, `for` duration и т.д.), необходимую для восстановления после рестарта. При запуске vmalert однократно читает этот ряд для восстановления.

## Capacity Planning

### Скрипт `scripts/fetch_capacity_snapshots.py`

**Что делает:** Скрипт автоматически собирает "снимки" (замеры) загрузки системы VictoriaMetrics в заранее выбранные моменты времени — примерно при 500, 5000, 10000, ... 50000 активных алертах. Для этого он запрашивает метрики напрямую у vmselect через Prometheus API — такие как загрузка CPU, используемая память подов (в пространстве имён `vmks`), нагрузка на kube-apiserver и количество HTTP-запросов к компонентам vmselect/vmstorage/vminsert.

**Как запустить** (только стандартная библиотека Python 3, зависимости не устанавливаются):

```bash
python3 scripts/fetch_capacity_snapshots.py
```

Исходный код: [scripts/fetch_capacity_snapshots.py](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/scripts/fetch_capacity_snapshots.py).

### Ресурсы подов при росте нагрузки

#### CPU (в среднем на pod)


| ALERTS | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500   | 14m     | 77m       | 13m      | 15m      | 28m     | 6m       |
| ~5000  | 33m     | 58m       | 55m      | 11m      | 33m     | 17m      |
| ~10000 | 82m     | 87m       | 81m      | 15m      | 51m     | 21m      |
| ~15000 | 133m    | 179m      | 87m      | 20m      | 69m     | 26m      |
| ~20000 | 215m    | 218m      | 95m      | 23m      | 73m     | 33m      |
| ~25000 | 179m    | 226m      | 140m     | 24m      | 82m     | 38m      |
| ~30000 | 207m    | 170m      | 146m     | 23m      | 89m     | 42m      |
| ~35000 | 216m    | 198m      | 187m     | 32m      | 124m    | 67m      |
| ~40000 | 260m    | 208m      | 201m     | 33m      | 147m    | 77m      |
| ~45000 | 314m    | 285m      | 255m     | 38m      | 174m    | 78m      |
| ~50000 | 509m    | 458m      | 288m     | 48m      | 250m    | 135m     |
| ~50000 + 1h | 277m | 272m    | 196m     | 44m      | 240m    | 32m      |


#### Memory (в среднем на pod)


| ALERTS | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500   | 45Mi    | 243Mi     | 32Mi     | 116Mi    | 55Mi    | 42Mi     |
| ~5000  | 33Mi    | 370Mi     | 72Mi     | 57Mi     | 72Mi    | 63Mi     |
| ~10000 | 227Mi   | 833Mi     | 193Mi    | 87Mi     | 99Mi    | 86Mi     |
| ~15000 | 324Mi   | 1065Mi    | 119Mi    | 103Mi    | 122Mi   | 116Mi    |
| ~20000 | 336Mi   | 1387Mi    | 135Mi    | 115Mi    | 118Mi   | 87Mi     |
| ~25000 | 508Mi   | 1635Mi    | 390Mi    | 122Mi    | 151Mi   | 152Mi    |
| ~30000 | 575Mi   | 1648Mi    | 341Mi    | 142Mi    | 148Mi   | 138Mi    |
| ~35000 | 768Mi   | 1978Mi    | 474Mi    | 146Mi    | 191Mi   | 220Mi    |
| ~40000 | 946Mi   | 2308Mi    | 250Mi    | 126Mi    | 222Mi   | 271Mi    |
| ~45000 | 1036Mi  | 2713Mi    | 607Mi    | 134Mi    | 267Mi   | 167Mi    |
| ~50000 | 1426Mi  | 3106Mi    | 380Mi    | 186Mi    | 339Mi   | 225Mi    |
| ~50000 + 1h | 1929Mi | 2400Mi | 293Mi    | 163Mi    | 185Mi   | 323Mi    |


### RPS и операционные метрики


| ALERTS | API Server RPS | API Server p99 lat | API Server CPU | vmselect HTTP RPS | vmstorage HTTP RPS | vminsert HTTP RPS |
| ------ | -------------- | ------------------ | -------------- | ----------------- | ------------------ | ----------------- |
| ~500   | 15.2           | 37 ms              | 94m            | 22.9              | 0.1                | 5                 |
| ~5000  | 12.7           | 37 ms              | 82m            | 42.1              | 0.1                | 5.2               |
| ~10000 | 12.3           | 37 ms              | 81m            | 123.4             | 0.1                | 5.7               |
| ~15000 | 12.5           | 44 ms              | 84m            | 107.5             | 0.1                | 5.9               |
| ~20000 | 13             | 47 ms              | 88m            | 92.7              | 0.1                | 6.1               |
| ~25000 | 13.3           | 48 ms              | 93m            | 183.1             | 0.1                | 6.3               |
| ~30000 | 12.9           | 50 ms              | 89m            | 250               | 0.1                | 6.4               |
| ~35000 | 12.9           | 46 ms              | 90m            | 346.3             | 0.1                | 6.7               |
| ~40000 | 13             | 45 ms              | 92m            | 396.8             | 0.1                | 7.1               |
| ~45000 | 13.2           | 48 ms              | 97m            | 476.2             | 0.1                | 7.6               |
| ~50000 | 14.3           | 72 ms              | 108m           | 644.2             | 0.1                | 8.6               |
| ~50000 + 1h | 12.1      | 44 ms              | 85m            | 350.1             | 0.1                | 8.5               |


#### Метрики компонентов VictoriaMetrics stack, выросшие при нагрузке

| ALERTS | vmalert_iteration_duration (max) | vm_concurrent_select_current | vmagent scrape_samples (vmalert) |
| ------ | -------------------------------- | ---------------------------- | -------------------------------- |
| ~500   | 0.97s                            | 2                            | 3463                             |
| ~5000  | 0.85s                            | 1                            | 21025                            |
| ~10000 | 1.68s                            | 2                            | 46263                            |
| ~15000 | 1.94s                            | 0                            | 57763                            |
| ~20000 | 1.75s                            | 0                            | 75535                            |
| ~25000 | 2.66s                            | 1                            | 91995                            |
| ~30000 | 4.27s                            | 0                            | 102209                           |
| ~35000 | 6.56s                            | 3                            | 139711                           |
| ~40000 | 5.04s                            | 1                            | 160979                           |
| ~45000 | 8.38s                            | 0                            | 192273                           |
| ~50000 | 15.1s                            | 16                           | 274941                           |
| ~50000 + 1h | 7.73s                        | 5                            | 276801                           |

#### Таблица для `vm_concurrent_select_current` и `vm_concurrent_select_limit_reached_total`

| ALERTS | vm_concurrent_select_current | vm_concurrent_select_limit_reached_total (increase[5m]) |
| ------ | ---------------------------- | -------------------------------------------------------- |
| ~500   | 2                            | 0                                                        |
| ~5000  | 1                            | 0                                                        |
| ~10000 | 2                            | 0                                                        |
| ~15000 | 0                            | 0                                                        |
| ~20000 | 0                            | 0                                                        |
| ~25000 | 1                            | 0                                                        |
| ~30000 | 0                            | 0                                                        |
| ~35000 | 3                            | 0                                                        |
| ~40000 | 1                            | 1                                                        |
| ~45000 | 0                            | 106                                                      |
| ~50000 | 16                           | 5347                                                     |
| ~50000 + 1h | 5                       | 0                                                        |

- `vm_concurrent_select_current` — текущее число одновременно выполняемых select-запросов в `vmselect`; рост показывает более сильную занятость параллелизма. На насыщении (участок ~45000 -> ~50000 `ALERTS`) значение выросло с **0** до **16**.
- `vm_concurrent_select_limit_reached_total (increase[5m])` — сколько раз за 5 минут запросы упирались в лимит параллельности; ненулевые значения означают насыщение и риск ошибок/очередей. На том же участке показатель вырос с **106** до **5347**, а примерно через 1 час после остановки добавления новых `ALERTS` вернулся к **0**.

Ориентиры по очереди и таймаутам поиска (из конфигурации стенда и дефолтов VictoriaMetrics; подробности — `vmks-values.yaml`):
| Параметр                                    | Значение                       | Комментарий                                                                                                                                         |
| ------------------------------------------- | ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `search.maxConcurrentRequests` (дефолт)     | **2**                          | При параллельной нагрузке — **429** (vmselect) / **503** (vmstorage) и сообщение **«couldn't start executing in 10s»** (таймаут ожидания в очереди) |
| `search.maxConcurrentRequests` (стенд)      | **16**                         | Поднято вместе с очередью и лимитом серий, чтобы снизить отказы при eval vmalert                                                                    |
| `search.maxQueueDuration` (стенд)           | **60s**                        | Максимальное ожидание в очереди перед стартом выполнения запроса                                                                                    |
| `search.maxQueryDuration` (vmselect, стенд) | **300s**                       | Согласовано с `queryTimeout` datasource Grafana (**300s**), иначе обрезка раньше клиента                                                            |

## Скриншоты Grafana (места под вставку)

Ниже скриншоты, где наблюдалась заметная динамика. Графики без треда (ровная линия) не включены.

- Kubernetes API dashboard (RPS / p99 latency / CPU).

![Kubernetes API dashboard](screenshots/kubernetes-api-dashboard.png)

- `vmalert`: iteration duration и динамика активных алертов.

![vmalert dashboard](screenshots/vmalert-dashboard.png)

- `vmselect`: HTTP RPS, concurrent select, saturation/limit reached.

![vmselect dashboard](screenshots/vmselect-dashboard.png)

- `vmstorage`: CPU/Memory (рост на верхних стадиях нагрузки).

![vmstorage dashboard](screenshots/vmstorage-dashboard.png)

- `vmagent`: scrape samples (рост с числом правил/целей).

![vmagent dashboard](screenshots/vmagent-dashboard.png)

## Выводы

**Отказоустойчивость подтверждена:** Конфигурация с двумя репликами vmalert, remoteRead/remoteWrite (`ALERTS`, `ALERTS_FOR_STATE`) и Alertmanager в кластерном режиме корректно восстанавливает состояние алертов после рестарта без потерь и без дублирования уведомлений.

### Важная оговорка: сложность алертов и реальная нагрузка на vmselect / vmstorage / vmalert

Нагрузка на `vmselect`, `vmstorage` и `vmalert` в данном тесте **существенно ниже**, чем при аналогичном количестве алертов в production-окружении. Причина — структура используемых выражений:

| Характеристика | Значение |
| -------------- | -------- |
| Всего правил | 50 000 |
| Используют `(real_query) or vector(fallback)` | 45 368 (90,7%) |
| `absent(...)` без обращения к TSDB | 4 632 (9,3%) |
| `histogram_quantile` (внутри `or vector()`) | 3 136 |
| `rate()` (внутри `or vector()`) | 21 146 |
| `avg_over_time` (внутри `or vector()`) | 1 725 |
| Subqueries (`[5m:1m]` и подобные) | 0 |
| Joins (`group_left` / `group_right`) | 0 |
| `label_join` / `label_replace` | 0 |

Все 50 000 правил построены по схеме `(real_metric_expression) or vector(N)`. Поскольку на тестовом стенде реальные прикладные метрики (например, `http_request_duration_seconds_bucket`, `kube_pod_container_status_restarts_total` от тестовых сервисов) **не существуют**, реальная часть выражения возвращает пустой результат, и вычисление сводится к `vector(N) > threshold` — практически бесплатная операция для query engine.

**Что это означает для интерпретации результатов:**

- Измеренное потребление CPU и RAM компонентами `vmselect` и `vmstorage` отражает главным образом **overhead управления 50 000 алертами** (запись/чтение рядов `ALERTS` и `ALERTS_FOR_STATE`), а **не** стоимость выполнения тяжёлых запросов к реальным данным.
- В production-окружении, где метрики реально существуют и имеют высокую кардинальность (тысячи pod'ов, контейнеров, сервисов), нагрузка на `vmselect` и `vmstorage` будет **значительно выше** из-за: сканирования множества `le`-бакетов в `histogram_quantile`, вычисления `rate()` по реальным временным рядам, агрегаций по большому числу серий.
- Тест показывает **нижнюю границу** потребления ресурсов — «стоимость самого факта существования 50 000 алертов» без учёта стоимости вычисления сложных выражений.
- Для оценки реальной нагрузки в production следует дополнительно закладывать ресурсы на тяжесть самих PromQL-запросов (subqueries, joins, высококардинальные агрегации), которые в данном тесте отсутствовали.

### Ключевые результаты

Эти значения стоит использовать как рабочий baseline для capacity planning и раннего масштабирования, а не как жёсткие универсальные лимиты для любого окружения. Практически это означает, что при планировании нужно закладывать запас по CPU, памяти, storage и параллелизму запросов заранее, ориентируясь на скорость прироста `ALERTS` и фактическую динамику `vmalert`/`vmselect`/`vmstorage` в вашем стенде. Перед переносом ориентиров в production желательно повторить прогон на своей инфраструктуре и зафиксировать локальные коэффициенты роста, чтобы пороги алертов и шаги масштабирования отражали реальный профиль нагрузки.

- **Запас по циклу eval (периодический пересчёт/оценка всех правил в `vmalert`) снизился, но ещё укладывается в `interval`:** пик `max(vmalert_iteration_duration_seconds)` достигал **~15,1 с** при `interval` **30 s**.
- **Линейные ориентиры по vmalert:** В прогоне рост `ALERTS` давал примерно **~10 m CPU** и **~27,9 MiB RAM** на каждые 1000 `ALERTS`.
- **Линейные ориентиры по vmstorage:** В этом прогоне на каждые 1000 `ALERTS` приходилось примерно **~57,8 MiB RAM** (`container_memory_working_set_bytes`) и **~7,7 m CPU** (`container_cpu_usage_seconds_total`).
- **Линейные ориентиры по operator:** В прогоне на каждые 1000 `ALERTS` приходилось примерно **~2,6 m CPU** (`process_cpu_seconds_total`) и **~3,7 MiB RAM** (`process_resident_memory_bytes`).
- **Control plane выдержал нагрузку:** При росте `count(ALERTS)` с ~500 до ~50 000 RPS API server оставался умеренным (**~12,3-15,2 req/s**), p99 задержки — **37-72 ms**, CPU kube-apiserver — **~81-108m**; в этом прогоне API server не стал главным узким местом относительно data plane.
- **Нагрузка на vmselect:** ориентир — около **~12,6 req/s** на каждую 1000 `ALERTS`; при дальнейшем росте нужны ранний мониторинг таймаутов и масштабирование `vmselect` (при необходимости — `vmstorage`).
- **Косвенный ориентир нагрузки на vmselect:** около **~6,8 req/s** на 1000 `ALERTS` по `vm_http_requests_total`; с ростом показателя ожидаемо увеличивается занятость параллелизма.
- **Поведение VMRule/ConfigMap стабильно:** Operator предсказуемо дробит правила около лимита ~~1 MiB; при росте количества правил число `rulefiles-*` ConfigMap растёт ступенчато. При массовом поэтапном apply новые ConfigMap могут вызывать пересоздание Pod'а vmalert, поэтому для будущих изменений стоит использовать батчи или GitOps с контролируемым темпом.
