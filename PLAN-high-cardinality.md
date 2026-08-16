# План перехода на сложные и высококардинальные метрики

## Цель

Добавить второй нагрузочный профиль «сложные алерты на высококардинальных метриках» параллельно существующему профилю «много простых алертов на низкокардинальных метриках», чтобы получить сравнимые ориентиры capacity planning для тяжёлого PromQL.

Принцип: текущий профиль не ломается, новый включается через `values.yaml`.

## Этап 0. Фиксация рамок

Решить и зафиксировать в `README.md` (раздел «Важная оговорка») параметры нового профиля:

- кардинальность на одно приложение (целевое число рядов на метрику);
- набор лейблов;
- классы тяжёлых PromQL (subqueries / joins / `label_*` / `quantile_over_time` / `predict_linear` / `topk`);
- число алертов на приложение в новом профиле;
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
При 1350 приложениях — ~25 млн рядов только по одному counter'у. Зафиксировать в README как «очень высокая» ступень и предусмотреть «среднюю» (`APP_TENANTS=5`, `APP_ROUTES=5`) для отладки.

## Этап 2. Сложные алерты в `chart/templates/vmrule.yaml`

Добавить в `values.yaml` блок `alerts.heavy` (`enabled: bool`, `count: int`, `profile: low|medium|high`). При `alerts.heavy.enabled=true` в `VMRule` добавляется группа `<app>-heavy-alerts`.

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

Добавить:

```yaml
app:
  cardinality:
    tenants: 50
    routes: 10
    histBuckets: 50
    region: "ru-central1-b"
    version: "1.6.1"
alerts:
  heavy:
    enabled: false
    count: 50
    profile: "high"
```

Передавать `tenants`/`routes`/`histBuckets` в Deployment как env-переменные.

## Этап 4. Deployment-шаблон

В `chart/templates/deployment.yaml` прокинуть env из `values.app.cardinality`. Здесь же проверить, что `resources.limits` (10m CPU / 20Mi) хватит при росте числа рядов — заложить в TODO отдельный замер RSS генератора при высокой кардинальности (вероятно, потребуется поднять лимит памяти app до 50–100 Mi и скорректировать таблицу «Ресурсные требования генератора нагрузки» в README).

## Этап 5. Скрипты нагрузки

### 5.1. `scripts/deploy-apps.sh`

Добавить режим `HEAVY=1` (передаёт `alerts.heavy.enabled=true` и `app.cardinality.*` через `--set`).

### 5.2. `scripts/generate-app-names.sh`

Без изменений.

### 5.3. `scripts/deploy-apps-heavy.sh`

Новый (обёртка над `deploy-apps.sh` с предустановленными `--set`) — для воспроизводимости. Записать target: 1350 приложений × 50 тяжёлых алертов = 67 500 правил, но при этом рядов в TSDB на 2–3 порядка больше.

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
- Память самого приложения-генератора при Vec-метриках с тысячами рядов — замерить RSS до деплоя 1350 экз.
- `victoria-metrics-operator` / ConfigMap: 50 тяжёлых правил на приложение могут не поместиться в существующую схему дробления — проверить размер `rulefiles-*` для профиля 2.
- `search.maxConcurrentRequests=32` может оказаться мало при тяжёлом PromQL — заранее планировать рост.
- Неясно, нужен ли `remoteRead/remoteWrite` флаг в `vmalert` — TODO в README (строка 152) перекрывает этот вопрос; до старта профиля 2 ответить на него.

## Порядок выполнения

0 → 3 → 1 → 4 → 5.1 → 5.3 → 2 → 6.1–6.2 → прогон на 10 app → замер RSS/`vm_rows_*` → 7 → 6.3.
