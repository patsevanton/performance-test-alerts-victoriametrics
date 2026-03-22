# Нагрузочное тестирование VictoriaMetrics большим количеством алертов

## Цель

**Главная цель:** получить практическую картину того, как ведёт себя стек VictoriaMetrics в Kubernetes при десятках тысяч активных правил оповещения (`VMRule`): где возникают узкие места (оператор, `vmalert`, хранилище), какие риски для непрерывности оценки правил и для состояния алертов, и какие метрики и наблюдения использовать для контроля такой нагрузки.

**Задачи исследования:**

- оценить увеличение ресурсов при увеличении алертов;
- исследовать, как VictoriaMetrics Operator распределяет правила по ConfigMap'ам при превышении лимита ~1 MiB и как растёт число объектов при добавлении `VMRule`;
- исследовать, при каких условиях происходит пересоздание Pod'а `vmalert`;
- исследовать механизм сохранения и восстановления состояния алертов через `remoteWrite`/`remoteRead` и влияние рестартов на «память» алертов (`for`, pending/firing);
- зафиксировать методику нагрузочного сценария (генерация и поэтапное применение `VMRule`, мониторинг ошибок) для воспроизводимости эксперимента.

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

Перезапуски `vmalert` ожидаемы: при появлении **нового** ConfigMap с правилами под пересобирается с новыми `volume`/`volumeMount`, из‑за чего происходит rolling restart. Если же число ConfigMap'ов не меняется, правила могут подхватываться через SIGHUP без рестарта Pod'а (см. таблицу ниже). Восстановление состояния алертов из VictoriaMetrics выполняется **один раз при старте** процесса `vmalert`; горячая перезагрузка правил (SIGHUP) **не триггерит** restore — см. [документацию](https://docs.victoriametrics.com/victoriametrics/vmalert/#alerts-state-on-restarts).

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
4. Если количество ConfigMap'ов **не изменилось** — Operator обновляет содержимое ConfigMap'ов и помечает аннотацию Pod'а (`configmap-sync-lastupdate-at`), что вызывает **SIGHUP** (горячую перезагрузку) через `config-reloader` sidecar. Pod **не перезапускается**.
5. Если создан **новый ConfigMap** — требуется добавить новый `volume` и `volumeMount` к Pod'у, что **принудительно вызывает пересоздание Pod'а**.

#### Горячая перезагрузка vs перезапуск Pod'а


| Ситуация                                     | Что происходит                                       | Даунтайм       |
| -------------------------------------------- | ---------------------------------------------------- | -------------- |
| Добавлена VMRule, ConfigMap'ы не переполнены | SIGHUP через config-reloader, правила перечитываются | Нет            |
| Добавлена VMRule, требуется новый ConfigMap  | Пересоздание Pod'а с новыми volume mounts            | Есть (секунды) |


#### Сохранение состояния (State Persistence)

vmalert настроен на запись и чтение состояния из VictoriaMetrics:

- **`-remoteWrite.url`** — при каждой оценке vmalert записывает ряды `ALERTS` и `ALERTS_FOR_STATE` в VMCluster (через vminsert);
- **`-remoteRead.url`** — при **старте** процесса vmalert восстанавливает состояние, запрашивая ряды `ALERTS_FOR_STATE` (через vmselect).

В нашем стенде:

```
-remoteWrite.url=http://vminsert-vmks-victoria-metrics-k8s-stack:8480/insert/0/prometheus/api/v1/write
-remoteRead.url=http://vmselect-vmks-victoria-metrics-k8s-stack:8481/select/0/prometheus
```

**`ALERTS_FOR_STATE`** содержит полную информацию о состоянии каждого алерта (`ActiveAt`, `for` duration и т.д.), необходимую для восстановления после рестарта. При запуске vmalert однократно читает этот ряд для восстановления.

Целевые RTO/RPO, сценарии отказов (рестарт vmalert, недоступность VMCluster, потеря PVC у vmstorage, потеря namespace/CRD) и правила дедупликации уведомлений при двух репликах vmalert вынесены в комментарии к [`vmks-values.yaml`](vmks-values.yaml): блок «HA / RTO / RPO» непосредственно перед секцией `vmcluster`.

### Как переобновлять этот срез

```bash
# Kubernetes: VMRule / ConfigMap / ReplicaSet
kubectl get vmrules -A --no-headers | wc -l
kubectl get vmrules -n vmks --no-headers | wc -l
kubectl get vmrules -A -o json | jq '[.items[] | select(.metadata.name | test("^vmrule-"))] | length'
kubectl get configmaps -n vmks -o json | jq '[.items[] | select(.metadata.name | test("rulefiles"))] | length'
kubectl get replicasets -n vmks -l app.kubernetes.io/name=vmalert --no-headers | wc -l

# VictoriaMetrics: ключевые показатели
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=count(ALERTS)' | jq -r '.data.result[0].value[1]'
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=count(ALERTS_FOR_STATE)' | jq -r '.data.result[0].value[1]'
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=sum(vmalert_execution_errors_total)' | jq -r '.data.result[0].value[1]'
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=max(vmalert_iteration_duration_seconds)' | jq -r '.data.result[0].value[1]'
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/status/tsdb' | jq -r '.data.totalSeries'
```

## Capacity Planning

### Ресурсы подов при росте нагрузки

Первая строка таблиц — базовый снимок **до** нагрузочного прогона (`apply-yaml.sh`): `kubectl top pods -n vmks` (среднее по двум подам там, где есть две реплики). Строки **5000–35000** — срезы из VictoriaMetrics в моменты времени, когда `count(ALERTS)` ближе всего к целевому уровню: `query_range` с шагом **1 минуты** по истории прогона **2026-03-22** (UTC), затем instant `query` с параметром `time` для метрик подов (`pod_cpu_usage_seconds_total` / `pod_memory_working_set_bytes`, `namespace=vmks`, среднее по репликам компонента) и операционных метрик; отклонение |`count(ALERTS)` − N| ≤ **150** для каждого уровня N. Прирост ресурсов ожидается пропорционален `count(ALERTS)`.

> **Метрики для таблиц ниже:** таблицы **CPU** и **Memory** — `avg(rate(pod_cpu_usage_seconds_total{namespace="vmks", pod=~"<компонент>-..."}[2m])) * 1000` (милликоры на реплику) и `avg(pod_memory_working_set_bytes{...}) / 1024 / 1024` (MiB); первая строка (455) согласована с `kubectl top pods` (те же `pod_*` с kubelet `/metrics/resource`). Таблица **RPS и операционные метрики** — `apiserver_request_total`, `apiserver_request_duration_seconds_bucket` (p99), `process_cpu_seconds_total` (CPU kube-apiserver), `vm_http_requests_total` по `job` для vmselect/vmstorage/vminsert, `vmalert_iteration_duration_seconds`, `vmalert_execution_errors_total`, `vmalert_iteration_missed_total`, `vmalert_remotewrite_total`. Полные запросы — в [scripts/fetch_capacity_snapshots.py](scripts/fetch_capacity_snapshots.py).


#### CPU (на реплику)


| ALERTS | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| 455    | 16m     | 45m       | 37m      | 12m      | 31m     | 5m       |
| 5000   | 151m    | 118m      | 109m     | 15m      | 54m     | 16m      |
| 10000  | 251m    | 96m       | 127m     | 19m      | 75m     | 34m      |
| 15000  | 438m    | 146m      | 218m     | 28m      | 101m    | 55m      |
| 20000  | 609m    | 156m      | 276m     | 33m      | 145m    | 67m      |
| 25000  | 884m    | 433m      | 342m     | 40m      | 173m    | 104m     |
| 30000  | 905m    | 505m      | 352m     | 45m      | 179m    | 97m      |
| 35000  | 1086m   | 322m      | 423m     | 46m      | 202m    | 121m     |


#### Memory (на реплику)


| ALERTS | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------ | ------- | --------- | -------- | -------- | ------- | -------- |
| 455    | 44Mi    | 227Mi     | 45Mi     | 62Mi     | 83Mi    | 39Mi     |
| 5000   | 136Mi   | 601Mi     | 156Mi    | 64Mi     | 96Mi    | 70Mi     |
| 10000  | 108Mi   | 744Mi     | 150Mi    | 91Mi     | 107Mi   | 85Mi     |
| 15000  | 357Mi   | 1529Mi    | 369Mi    | 145Mi    | 160Mi   | 88Mi     |
| 20000  | 456Mi   | 2057Mi    | 390Mi    | 128Mi    | 168Mi   | 148Mi    |
| 25000  | 633Mi   | 2341Mi    | 495Mi    | 154Mi    | 254Mi   | 175Mi    |
| 30000  | 508Mi   | 2548Mi    | 344Mi    | 261Mi    | 203Mi   | 197Mi    |
| 35000  | 930Mi   | 2650Mi    | 919Mi    | 202Mi    | 254Mi   | 238Mi    |


### RPS и операционные метрики

| ALERTS | API Server RPS | API Server p99 lat | API Server CPU | vmselect HTTP RPS | vmstorage HTTP RPS | vminsert HTTP RPS | vmalert iter_duration (max) | vmalert exec_errors | vmalert iter_missed | vmalert remotewrite_req |
| ------ | -------------- | ------------------ | -------------- | ----------------- | ------------------ | ----------------- | --------------------------- | ------------------- | ------------------- | ----------------------- |
| 455    | 11.4           | 34 ms              | 70m            | 23.2              | 0.1                | 8.0               | 1.01 сек                    | 2 189               | 0                   | 144                     |
| 5000   | 12.3           | 48 ms              | 84m            | 206               | 0.1                | 10.5              | 0.85 сек                    | 0                   | 0                   | 861                     |
| 10000  | 12.3           | 47 ms              | 83m            | 339               | 0.1                | 10.7              | 0.90 сек                    | 0                   | 0                   | 697                     |
| 15000  | 13.1           | 43 ms              | 86m            | 634               | 0.1                | 11.1              | 2.42 сек                    | 0                   | 0                   | 2372                    |
| 20000  | 12.8           | 76 ms              | 88m            | 796               | 0.1                | 11.3              | 3.00 сек                    | 0                   | 0                   | 2961                    |
| 25000  | 13.7           | 79 ms              | 94m            | 1021              | 0.1                | 11.6              | 5.39 сек                    | 0                   | 0                   | 3796                    |
| 30000  | 13.2           | 85 ms              | 96m            | 1105              | 0.1                | 11.8              | 4.67 сек                    | 0                   | 0                   | 4367                    |
| 35000  | 13.4           | 84 ms              | 100m           | 1501              | 0.1                | 12.1              | 9.57 сек                    | 0                   | 0                   | 5415                    |

Колонка **vmalert remotewrite_req** — `sum(rate(vmalert_remotewrite_total[5m]))` (RPS remoteWrite). Воспроизведение строк таблицы: [scripts/fetch_capacity_snapshots.py](scripts/fetch_capacity_snapshots.py).

## Рекомендации по повышению устойчивости

### Краткосрочные

- Отслеживать рост CPU vmalert при продолжении теста (при ~25 000 алертов — **~1 102m** на реплику, запас до лимита 4 CPU ~73%)
- **Критично:** `vmalert_iteration_duration_seconds` достигла **13.19 сек** — при `interval=30s` это занимает **44%** интервала. Рост нелинейный: 0.87s→1.48s→2.68s→3.46s→**13.19s**. Необходимо отслеживать `vmalert_iteration_missed_total` (пока 0)
- Контролировать память vmstorage (**~2 949 Mi** на реплику при ~25 000 алертов; лимит 4 Gi — осталось **~26%** запаса)
- Память vmselect резко выросла до **~1 071 Mi** на реплику (ранее ~218 Mi при ~20 000 алертов) — 5-кратный рост
- `vm_concurrent_select_limit_reached_total` суммарно **67 583** — vmselect упирается в лимит конкурентных запросов, рекомендуется увеличить `-search.maxConcurrentRequests`
- Размер `/metrics` vmalert уже **~31.6 MiB** на реплику — отслеживать приближение к `maxScrapeSize` (128 MB)

## Полезные команды для мониторинга

**Топ метрик при росте нагрузки:** см. [Метрики, выросшие при нагрузке](#метрики-выросшие-при-нагрузке-victoriametrics-stack) ниже — vmalert, vmselect, vmstorage, vminsert, vmagent, Operator, ресурсы подов и примеры запросов ко всем ключевым метрикам.
Примеры `curl` ниже используют внешний ingress `vmselect.apatsev.org.ru`; при выполнении внутри кластера можно заменить на сервис `vmselect-vmks-victoria-metrics-k8s-stack:8481`.

### Размер ConfigMap'ов с правилами

```bash
kubectl get configmaps -n vmks -o json | \
  jq -r '.items[] | select(.metadata.name | contains("rulefiles")) | {
    name: .metadata.name,
    size: (.data | to_entries | map(.value | length) | add // 0)
  } | "\(.name)\t\(.size)"' | \
  awk '{
    size = $2;
    if (size >= 1024*1024) {
      human = sprintf("%.2f MB", size/1024/1024);
    } else if (size >= 1024) {
      human = sprintf("%.2f KB", size/1024);
    } else {
      human = size " bytes";
    }
    printf "%-60s %-15s\n", $1, human
  }' | sort -k2 -hr
```

### Количество VMRule и активных алертов

```bash
kubectl get vmrules -A --no-headers | wc -l

curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=count(ALERTS)' \
  | jq '.data.result[0].value[1]'
```

### Длительность итераций vmalert

```bash
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=max(vmalert_iteration_duration_seconds)' \
  | jq '.data.result[0].value[1]'
```

### История перезапусков vmalert

```bash
kubectl get replicasets -n vmks -l app.kubernetes.io/name=vmalert \
  -o custom-columns='NAME:.metadata.name,DESIRED:.spec.replicas,CREATED:.metadata.creationTimestamp' \
  --sort-by=.metadata.creationTimestamp
```

### Kubernetes / System / API Server (ключевые метрики)

```bash
# CPU kube-apiserver (cores -> mCPU: *1000)
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=sum(rate(process_cpu_seconds_total{job=~".*apiserver.*"}[5m]))' \
  | jq -r '.data.result[0].value[1]'

# RPS
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=sum(rate(apiserver_request_total[5m]))' \
  | jq -r '.data.result[0].value[1]'

# p99 latency (исключить le="+Inf", иначе p99 может быть завышен)
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket{le!="+Inf"}[5m])) by (le))' \
  | jq -r '.data.result[0].value[1]'
```


## Метрики, выросшие при нагрузке (VictoriaMetrics stack)

> Данные ниже — обобщение **прошлых** прогонов (до ~23 000 алертов). Базовый снимок текущего эксперимента и последующие этапы — в таблицах раздела [Capacity Planning](#capacity-planning).

Оценки даны для роста от малой нагрузки до **~23 000 алертов** (~252 VMRule, ~4,79 млн рядов). Базовый URL запросов: `http://vmselect-vmks-victoria-metrics-k8s-stack:8481/select/0/prometheus`.


### 1. VMAlert (vmalert)

- **vmalert_iteration_duration_seconds** — выросла в **десятки раз** (оценка всех групп за одну итерацию занимает существенную долю interval).
- **vmalert_iteration_missed_total** — при перегрузке растёт; при ~15 000 алертов может оставаться 0, при дальнейшем росте — рост в **разы**.
- **vmalert_execution_errors_total** — при сбоях vmselect/vminsert рост от 0 до **единиц–десятков** в час.
- **vmalert_alerts_firing** / **vmalert_alerts_pending** — растут **пропорционально числу правил** (~23 000 ALERTS, sum(vmalert_alerts_firing) ~36 883 по 2 репликам).
- **vmalert_remotewrite_requests_total** — рост примерно **в 2–3 раза** от числа групп (запись ALERTS и ALERTS_FOR_STATE при каждой итерации).
- **vmalert_remoteread_requests_total** — скачок при каждом рестарте vmalert (один большой запрос при старте).
- **container_cpu_usage_seconds_total** (vmalert) — при ~23 000 алертов **~723m** (в среднем на реплику); относительно пустого старта рост в **несколько раз**, запас до лимита 4 CPU.
- **container_memory_working_set_bytes** (vmalert) — **~2.5–3 раза** (с ~200–300 Mi до ~676 Mi).


### 2. VMSelect

- **vm_concurrent_select_current** — среднее значение выросло в **2–5 раз** (много запросов от vmalert на eval и от remoteRead при рестартах).
- **vm_concurrent_select_limit_reached_total** — при приближении к лимиту рост от 0 до **единиц–сотен** в час.
- **vm_concurrent_select_limit_timeout_total** — при перегрузке рост от 0; в норме 0.
- **vm_select_request_duration_seconds** — p99 вырос в **2–4 раза** (тяжёлые запросы по ALERTS_FOR_STATE и правилам).
- **vm_http_requests_total** (job=vmselect) — запросы к select выросли **пропорционально числу групп и интервалам** (в **десятки раз**).


### 3. VMStorage

- **vm_rows** / **vm_rows_inserted_total** — рост **пропорционально числу рядов** (~4,79 млн при ~23 000 алертов; от нуля — в **тысячи раз**).
- **vm_storage_blocks** — рост в **разы** с ростом объёма данных.
- **vm_cache_*_requests_total** / **vm_cache_*_misses_total** — объём запросов вырос в **разы**; miss rate может вырасти в **1,5–2 раза** при нехватке кэша.
- **vm_http_requests_total** (job=vmstorage) — запросы от vmselect выросли в **десятки раз**.


### 4. VMInsert

- **vm_http_requests_total** (job=vminsert, path insert) — рост в **2–3 раза** от числа групп vmalert (remote write при каждой итерации).
- **vm_insert_request_duration_seconds** — при росте объёма записи p99 может вырасти в **1,5–3 раза**.
- **vm_insert_requests_total** — рост **пропорционально** записи (в **десятки раз** относительно малой нагрузки).


### 5. VMAgent

- **scrape_series_added** (target=vmalert) — выросло в **десятки раз** (размер /metrics vmalert растёт с числом правил и алертов).
- **scrape_body_size_bytes** (target=vmalert) — рост в **10–20+ раз** (при ~23 000 алертов уже сотни KB; при ~50 000+ алертов может превысить maxScrapeSize 16 MB).
- **scrape_samples_scraped** (job=vmalert) — рост **пропорционально** числу метрик vmalert (в **десятки раз**).


### 6. VictoriaMetrics Operator

- **process_cpu_seconds_total** (job=operator) — при ~23 000 алертов **~52m**; относительно малой нагрузки рост в **2–5 раз** (reconcile по всем правилам и сборке ConfigMap).
- **process_resident_memory_bytes** (job=operator) — **~173Mi** при ~23 000 алертов; рост в **1.5–2 раза** с ростом числа алертов.


### 7. Kubernetes / ресурсы подов

- **container_cpu_usage_seconds_total** (vmalert) — см. раздел 1; **container_cpu** для vmselect, vmstorage, vminsert — рост в **2–4 раза** при той же нагрузке.
- **container_memory_working_set_bytes** (vmstorage) — при ~23 000 алертов **~1 613–1 662 Mi** на реплику; рост в **3–5 раз** от старта.
- **container_memory_working_set_bytes** (vmselect) — **~221–257Mi**; рост в **1,5–3 раза**.


### 8. Алерты и объём данных

- **count(ALERTS)** — при ~23 000 алертов **~22 973**; рост от 0 до этого значения (фактически **на порядки**).
- **count(ALERTS_FOR_STATE)** — **~21 298**; того же порядка, что и ALERTS; рост **пропорционально** числу алертов.
- **totalSeries** (через API/tsdb) — при ~23 000 алертов **~4,79 млн** рядов; рост от нуля в **тысячи раз**.


### Как считать прирост

- Для счётчиков: `increase(metric_name[1h])` или сравнение с периодом до нагрузки.
- Для gauge (CPU, память, длительность): сравнение средних/перцентилей «до» и «после» по тому же окну.
- Список имён метрик: `GET /api/v1/label/__name__/values`, затем фильтр по префиксу (`vmalert_`*, `vm_concurrent_select_*` и т.д.).
- Ресурсы подов: `kubectl top pods -n vmks`.


## Заключение и выводы

Проведённое нагрузочное тестирование VictoriaMetrics stack большим количеством VMRule подтвердило заявленные цели и позволило сформулировать практические выводы.

### Достигнутые результаты

- **Распределение правил по ConfigMap'ам:** Operator стабильно дробит правила при приближении к лимиту ~1 MiB: каждый ConfigMap заполняется до ~505–509 KB, затем создаётся следующий. При ~23 000 алертов наблюдается 17 ConfigMap'ов (~8,5 MB суммарно). Механизм предсказуем и масштабируется линейно.
- **Перезапуски vmalert:** Каждое появление нового ConfigMap приводит к пересозданию Pod'а vmalert (из-за добавления volume/volumeMount). Интервал между рестартами составил ~13–15 мин. За время теста зафиксировано 15 ReplicaSet'ов (14 пересозданий). Горячая перезагрузка (SIGHUP) применяется только при обновлении существующих ConfigMap'ов без добавления новых.
- **Сохранение состояния:** Механизм remoteWrite/remoteRead (`ALERTS`, `ALERTS_FOR_STATE`) работает корректно: после рестарта vmalert восстанавливает состояние алертов из VictoriaMetrics, счётчики `for` не сбрасываются, потери алертов не происходит. `vmalert_execution_errors_total = 0` и `vmalert_iteration_missed_total = 0` подтверждают стабильную работу. Временное падение `sum(ALERTS)` во время рестарта — следствие задержки первой итерации, а не потери данных.
- **Пороги масштабируемости:** При ~23 000 алертов (~4,79 млн рядов) vmalert потребляет ~723m CPU и ~676Mi памяти на реплику — нагрузка заметная, но в пределах лимитов (4 CPU / 4 Gi). VM Operator потребляет ~52m CPU. `max(vmalert_iteration_duration_seconds)` достигла 3.84 сек — важный индикатор приближения к пределу при интервалах 30s. Линейная экстраполяция до ~100 000 алертов указывает на необходимость подготовки шардирования vmalert и масштабирования VMCluster.

### Основные выводы

1. **Операционная модель:** При массовом добавлении VMRule следует учитывать периодические рестарты vmalert (по одному на каждый новый ConfigMap). Для production целесообразно применять правила батчами или через GitOps с контролируемым темпом, чтобы не создавать лишние ConfigMap'ы подряд и снизить частоту рестартов.
2. **Ресурсы:** При ~23 000 алертов CPU vmalert ~723m — запас до лимита 4 CPU есть. При росте к ~100 000 алертов потребуется довести лимит до 3–4 CPU и подготовить шардирование vmalert с масштабированием VMCluster. vmstorage потребляет ~1.6 Gi RAM на реплику — следует учитывать при планировании ресурсов нод.
3. **Отказоустойчивость:** Конфигурация с двумя репликами vmalert, remoteRead/remoteWrite и Alertmanager в кластерном режиме обеспечивает восстановление без потери состояния алертов и без дублирования уведомлений. RTO vmalert — в пределах минуты (подробности RTO/RPO и сценарии — в комментариях к `vmks-values.yaml`). На текущем этапе `vmalert_execution_errors_total = 0` и `vmalert_iteration_missed_total = 0`.
4. **Мониторинг:** Ключевые метрики для раннего обнаружения перегрузки — `vmalert_iteration_duration_seconds` (текущий max ~3.84 сек — рост в 2.4 раза по сравнению со снимком ~17 000 алертов), `vmalert_iteration_missed_total`, `container_cpu_usage_seconds_total` (vmalert), а также размер и количество ConfigMap'ов с правилами.

Итог: VictoriaMetrics stack при правильной настройке (remoteRead/remoteWrite, HA vmalert и Alertmanager) выдерживает нагрузку тысячами правил и алертов с сохранением целостности состояния. Ограничения носят в основном ресурсный характер и снимаются шардированием и увеличением ресурсов в соответствии с приведёнными рекомендациями.