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

Скрипт [alerts/generate_alerts.py](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py) генерирует YAML-файлы `VMRule` в директорию `alerts/vmrules/`. По умолчанию создаётся **500** файлов; каждый `VMRule` содержит **4–6 групп** (с `interval` 30s/1m/2m) и **100 алертов** суммарно.

Исходный код файла [alerts/generate_alerts.py](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py).

```bash
cd alerts
./generate_alerts.py
```

Правила «псевдо-реалистичные»: разные шаблоны (k8s/node/http/db/…), `expr` построены на `vector(...)`, `severity` задаётся шаблоном (в основном `warning`/`critical`), `for` — от `0s` до `1h`. Объём можно изменить в `main()` через `num_vmrules` и `alerts_per_vmrule`. Скрипт перезаписывает только файлы `vmrule-00001.yaml` … `vmrule-NNNNN.yaml` в пределах `num_vmrules`;

### Применение VMRule в Kubernetes

Скрипт [alerts/apply-yaml.sh](alerts/apply-yaml.sh) применяет все **500** YAML-файлов из `alerts/vmrules/` по одному с фиксированной паузой между вызовами `kubectl apply`.

**Темп:** пауза между apply задаётся константой `APPLY_TIMEOUT` (по умолчанию **30 с**). Общее расчётное время при 500 файлах и 30 с паузы: 30 × 499 = 14 970 с ≈ **~4 ч 9 мин**.

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

- **`-remoteWrite.url`** — при каждой оценке vmalert записывает ряды `ALERTS` и `ALERTS_FOR_STATE` в VMCluster (через vminsert);
- **`-remoteRead.url`** — при **старте** процесса vmalert восстанавливает состояние, запрашивая ряды `ALERTS_FOR_STATE` (через vmselect).


**`ALERTS_FOR_STATE`** содержит полную информацию о состоянии каждого алерта (`ActiveAt`, `for` duration и т.д.), необходимую для восстановления после рестарта. При запуске vmalert однократно читает этот ряд для восстановления.

## Capacity Planning

### Скрипт `scripts/fetch_capacity_snapshots.py`

**Что делает:** Скрипт автоматически собирает "снимки" (замеры) загрузки системы VictoriaMetrics в заранее выбранные моменты времени — примерно при 5000, 10000, ... 50000 активных алертах. Для этого он запрашивает метрики напрямую у vmselect через Prometheus API — такие как загрузка CPU, используемая память подов (в пространстве имён `vmks`), нагрузка на kube-apiserver и количество HTTP-запросов к компонентам vmselect/vmstorage/vminsert.

**Как запустить** (только стандартная библиотека Python 3, зависимости не устанавливаются):

```bash
python3 scripts/fetch_capacity_snapshots.py
```

### Ресурсы подов при росте нагрузки

#### CPU (в среднем на pod)


| ALERTS | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500   | 16m     | 45m       | 37m      | 12m      | 31m     | 5m       |
| ~5000  | 151m    | 118m      | 109m     | 15m      | 54m     | 16m      |
| ~10000 | 251m    | 96m       | 127m     | 19m      | 75m     | 34m      |
| ~15000 | 464m    | 344m      | 236m     | 30m      | 115m    | 62m      |
| ~20000 | 609m    | 156m      | 276m     | 33m      | 145m    | 67m      |
| ~25000 | 875m    | 240m      | 294m     | 38m      | 160m    | 100m     |
| ~30000 | 827m    | 210m      | 293m     | 41m      | 168m    | 95m      |
| ~35000 | 1063m   | 209m      | 333m     | 42m      | 249m    | 127m     |
| ~40000 | 1364m   | 484m      | 337m     | 63m      | 284m    | 138m     |
| ~45000 | 1510m   | 232m      | 349m     | 65m      | 289m    | 160m     |
| ~50000 | 1379m   | 238m      | 335m     | 75m      | 306m    | 169m     |


#### Memory (в среднем на pod)


| ALERTS | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| ~500   | 44Mi    | 227Mi     | 45Mi     | 62Mi     | 83Mi    | 39Mi     |
| ~5000  | 136Mi   | 601Mi     | 156Mi    | 64Mi     | 96Mi    | 70Mi     |
| ~10000 | 108Mi   | 744Mi     | 150Mi    | 91Mi     | 107Mi   | 85Mi     |
| ~15000 | 314Mi   | 1390Mi    | 395Mi    | 144Mi    | 135Mi   | 116Mi    |
| ~20000 | 456Mi   | 2057Mi    | 390Mi    | 128Mi    | 168Mi   | 148Mi    |
| ~25000 | 728Mi   | 2334Mi    | 559Mi    | 135Mi    | 204Mi   | 173Mi    |
| ~30000 | 167Mi   | 2093Mi    | 370Mi    | 158Mi    | 209Mi   | 174Mi    |
| ~35000 | 603Mi   | 2053Mi    | 553Mi    | 148Mi    | 246Mi   | 212Mi    |
| ~40000 | 1012Mi  | 2884Mi    | 237Mi    | 189Mi    | 318Mi   | 264Mi    |
| ~45000 | 1080Mi  | 2538Mi    | 239Mi    | 152Mi    | 294Mi   | 177Mi    |
| ~50000 | 1202Mi  | 2635Mi    | 262Mi    | 188Mi    | 312Mi   | 306Mi    |


### RPS и операционные метрики

| ALERTS | API Server RPS | API Server p99 lat | API Server CPU | vmselect HTTP RPS | vmstorage HTTP RPS | vminsert HTTP RPS |
| ------ | -------------- | ------------------ | -------------- | ----------------- | ------------------ | ----------------- |
| ~500   | 11.4           | 34 ms              | 70m            | 23.2              | 0.1                | 8.0               |
| ~5000  | 12.3           | 48 ms              | 84m            | 206               | 0.1                | 10.5              |
| ~10000 | 12.3           | 47 ms              | 83m            | 339               | 0.1                | 10.7              |
| ~15000 | 12.6           | 44 ms              | 86m            | 630               | 0.1                | 11.1              |
| ~20000 | 12.8           | 76 ms              | 88m            | 796               | 0.1                | 11.3              |
| ~25000 | 13.1           | 83 ms              | 92m            | 1027              | 0.1                | 11.6              |
| ~30000 | 13.7           | 78 ms              | 97m            | 1056              | 0.1                | 11.8              |
| ~35000 | 12.8           | 96 ms              | 94m            | 1143              | 0.1                | 11.8              |
| ~40000 | 14.3           | 95 ms              | 104m           | 1645              | 0.1                | 12.3              |
| ~45000 | 13.9           | 153 ms             | 108m           | 1871              | 0.1                | 12.5              |
| ~50000 | 14.3           | 119 ms             | 110m           | 2021              | 0.1                | 12.9              |


## Метрики, выросшие при нагрузке (VictoriaMetrics stack)

### 1. VMAlert (vmalert)

- **vmalert_iteration_duration_seconds** — растёт сильнее всего: при десятках тысяч активных алертов максимум по группам может занимать **существенную долю** `interval` (см. также выводы в [Заключение](#заключение-и-выводы)).
- **vmalert_iteration_missed_total** — в норме 0; при перегрузке или узком vmselect — рост от 0.
- **vmalert_execution_errors_total** — при сбоях vmselect/vminsert рост от 0 до **единиц–десятков** в час.
- **vmalert_alerts_firing** / **vmalert_alerts_pending** — растут **с числом срабатывающих правил**; при `count(ALERTS)` ~50k суммы по репликам — **десятки тысяч** (учитывайте дублирование метрик между репликами при `sum()` без `max by`/`avg by`).
- **vmalert_remotewrite_requests_total** — растёт с числом групп и итераций (запись `ALERTS` и `ALERTS_FOR_STATE`).
- **vmalert_remoteread_requests_total** — всплеск при **старте** процесса (restore из `ALERTS_FOR_STATE`), плюс нагрузка от eval.
- **container_cpu_usage_seconds_total** (vmalert) — по срезам таблицы: **~16m** → **~1379m** на реплику (**~500** vs **~50 000** `ALERTS`), т.е. порядка **в десятки–сотню раз** между крайними точками; запас до лимита 4 CPU зависит от шардирования и HPA.
- **container_memory_working_set_bytes** (vmalert) — **~44 Mi** → **~1202 Mi** на реплику между теми же срезами (**~25–30×**).


### 2. VMSelect

- **vm_concurrent_select_current** — растёт с параллельными запросами vmalert (eval, запросы правил, remote read при старте).
- **vm_concurrent_select_limit_reached_total** / **vm_concurrent_select_limit_timeout_total** — при упоре в лимиты — рост от 0; в норме timeout близок к нулю.
- **vm_select_request_duration_seconds** — p99 растёт с тяжестью запросов (`ALERTS_FOR_STATE`, большие группы правил).
- **vm_http_requests_total** (job=vmselect) — по таблице [RPS](#rps-и-операционные-метрики): **~23** → **~2 000** req/s (**~90×** между срезами ~500 и ~50 000 `ALERTS`).


### 3. VMStorage

- **vm_rows** / **vm_rows_inserted_total** — рост **пропорционально** объёму вставляемых рядов; от «почти пустого» стенда до полной нагрузки — **на порядки**.
- **vm_storage_blocks** — растёт с объёмом данных на диске.
- **vm_cache_*_requests_total** / **vm_cache_*_misses_total** — объём обращений к кэшу растёт с нагрузкой на select; miss rate может ухудшаться при нехватке RAM.
- **vm_http_requests_total** (job=vmstorage) — следует за запросами vmselect; абсолютные значения ниже, чем у vmselect (см. таблицу RPS: **~0,1** vs **~2k** на стороне select).


### 4. VMInsert

- **vm_http_requests_total** (job=vminsert) — **~8** → **~13** req/s по таблице RPS (умеренный рост: remote write не доминирует над чтением select при таком сценарии).
- **vm_insert_request_duration_seconds** — p99 может вырасти с пиковой записью.
- **vm_insert_requests_total** — растёт с объёмом записи vmalert/vmagent; относительно малого среза — **в разы**.


### 5. VMAgent

- **scrape_series_added** (target=vmalert) — резко растёт: `/metrics` vmalert раздувается с числом правил и состояний.
- **scrape_body_size_bytes** (target=vmalert) — рост **на порядки**; при десятках тысяч алертов следите за `maxScrapeSize` и лимитами памяти агента.
- **scrape_samples_scraped** (job=vmalert) — **пропорционально** числу отдаваемых vmalert сэмплов.


### 6. VictoriaMetrics Operator

- **process_cpu_seconds_total** (job=operator) — по таблице CPU: **~5m** → **~169m** (**~500** vs **~50 000** `ALERTS`), т.е. **десятки раз** при росте числа `VMRule` и reconcile.
- **process_resident_memory_bytes** (job=operator) — **~39 Mi** → **~306 Mi** в тех же срезах (**~8×**).


### 7. Kubernetes / ресурсы подов

- **container_cpu_usage_seconds_total** — для vmselect/vmstorage/vminsert по таблице: типичный рост **в несколько раз** (например vmstorage **~45m** → **~238m**, vmselect **~37m** → **~335m**, vminsert **~12m** → **~75m**).
- **container_memory_working_set_bytes** (vmstorage) — **~227 Mi** → **~2,6 Gi** на реплику (**~500** vs **~50 000** `ALERTS`).
- **container_memory_working_set_bytes** (vmselect) — **~45 Mi** → **~262 Mi**.


### 8. Алерты и объём данных

- **count(ALERTS)** — от среза **~500** до **~50 000** в таблицах (рост **на порядки**).
- **count(ALERTS_FOR_STATE)** — того же порядка, что и `ALERTS`; растёт с числом восстанавливаемых состояний.
- **totalSeries** (через API/tsdb) — растёт с объёмом рядов `ALERTS`/`ALERTS_FOR_STATE` и служебных метрик; в раннем измерении при **~23k** алертов фиксировали **~4,79 млн** рядов — к **~50k** ожидаемо **порядка ~10 млн** (оценка по масштабу; точное значение зависит от меток и ретенции). Имеет смысл сверять с `-search.maxUniqueTimeseries` в [vmks-values.yaml](vmks-values.yaml).


### Как считать прирост

- Для счётчиков: `increase(metric_name[1h])` или сравнение с периодом до нагрузки.
- Для gauge (CPU, память, длительность): сравнение средних/перцентилей «до» и «после» по тому же окну.
- Список имён метрик: `GET /api/v1/label/__name__/values`, затем фильтр по префиксу (`vmalert_*`, `vm_concurrent_select_*` и т.д.).
- Ресурсы подов: `kubectl top pods -n vmks`.


## Заключение и выводы

Проведённое нагрузочное тестирование VictoriaMetrics stack большим количеством VMRule подтвердило заявленные цели и позволило сформулировать практические выводы.

### Достигнутые результаты

- **Отказоустойчивость:** Механизм remoteWrite/remoteRead (`ALERTS`, `ALERTS_FOR_STATE`) работает корректно: после рестарта vmalert восстанавливает состояние алертов из VictoriaMetrics, счётчики `for` не сбрасываются, потери алертов не происходят. `vmalert_execution_errors_total = 0` и `vmalert_iteration_missed_total = 0` подтверждают стабильную работу. Временное падение `sum(ALERTS)` во время рестарта — следствие задержки первой итерации, а не потери данных. **Перезапуски vmalert:** каждое появление нового ConfigMap приводит к пересозданию Pod'а (из-за volume/volumeMount); интервал между рестартами ~13–15 мин, за тест — 15 ReplicaSet'ов (14 пересозданий). Горячая перезагрузка (SIGHUP) — только при обновлении существующих ConfigMap'ов без новых.
- **Потребление ресурсов и масштабируемость:** При ~23 000 алертов (~4,79 млн рядов) vmalert потребляет ~723m CPU и ~676Mi памяти на реплику — в пределах лимитов (4 CPU / 4 Gi). VM Operator — ~52m CPU. `max(vmalert_iteration_duration_seconds)` достигла 3.84 сек — важный индикатор при интервалах 30s. Экстраполяция к ~100 000 алертов указывает на шардирование vmalert и масштабирование VMCluster. **Распределение по ConfigMap'ам:** Operator стабильно дробит правила у лимита ~1 MiB (~505–509 KB на ConfigMap); при ~23 000 алертов — 17 ConfigMap'ов (~8,5 MB суммарно), механизм предсказуем.
- **Нагрузка на API server:** По срезам Capacity Planning рост `count(ALERTS)` с ~500 до ~35 000 сопровождался умеренным ростом RPS API server (порядка **11–14** req/s в среднем по окнам), p99 задержки **34–119 ms** (в верхнем диапазоне — в том числе срез **~50000**), CPU kube-apiserver **~70–110m** — нагрузка на control plane заметна, но не доминирует над узкими местами по vmalert/vmselect/vmstorage в этом прогоне.

### Основные выводы

1. **Отказоустойчивость:** Конфигурация с двумя репликами vmalert, remoteRead/remoteWrite и Alertmanager в кластерном режиме обеспечивает восстановление без потери состояния алертов и без дублирования уведомлений. RTO vmalert — в пределах минуты (детали RTO/RPO — в комментариях к `vmks-values.yaml`). На текущем этапе `vmalert_execution_errors_total = 0` и `vmalert_iteration_missed_total = 0`. При массовом добавлении VMRule учитывать периодические рестарты vmalert (по одному на каждый новый ConfigMap); в production — батчи или GitOps с темпом, чтобы не плодить лишние ConfigMap'ы подряд.
2. **Потребление ресурсов:** При ~23 000 алертов CPU vmalert ~723m — запас до лимита 4 CPU есть. При росте к ~100 000 алертов — лимиты 3–4 CPU и шардирование vmalert с масштабированием VMCluster. vmstorage ~1.6 Gi RAM на реплику — закладывать в планирование нод.
3. **Нагрузка на API server:** Рост числа VMRule и reconcile Operator'а увеличивает RPS и задержки apiserver умеренно относительно роста алертов; имеет смысл отслеживать `apiserver_request_total`, `apiserver_request_duration_seconds` и CPU kube-apiserver наряду с метриками data plane.
4. **Мониторинг:** Раннее обнаружение перегрузки — `vmalert_iteration_duration_seconds`, `vmalert_iteration_missed_total`, `container_cpu_usage_seconds_total` (vmalert), размер и число ConfigMap'ов с правилами.

Итог: приоритеты подтверждены — **отказоустойчивость** (целостность состояния и HA) сохраняется при тысячах правил; **ресурсы** и **нагрузка на API server** растут предсказуемо, основные ограничения снимаются шардированием, масштабированием VMCluster и контролируемым темпом применения правил.