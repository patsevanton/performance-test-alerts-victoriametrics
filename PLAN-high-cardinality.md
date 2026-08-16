# План повышения сложности и кардинальности метрик

## Цель

Повысить кардинальность существующих метрик (перевести в Vec с лейблами) и добавить немного новых высококардинальных метрик, а часть существующих 40 `ExtraAlert0xx` сделать сложными (subqueries/joins/`label_*`/`quantile_over_time`/`predict_linear`/`topk`), чтобы получить ориентиры capacity planning для тяжёлого PromQL.

Принцип: текущий профиль эволюционирует на месте — отдельного «второго режима» нет, тяжёлая часть всегда активна и параметризуется через `app.cardinality.*` и `alerts.extra.count` в `values.yaml`.

## Этап 0. Фиксация рамок

Зафиксировать в `README.md` (раздел «Важная оговорка») параметры:

- кардинальность на одно приложение (целевое число рядов на метрику);
- набор лейблов;
- классы тяжёлого PromQL, встраиваемые в существующие `ExtraAlert0xx` (subqueries / joins / `label_*` / `quantile_over_time` / `predict_linear` / `topk`);
- число алертов на приложение не меняется (50 = 10 базовых + 40 extra);
- расширять ли `fetch_capacity_snapshots.py` метриками `vm_search_*`, `vm_rows_*`, cache miss.

## Этап 1. Высококардинальные метрики в `app/main.go`

Параметризовать через переменные окружения (`APP_CARDINALITY`, `APP_TENANTS`, `APP_ROUTES`, `APP_HIST_BUCKETS`).

### 1.1. Лейбл-пространства

- `method` — `GET`, `POST`, `PUT`, `DELETE`, `PATCH` (5);
- `endpoint` — `/work`, `/healthz`, `/metrics`, `/api/v1`, `/api/v2` (5);
- `status_code` — `200`, `429`, `500`, `503`, `504` (5);
- `route` — `route-0` … `route-N` (по `APP_ROUTES`, по умолчанию 10);
- `tenant_id` — `tenant-0` … `tenant-M` (по `APP_TENANTS`, по умолчанию 50);
- `region` — `ru-central1-b/-d/-e` (3, берётся из env `APP_REGION`);
- `version` — из env, например `1.6.1`.

### 1.2. Vec-метрики

Перевести существующие метрики в `prometheus.NewHistogramVec` / `NewCounterVec` / `NewGaugeVec` с лейблами:

- `app_requests_total{method,endpoint,status_code,route,tenant_id,region,version}` (CounterVec);
- `app_errors_total{method,endpoint,status_code,route,tenant_id,region,version}` (CounterVec);
- `app_request_latency_seconds` (HistogramVec, кастомные `Buckets` длиной `APP_HIST_BUCKETS`, по умолчанию 50);
- `app_goroutines` — оставить gauge без лейблов (служебная).

Доп. метрики для тяжёлых алертов:

- `app_inflight_requests` (GaugeVec по `route,tenant_id`);
- `app_cache_operations_total{cache_hit,route,tenant_id}` (CounterVec, `cache_hit` = `hit`/`miss`);
- `app_queue_size` (GaugeVec по `queue,tenant_id`);
- `app_request_duration_seconds` (HistogramVec, альтернатива с большим числом bucket'ов).

### 1.3. Обработчик `/work`

Выбирать `tenant_id`, `route`, `status_code` случайным образом из соответствующих пространств и инкрементить все Vec-метрики с полным набором лейблов.

### 1.4. Эмуляция трафика

Фоновый тикер дополнительно выбирает случайные `tenant`/`route`/`method` для каждого запроса; вероятность 500 — 20 %, 429/503/504 — по 5 % каждая.

### 1.5. Оценка целевой кардинальности

При `APP_TENANTS=50`, `APP_ROUTES=10`, 5 method, 5 endpoint, 5 status: рядов на `app_requests_total` ≈ 50 × 10 × 5 × 5 × 5 × 3 × 1 = 18 750 на приложение.
При 1700 приложениях — ~25 млн рядов только по одному counter'у. Зафиксировать в README как «очень высокая» ступень и предусмотреть «среднюю» (`APP_TENANTS=5`, `APP_ROUTES=5`) для отладки.

## Этап 2. Сложные алерты в `chart/templates/vmrule.yaml`

Сложные алерты — **не отдельная группа и не отдельный режим**. Классы тяжёлого PromQL (subqueries/joins/`label_*`/`quantile_over_time`/`predict_linear`/`topk`) встраиваются **внутрь существующих 40 `ExtraAlert0xx`** в той же группе `<app>-alerts`, замещая/дополняя часть шаблонов `range` в блоке `alerts.extra`. Отдельная группа `<app>-heavy-alerts` **не создаётся**. Блок `alerts.heavy` в `values.yaml` **не вводится** — используется существующий `alerts.extra.count` (по умолчанию 40). Часть extra-шаблонов теперь ссылается на новые высококардинальные метрики (`app_inflight_requests`, `app_cache_operations_total`, `app_queue_size`, `app_request_duration_seconds`).

### 2.1. Классы тяжёлых выражений

По одному шаблону на класс, размножаются через `range` с вариацией порогов/лейблов как в существующем `extra`:

- **Subqueries:** `max_over_time(rate(app_errors_total{...}[5m])[5m:1m]) > N`, `changes(app_errors_total{...}[10m]) > N`, `deriv(app_inflight_requests{...}[10m]) > N`.
- **Joins:** `rate(app_errors_total{...}[5m]) * on(route,tenant_id) group_left(region) rate(app_requests_total{...}[5m]) > N`, `... or on(...) ...`, `... and on(...) ...`.
- **`label_replace`/`label_join`:** склейка `tenant_id` и `route` в `tenant_route`, агрегация по новой метке.
- **`histogram_quantile` по высококардинальной оси:** `histogram_quantile(0.99, sum by (le, tenant_id, route) (rate(app_request_latency_seconds_bucket{...}[5m]))) > N`.
- **`quantile_over_time`/`stddev_over_time`:** `quantile_over_time(0.95, app_request_latency_seconds_count{...}[10m])`, `stddev_over_time(rate(app_errors_total{...}[5m])[10m:1m])`.
- **`predict_linear`:** `predict_linear(app_inflight_requests{...}[15m], 300) > N`.
- **`absent`/`or vector(fallback)`:** `absent(rate(app_requests_total{...}[5m])) or vector(0)`.
- **`topk`/`bottomk` + `count by`:** `topk(5, sum by (tenant_id) (rate(app_errors_total{...}[5m]))) > N`.

### 2.2. Распределение функций по правилам

Зафиксировать в README (как для текущего профиля): например 30 % subqueries, 25 % joins, 15 % `label_*`, 15 % `histogram_quantile by high-card axis`, 15 % `*_over_time`+`predict_linear`.

### 2.3. Фильтрация

Все тяжёлые правила также фильтровать по `job="{{ appName }}"` — сохранить сопоставимость с текущим профилем по числу правил на приложение.

## Этап 3. Параметризация в `chart/values.yaml`

Добавить только блок кардинальности (блок `alerts.heavy` **не вводится** — сложные алерты живут внутри существующего `alerts.extra`):

```yaml
app:
  cardinality:
    tenants: 50
    routes: 10
    histBuckets: 50
    region: "ru-central1-b"
    version: "1.6.1"
```

Передавать `tenants`/`routes`/`histBuckets` в Deployment как env-переменные.

## Этап 4. Deployment-шаблон

В `chart/templates/deployment.yaml` прокинуть env из `values.app.cardinality`. Здесь же проверить, что `resources.limits` (10m CPU / 20Mi) хватит при росте числа рядов — заложить в TODO отдельный замер RSS генератора при высокой кардинальности (вероятно, потребуется поднять лимит памяти app до 50–100 Mi и скорректировать таблицу «Ресурсные требования генератора нагрузки» в README).

## Этап 5. Скрипты нагрузки

### 5.1. `scripts/deploy-apps.sh`

Режим `HEAVY` **не вводится** — тяжёлая часть всегда активна. Скрипт дополнительно передаёт `app.cardinality.*` через `--set` (tenants/routes/histBuckets) при их задании через env/аргументы. `alerts.heavy.*` не передаётся (блока нет).

### 5.2. `scripts/generate-app-names.sh`

Без изменений.

### 5.3. Отдельного `scripts/deploy-apps-heavy.sh` не должно быть

Отдельный файл-обёртку `scripts/deploy-apps-heavy.sh` **не создавать** — никакого отдельного «тяжёлого режима» нет; сложные алерты и высококардинальные метрики всегда активны и параметризуются через `app.cardinality.*` и `alerts.extra.count` в существующем `scripts/deploy-apps.sh` (см. 5.1). Целевой ориентир: 1700 приложений × 50 алертов = 67 500 правил, но при этом рядов в TSDB на 2–3 порядка больше.

### 5.4. `scripts/fetch_capacity_snapshots.py`

Добавить в `QUERIES` (опционально, по флагу `HEAVY_PROFILE`):

- `sum(vm_rows_scanned_total{job="vmselect-..."})` / rate;
- `sum(vm_cache_*_misses_total{...})`;
- `vm_search_*` (latency по рядам);
- `vm_evaluation_*` для vmalert по тяжёлой группе;
- `histogram_quantile` по `vmalert_iteration_duration_seconds`.

## Этап 6. Обновление README

### 6.1. «Что генерирует нагрузку»

Добавить подраздел «Профиль 2: сложные алерты на высококардинальных метриках» — структура метрик, лейблы, оценка кардинальности, классы PromQL.

### 6.2. «Важная оговорка»

Указать, что добавлен второй профиль и как его включать.

### 6.3. Таблицы результатов

После прогона — новые таблицы по порогам `count(ALERTS)` (отдельные от существующих) с пометкой «профиль 2».

## Этап 7. Замеры и интерпретация

### 7.1. Прогон

Прогнать профиль 2 при тех же порогах `count(ALERTS)` (500 → 50 000).

### 7.2. Сравнение

Сравнить с профилем 1 по тем же метрикам (CPU/RAM `vmalert`/`vmselect`/`vmstorage`, `vm_concurrent_select_*`, `vmalert_iteration_duration_seconds`, RPS vmselect).

### 7.3. Выводы

Зафиксировать разницу в коэффициентах «на 1000 ALERTS» и накладные тяжёлого PromQL.

## Риски / открытые вопросы

- Память `vmstorage` при ~25 млн рядов может не влезть в 5 Gi — заранее оценить `vm_rows_*` на малом числе приложений (10–50) до полного прогона.
- Память самого приложения-генератора при Vec-метриках с тысячами рядов — замерить RSS до деплоя 1700 экз.
- `victoria-metrics-operator` / ConfigMap: 50 тяжёлых правил на приложение могут не поместиться в существующую схему дробления — проверить размер `rulefiles-*` для профиля 2.
- `search.maxConcurrentRequests=32` может оказаться мало при тяжёлом PromQL — заранее планировать рост.
- Неясно, нужен ли `remoteRead/remoteWrite` флаг в `vmalert` — TODO в README (строка 152) перекрывает этот вопрос; до старта профиля 2 ответить на него.

## Порядок выполнения

0 → 3 → 1 → 4 → 5.1 → 5.3 → 2 → 6.1–6.2 → прогон на 10 app → замер RSS/`vm_rows_*` → 7 → 6.3.
