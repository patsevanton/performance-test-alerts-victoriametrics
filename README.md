# Нагрузочное тестирование VictoriaMetrics большим количеством алертов

## Цели

1. **Отказоустойчивость и непрерывность:** как при десятках тысяч активных `VMRule` сохраняются оценка правил, состояние алертов (`for`, pending/firing), восстановление после рестартов `vmalert`, и какие риски для этой цепочки возникают у оператора, `vmalert` и хранилища.
2. **Потребление ресурсов:** где узкие места по CPU и памяти (`vmalert`, VMCluster, Operator) и как растут затраты при росте числа алертов.
3. **Нагрузка на API server:** как растут RPS, задержки и CPU `kube-apiserver` при массовом применении `VMRule` и reconcile Operator'а.

Дополнительно — практическая картина стека VictoriaMetrics в Kubernetes при такой нагрузке и набор метрик для контроля.

## Архитектура и стенд

### Infrastructure

**Кластер:** 3 ноды Kubernetes v1.32.1 на Yandex Cloud (Ubuntu 22.04.5 LTS, containerd 1.7.27).

## Установка

### victoria-metrics-k8s-stack

```bash
helm repo add vm https://victoriametrics.github.io/helm-charts/
helm repo update

helm upgrade --install vmks vm/victoria-metrics-k8s-stack \
  --namespace vmks --create-namespace \
  --version 0.72.5 \
  --wait --values vmks-values.yaml
```

Исходный код файла [vmks-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml). Включает Grafana с ingress на [grafana.apatsev.org.ru](http://grafana.apatsev.org.ru).

Получение пароля Grafana:

```bash
kubectl get secret vmks-grafana -n vmks -o jsonpath='{.data.admin-password}' | base64 --decode; echo
```

### VictoriaLogs

[VictoriaLogs](https://docs.victoriametrics.com/victorialogs/) — хранилище логов с поддержкой LogsQL.

**Требование:** VictoriaMetrics K8s Stack установлен первым (CRD VMServiceScrape).

**1. VictoriaLogs cluster (vlselect, vlinsert, vlstorage):**

```bash
helm repo add vm https://victoriametrics.github.io/helm-charts/
helm repo update

helm upgrade --install victoria-logs-cluster vm/victoria-logs-cluster \
  --namespace victoria-logs-cluster \
  --create-namespace \
  --version 0.0.31 \
  --wait \
  --timeout 15m \
  -f victoria-logs-cluster-values.yaml
```

Исходный код файла [victoria-logs-cluster-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/victoria-logs-cluster-values.yaml).

Проверка: `kubectl get pods -n victoria-logs-cluster`. Ingress для vlselect: `victorialogs.apatsev.org.ru` (из values).

**2. Victoria-logs-collector (сбор логов с подов кластера):**

```bash
helm upgrade --install victoria-logs-collector vm/victoria-logs-collector \
  --namespace victoria-logs-collector \
  --create-namespace \
  --version 0.2.13 \
  --wait \
  --timeout 15m \
  -f victoria-logs-collector-values.yaml
```

Исходный код файла [victoria-logs-collector-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/victoria-logs-collector-values.yaml).

### Генерация нагрузочных VMRule

Скрипт `alerts/generate_alerts.py` генерирует YAML-файлы `VMRule` в директорию `alerts/vmrules/`. По умолчанию создаётся 500 файлов; каждый `VMRule` содержит 4–6 групп (с `interval` 5m/10m) и 100 алертов суммарно.

Исходный код файла [alerts/generate_alerts.py](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py).

```bash
cd alerts
./generate_alerts.py
```

Правила «псевдо-реалистичные»: разные шаблоны (k8s/node/http/db/…), `expr` построены на `vector(...)`, `severity` задаётся шаблоном (в основном `warning`/`critical`), `for` — от `0s` до `1h`. Объём можно изменить в `main()` через `num_vmrules` и `alerts_per_vmrule`. Скрипт перезаписывает только файлы `vmrule-00001.yaml` … `vmrule-NNNNN.yaml` в пределах `num_vmrules`;

### Применение VMRule в Kubernetes

Скрипт [alerts/apply-yaml.sh](alerts/apply-yaml.sh) применяет все **500** YAML-файлов из `alerts/vmrules/` по одному с фиксированной паузой между вызовами `kubectl apply`.

**Темп:** пауза между apply задаётся константой `APPLY_TIMEOUT` (по умолчанию **45 с**). Общее расчётное время при 500 файлах и 45 с паузы: 45 × 499 = 22 455 с ≈ **~6 ч 14 мин**. При старте скрипт выводит найденное число файлов, таймаут и эту оценку.

**Запуск (из каталога `alerts`):**

```bash
cd alerts
./apply-yaml.sh
```

Исходный код: [alerts/apply-yaml.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/apply-yaml.sh).

**Мониторинг ошибок:** в отдельном терминале (из `alerts`) запустите `./monitor-batch.sh`. Проверяются логи VictoriaLogs (широкий фильтр ошибок), OOM vmalert, счётчики ошибок и 5xx по компонентам VictoriaMetrics.

Исходный код: [alerts/monitor-batch.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/monitor-batch.sh).

После батча можно анализировать графики нагрузки на vmalert, vmselect, operator.

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

Числовые значения по столбцам — по ходу нагрузочного теста; строки совпадают с уровнями в таблицах CPU, Memory и RPS выше.


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


#### Описание метрик компонентов VictoriaMetrics stack, выросшие при нагрузке

Ниже — только те показатели, которые в этом сценарии **реально растут** вместе с числом активных `ALERTS` и на которые **имеет смысл смотреть в первую очередь** при планировании ёмкости и отладке. Метрики, которые в успешном прогоне остаются нулевыми или почти не меняются (ошибки eval, RPS к vmstorage), сюда не включены.

### vmselect и запросы к TSDB


| Метрика                                    | Чем грозит рост метрики и что делать                                                                                                                                                                                                                                                 | Порядок роста (прогон)                                                                                        |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| `vm_concurrent_select_current`             | **Риск:** заняты все слоты параллельных запросов, новые запросы встают в очередь и ждут. **Что делать:** поднять `search.maxConcurrentRequests`, проверить достаточность CPU/RAM у vmselect, разгрузить запросы (реже выполнять тяжёлые rule-group).                                 | Растёт с RPS; при перегрузке смотреть также `vm_concurrent_select_limit_reached_total` и таблицу лимитов ниже |
| `vm_concurrent_select_limit_reached_total` | **Риск:** часть запросов вообще не запускается из-за лимита параллельности, появляются ошибки/потери данных в окне оценки. **Что делать:** увеличить `search.maxConcurrentRequests` и/или `search.maxQueueDuration`, после этого проверить, не упёрлись ли в CPU и latency.          | `increase(...[1m])` в скриптах мониторинга                                                                    |
| `vmselect_request_duration_seconds`        | **Риск:** чем выше p95/p99, тем медленнее eval и восстановление состояния алертов после рестарта. **Что делать:** оптимизировать PromQL/LogsQL в правилах, добавить ресурсы/реплики vmselect, отслеживать p95/p99 и таймауты как SLO.                                                | Смотреть p95/p99 на графике                                                                                   |

Практические запросы для `vmselect_request_duration_seconds`:

```promql
histogram_quantile(0.95, sum by (vmrange) (rate(vmselect_request_duration_seconds_bucket[5m])))
```

```promql
sum(rate(vmselect_request_duration_seconds_count[5m]))
```

На что обращать внимание:

- p95 должен быть стабильно ниже `search.maxQueueDuration` и далеко от `search.maxQueryDuration`; опасен не единичный пик, а устойчивый рост.
- Смотрите динамику p95 вместе с RPS (`..._count`): если RPS почти не растёт, а p95 растёт, значит ухудшается «стоимость» запросов (тяжёлые выражения, нехватка CPU/параллелизма).
- Если p95 растёт одновременно с `vm_concurrent_select_current` и увеличением `vm_concurrent_select_limit_reached_total`, это признак насыщения vmselect по параллелизму.
- Для алертов полезно проверять не только абсолютную границу, но и длительность деградации (например, p95 выше базового уровня более 10-15 минут подряд).


Очередь и таймауты поиска (ориентиры из конфигурации стенда и дефолтов VictoriaMetrics; подробности — `vmks-values.yaml`):


| Параметр                                    | Значение                       | Комментарий                                                                                                                                         |
| ------------------------------------------- | ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `search.maxConcurrentRequests` (дефолт)     | **2**                          | При параллельной нагрузке — **429** (vmselect) / **503** (vmstorage) и сообщение **«couldn't start executing in 10s»** (таймаут ожидания в очереди) |
| `search.maxConcurrentRequests` (стенд)      | **16**                         | Поднято вместе с очередью и лимитом серий, чтобы снизить отказы при eval vmalert                                                                    |
| `search.maxQueueDuration` (стенд)           | **60s**                        | Максимальное ожидание в очереди перед стартом выполнения запроса                                                                                    |
| `search.maxQueryDuration` (vmselect, стенд) | **300s**                       | Согласовано с `queryTimeout` datasource Grafana (**300s**), иначе обрезка раньше клиента                                                            |
| Косвенный ориентир нагрузки на vmselect     | **~6,8** req/s на 1000 `ALERTS` | Строка `vm_http_requests_total` выше; от неё ожидаемо растёт и занятость параллелизма                                                               |


### vmstorage (данные и память)


| Метрика                                                   | Чем грозит рост метрики и что делать                                                                                                                                                                                                                                            | Порядок роста (прогон)                    |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `vm_rows` / `vm_rows_inserted_total`, `vm_storage_blocks` | **Риск:** быстрее заканчивается диск, растёт нагрузка на I/O, резервные копии и обслуживание занимают больше времени. **Что делать:** планировать storage заранее (capacity + IOPS), включить ретеншн/политику очистки, контролировать скорость прироста данных и время бэкапа. | Рост на порядки по мере заполнения стенда |


### vmagent (скрейп vmalert)


| Метрика                                                              | Чем грозит рост метрики и что делать                                                                                                                                                                                                                                      | Порядок роста (прогон)                                           |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `scrape_body_size_bytes` / `scrape_samples_scraped` (target vmalert) | **Риск:** vmagent начинает дольше скрейпить таргет, возможны таймауты, обрезка ответа и рост памяти самого агента. **Что делать:** увеличить `maxScrapeSize` и ресурсы vmagent, уменьшить объём экспортируемых метрик/лейблов, при необходимости снизить частоту скрейпа. | **На порядки**; проверять лимиты `maxScrapeSize` и память агента |


## Выводы

**Отказоустойчивость подтверждена:** Конфигурация с двумя репликами vmalert, remoteRead/remoteWrite (`ALERTS`, `ALERTS_FOR_STATE`) и Alertmanager в кластерном режиме корректно восстанавливает состояние алертов после рестарта без потерь и без дублирования уведомлений.

### Ключевые результаты

Эти значения стоит использовать как рабочий baseline для capacity planning и раннего масштабирования, а не как жёсткие универсальные лимиты для любого окружения. Практически это означает, что при планировании нужно закладывать запас по CPU, памяти, storage и параллелизму запросов заранее, ориентируясь на скорость прироста `ALERTS` и фактическую динамику `vmalert`/`vmselect`/`vmstorage` в вашем стенде. Перед переносом ориентиров в production желательно повторить прогон на своей инфраструктуре и зафиксировать локальные коэффициенты роста, чтобы пороги алертов и шаги масштабирования отражали реальный профиль нагрузки.

- **Запас по циклу eval (периодический пересчёт/оценка всех правил в `vmalert`) снизился, но ещё укладывается в `interval`:** пик `max(vmalert_iteration_duration_seconds)` достигал **~15,1 с** при `interval` **30 s**.
- **Линейные ориентиры по vmalert:** В прогоне рост `ALERTS` давал примерно **~10 m CPU** и **~27,9 MiB RAM** на каждые 1000 `ALERTS`.
- **Линейные ориентиры по vmstorage:** В этом прогоне на каждые 1000 `ALERTS` приходилось примерно **~57,8 MiB RAM** (`container_memory_working_set_bytes`) и **~7,7 m CPU** (`container_cpu_usage_seconds_total`).
- **Линейные ориентиры по operator:** В прогоне на каждые 1000 `ALERTS` приходилось примерно **~2,6 m CPU** (`process_cpu_seconds_total`) и **~3,7 MiB RAM** (`process_resident_memory_bytes`).
- **Control plane выдержал нагрузку:** При росте `count(ALERTS)` с ~500 до ~50 000 RPS API server оставался умеренным (**~12,3-15,2 req/s**), p99 задержки — **37-72 ms**, CPU kube-apiserver — **~81-108m**; в этом прогоне API server не стал главным узким местом относительно data plane.
- **Нагрузка на vmselect:** ориентир — около **~12,6 req/s** на каждую 1000 `ALERTS`; при дальнейшем росте нужны ранний мониторинг таймаутов и масштабирование `vmselect` (при необходимости — `vmstorage`).
- **Поведение VMRule/ConfigMap стабильно:** Operator предсказуемо дробит правила около лимита ~~1 MiB; при росте количества правил число `rulefiles-*` ConfigMap растёт ступенчато. При массовом поэтапном apply новые ConfigMap могут вызывать пересоздание Pod'а vmalert, поэтому для будущих изменений стоит использовать батчи или GitOps с контролируемым темпом.
- **Фокус мониторинга на ранние сигналы перегрузки:** Ключевые метрики — `vmalert_iteration_duration_seconds`, `vmalert_iteration_missed_total`, `container_cpu_usage_seconds_total` (vmalert), а также число и размер ConfigMap'ов с правилами и метрики apiserver (`apiserver_request_total`, `apiserver_request_duration_seconds`, CPU kube-apiserver).
