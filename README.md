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

- `**-remoteWrite.url**` — при каждой оценке vmalert записывает ряды `ALERTS` и `ALERTS_FOR_STATE` в VMCluster (через vminsert);
- `**-remoteRead.url**` — при **старте** процесса vmalert восстанавливает состояние, запрашивая ряды `ALERTS_FOR_STATE` (через vmselect).

`**ALERTS_FOR_STATE`** содержит полную информацию о состоянии каждого алерта (`ActiveAt`, `for` duration и т.д.), необходимую для восстановления после рестарта. При запуске vmalert однократно читает этот ряд для восстановления.

## Capacity Planning

### Скрипт `scripts/fetch_capacity_snapshots.py`

**Что делает:** Скрипт автоматически собирает "снимки" (замеры) загрузки системы VictoriaMetrics в заранее выбранные моменты времени — примерно при 500, 5000, 10000, ... 50000 активных алертах. Для этого он запрашивает метрики напрямую у vmselect через Prometheus API — такие как загрузка CPU, используемая память подов (в пространстве имён `vmks`), нагрузка на kube-apiserver и количество HTTP-запросов к компонентам vmselect/vmstorage/vminsert.

**Как запустить** (только стандартная библиотека Python 3, зависимости не устанавливаются):

```bash
python3 scripts/fetch_capacity_snapshots.py
```

### Ресурсы подов при росте нагрузки

#### CPU (в среднем на pod)


| ALERTS | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500   | 21m     | 54m       | 40m      | 11m      | 34m     | 8m       |
| ~5000  | 60m     | 99m       | 69m      | 14m      | 46m     | 15m      |
| ~10000 | 86m     | 105m      | 92m      | 19m      | 63m     | 30m      |
| ~15000 | 96m     | 98m       | 101m     | 23m      | 78m     | 37m      |
| ~20000 | 257m    | 206m      | 104m     | 27m      | 103m    | 55m      |
| ~25000 | 167m    | 131m      | 134m     | 33m      | 128m    | 53m      |
| ~30000 | 180m    | 371m      | 142m     | 35m      | 166m    | 80m      |
| ~35000 | 206m    | 534m      | 164m     | 40m      | 162m    | 80m      |
| ~40000 | 269m    | 274m      | 254m     | 45m      | 196m    | 77m      |
| ~45000 | 377m    | 336m      | 318m     | 45m      | 191m    | 96m      |
| ~50000 | 181m    | 276m      | 190m     | 53m      | 277m    | 60m      |


#### Memory (в среднем на pod)


| ALERTS | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500   | 56Mi    | 236Mi     | 46Mi     | 54Mi     | 94Mi    | 46Mi     |
| ~5000  | 122Mi   | 566Mi     | 80Mi     | 82Mi     | 92Mi    | 56Mi     |
| ~10000 | 251Mi   | 771Mi     | 108Mi    | 97Mi     | 128Mi   | 94Mi     |
| ~15000 | 298Mi   | 958Mi     | 190Mi    | 134Mi    | 118Mi   | 103Mi    |
| ~20000 | 332Mi   | 1290Mi    | 292Mi    | 208Mi    | 109Mi   | 127Mi    |
| ~25000 | 632Mi   | 1734Mi    | 694Mi    | 151Mi    | 197Mi   | 116Mi    |
| ~30000 | 528Mi   | 1981Mi    | 254Mi    | 194Mi    | 322Mi   | 167Mi    |
| ~35000 | 584Mi   | 2320Mi    | 209Mi    | 231Mi    | 324Mi   | 189Mi    |
| ~40000 | 966Mi   | 2205Mi    | 1012Mi   | 199Mi    | 237Mi   | 242Mi    |
| ~45000 | 809Mi   | 2678Mi    | 663Mi    | 182Mi    | 270Mi   | 243Mi    |
| ~50000 | 1462Mi  | 2164Mi    | 258Mi    | 224Mi    | 367Mi   | 338Mi    |


### RPS и операционные метрики


| ALERTS | API Server RPS | API Server p99 lat | API Server CPU | vmselect HTTP RPS | vmstorage HTTP RPS | vminsert HTTP RPS |
| ------ | -------------- | ------------------ | -------------- | ----------------- | ------------------ | ----------------- |
| ~500   | 12.1           | 47 ms              | 76m            | 25.5              | 0.1                | 9.4               |
| ~5000  | 12.8           | 45 ms              | 81m            | 56.1              | 0.1                | 9.7               |
| ~10000 | 12.4           | 42 ms              | 79m            | 117               | 0.1                | 10.2              |
| ~15000 | 11.9           | 49 ms              | 80m            | 128.1             | 0.1                | 10.4              |
| ~20000 | 12.5           | 77 ms              | 88m            | 104               | 0.1                | 11.1              |
| ~25000 | 12.8           | 46 ms              | 90m            | 106.3             | 0.1                | 11.2              |
| ~30000 | 13.5           | 92 ms              | 97m            | 153.7             | 0.1                | 11.5              |
| ~35000 | 13.2           | 84 ms              | 98m            | 159.4             | 0.1                | 11.8              |
| ~40000 | 12.7           | 50 ms              | 95m            | 362.2             | 0.1                | 12               |
| ~45000 | 13.9           | 92 ms              | 101m           | 345.5             | 0.1                | 12.2              |
| ~50000 | 12.7           | 48 ms              | 89m            | 198.3             | 0.1                | 13.1              |


#### Метрики компонентов VictoriaMetrics stack, выросшие при нагрузке

Числовые значения по столбцам — по ходу нагрузочного теста; строки совпадают с уровнями в таблицах CPU, Memory и RPS выше.


| ALERTS | vmalert_iteration_duration (max) | vm_concurrent_select_current | vmagent scrape_samples (vmalert) |
| ------ | -------------------------------- | ---------------------------- | -------------------------------- |
| ~500   | 0.98s                            | 1                            | 5114                             |
| ~5000  | 0.82s                            | 0                            | 22679                            |
| ~10000 | 0.99s                            | 0                            | 46560                            |
| ~15000 | 1.13s                            | 0                            | 61383                            |
| ~20000 | 1.62s                            | 0                            | 76174                            |
| ~25000 | 1.65s                            | 0                            | 118624                           |
| ~30000 | 2.56s                            | 2                            | 117805                           |
| ~35000 | 4.85s                            | 0                            | 137285                           |
| ~40000 | 4.76s                            | 0                            | 181470                           |
| ~45000 | 6.37s                            | 0                            | 176999                           |
| ~50000 | 3.44s                            | 1                            | 269068                           |


#### Описание метрик компонентов VictoriaMetrics stack, выросшие при нагрузке

Ниже — только те показатели, которые в этом сценарии **реально растут** вместе с числом активных `ALERTS` и на которые **имеет смысл смотреть в первую очередь** при планировании ёмкости и отладке. Метрики, которые в успешном прогоне остаются нулевыми или почти не меняются (ошибки eval, RPS к vmstorage), сюда не включены.

### vmalert


| Метрика                                            | Чем грозит рост метрики и что делать                                                                                                                                                                                                                                                                                        | Порядок роста (прогон)         |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| `vmalert_iteration_duration_seconds`               | **Что это:** время одного цикла пересчёта rule-group в `vmalert` (оценка всех правил группы). **Риск:** если это время близко к `interval` группы или больше него, `vmalert` не успевает запускать eval вовремя: алерты срабатывают с задержкой, а короткие всплески могут быть пропущены; это также косвенно влияет на `for` (firing/resolved наступают позже). **В этом прогоне:** `interval` группы = `30s`, при пике `vmalert_iteration_duration_seconds` ~`6.4s` запас составлял около `4.7x`. **Где смотреть `interval`:** в YAML группы правил (`groups[].interval`), а если не задан — в глобальном `evaluationInterval` у `vmalert` (helm values/аргументы запуска). **Что делать:** увеличить `interval`, вынести тяжёлые правила в отдельные группы/шарды, ускорить backend (vmselect/VMCluster), уменьшить сложность запросов. | До **~6,4 с**                 |


### vmselect и запросы к TSDB


| Метрика                                    | Чем грозит рост метрики и что делать                                                                                                                                                                                                                                                 | Порядок роста (прогон)                                                                                        |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| `vm_http_requests_total{job="vmselect"}`   | **Риск:** vmselect перегружается по CPU/диску, ответы становятся медленнее, vmalert и Grafana начинают получать таймауты. **Что делать:** масштабировать vmselect (и при необходимости vmstorage), включить шардирование vmalert, уменьшать частоту и "тяжесть" запросов в правилах. | **~6,8** req/s на 1000 `ALERTS`                                                                                |
| `vm_concurrent_select_current`             | **Риск:** заняты все слоты параллельных запросов, новые запросы встают в очередь и ждут. **Что делать:** поднять `search.maxConcurrentRequests`, проверить достаточность CPU/RAM у vmselect, разгрузить запросы (реже выполнять тяжёлые rule-group).                                 | Растёт с RPS; при перегрузке смотреть также `vm_concurrent_select_limit_reached_total` и таблицу лимитов ниже |
| `vm_concurrent_select_limit_reached_total` | **Риск:** часть запросов вообще не запускается из-за лимита параллельности, появляются ошибки/потери данных в окне оценки. **Что делать:** увеличить `search.maxConcurrentRequests` и/или `search.maxQueueDuration`, после этого проверить, не упёрлись ли в CPU и latency.          | `increase(...[1m])` в скриптах мониторинга                                                                    |
| `vm_select_request_duration_seconds`       | **Риск:** чем выше p95/p99, тем медленнее eval и восстановление состояния алертов после рестарта. **Что делать:** оптимизировать PromQL/LogsQL в правилах, добавить ресурсы/реплики vmselect, отслеживать p95/p99 и таймауты как SLO.                                                | Смотреть p95/p99 на графике                                                                                   |


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
| `container_memory_working_set_bytes` (pod vmstorage)      | **Риск:** при росте памяти остаётся меньше запаса на ноде, возрастает шанс OOM и деградации I/O из-за давления на память. **Что делать:** увеличить memory requests/limits и ёмкость нод, масштабировать vmstorage, настроить алерты по памяти до критических значений.         | **~44,4 MiB** на 1000 `ALERTS`              |
| `container_cpu_usage_seconds_total` (pod vmstorage)       | **Риск:** при пиках CPU может расти latency чтения/записи и общее время обработки запросов. **Что делать:** поднять CPU лимиты/реплики, проверить фоновые задачи (compaction/merge), распределять нагрузку равномерно между pod.                                                | **~5,3 m** CPU на 1000 `ALERTS`           |
| `vm_rows` / `vm_rows_inserted_total`, `vm_storage_blocks` | **Риск:** быстрее заканчивается диск, растёт нагрузка на I/O, резервные копии и обслуживание занимают больше времени. **Что делать:** планировать storage заранее (capacity + IOPS), включить ретеншн/политику очистки, контролировать скорость прироста данных и время бэкапа. | Рост на порядки по мере заполнения стенда |


### vmagent (скрейп vmalert)


| Метрика                                                              | Чем грозит рост метрики и что делать                                                                                                                                                                                                                                      | Порядок роста (прогон)                                           |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `scrape_body_size_bytes` / `scrape_samples_scraped` (target vmalert) | **Риск:** vmagent начинает дольше скрейпить таргет, возможны таймауты, обрезка ответа и рост памяти самого агента. **Что делать:** увеличить `maxScrapeSize` и ресурсы vmagent, уменьшить объём экспортируемых метрик/лейблов, при необходимости снизить частоту скрейпа. | **На порядки**; проверять лимиты `maxScrapeSize` и память агента |


### VictoriaMetrics Operator


| Метрика                                         | Чем грозит рост метрики и что делать                                                                                                                                                                                                                                    | Порядок роста (прогон)          |
| ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| `process_cpu_seconds_total` / CPU пода operator | **Риск:** reconcile начинает отставать, изменения VMRule применяются медленнее, дольше идёт схождение состояния. **Что делать:** увеличить CPU лимит operator, уменьшить "бурст" изменений (батчи/GitOps-волны), следить за длительностью reconcile и очередью событий. | **~1,6 m** CPU на 1000 `ALERTS` |
| `process_resident_memory_bytes` / память пода   | **Риск:** при очень больших наборах правил возможен OOM и рестарт operator, что задержит обновления конфигурации. **Что делать:** добавить memory запас, контролировать количество/размер `VMRule`, периодически проверять число и размер `rulefiles` ConfigMap.        | **~3,5 MiB** на 1000 `ALERTS`   |


## Заключение и выводы
### Ключевые выводы

- **Отказоустойчивость подтверждена:** Конфигурация с двумя репликами vmalert, remoteRead/remoteWrite (`ALERTS`, `ALERTS_FOR_STATE`) и Alertmanager в кластерном режиме корректно восстанавливает состояние алертов после рестарта без потерь и без дублирования уведомлений; временное падение `sum(ALERTS)` связано с задержкой первой итерации, а не с потерей данных. RTO vmalert — в пределах минуты (детали — в комментариях к `vmks-values.yaml`).
- **Ресурсы и масштабируемость предсказуемы:** На финальном срезе (~50k `ALERTS`) vmalert потреблял около **~0,18 CPU** и **~1,43 Gi RAM** на реплику при лимитах 4 CPU / 4 Gi; пик `max(vmalert_iteration_duration_seconds)` достигал **~6,4 с** при `interval` **30 s**. Для дальнейшего роста нужно заранее планировать шардирование vmalert и масштабирование VMCluster; vmstorage на срезах — до **~2,6 Gi RAM** на реплику.
- **Линейные ориентиры по vmalert:** В прогоне рост `ALERTS` давал примерно **~5,2 m CPU** и **~14,1 MiB RAM** на каждые 1000 `ALERTS`; эти коэффициенты можно использовать как быстрый capacity baseline при планировании ресурсов.
- **Control plane выдержал нагрузку:** При росте `count(ALERTS)` с ~500 до ~50 000 RPS API server оставался умеренным (**~11,9–13,9 req/s**), p99 задержки — **42–92 ms**, CPU kube-apiserver — **~76–101m**; в этом прогоне API server не стал главным узким местом относительно data plane.
- **Поведение VMRule/ConfigMap стабильно:** Operator предсказуемо дробит правила около лимита ~~1 MiB; при ~50k алертов зафиксировано 39 ConfigMap'ов (~~19,2 MB суммарно). При массовом поэтапном apply каждый новый ConfigMap вызывал пересоздание Pod'а vmalert (интервал ~9–11 мин, 11 ReplicaSet'ов/10 пересозданий), поэтому для будущих изменений стоит использовать батчи или GitOps с контролируемым темпом.
- **Фокус мониторинга на ранние сигналы перегрузки:** Ключевые метрики — `vmalert_iteration_duration_seconds`, `vmalert_iteration_missed_total`, `container_cpu_usage_seconds_total` (vmalert), а также число и размер ConfigMap'ов с правилами и метрики apiserver (`apiserver_request_total`, `apiserver_request_duration_seconds`, CPU kube-apiserver).
