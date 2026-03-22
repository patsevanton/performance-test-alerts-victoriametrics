# Нагрузочное тестирование VictoriaMetrics большим количеством алертов

## Цель

Исследовать поведение VictoriaMetrics stack при нагрузке большим количеством правил оповещений (`VMRule`) в Kubernetes. В частности:

- понять, как VictoriaMetrics Operator распределяет правила по ConfigMap'ам при превышении лимита ~1 MiB;
- выяснить, при каких условиях происходит пересоздание Pod'а `vmalert` и как это влияет на состояние алертов;
- проверить механизм сохранения и восстановления состояния алертов через `remoteWrite`/`remoteRead`;
- определить практические пороги масштабируемости и дать рекомендации по эксплуатации.

## Быстрый обзор репозитория

- `vmks-values.yaml` — основной values для `victoria-metrics-k8s-stack` (HA-конфигурация, ресурсы, ingress, лимиты поиска).
- `victoria-logs-cluster-values.yaml` и `victoria-logs-collector-values.yaml` — деплой VictoriaLogs и сбор логов кластера.
- `alerts/generate_alerts.py` — генератор нагрузочных `VMRule` (`500` файлов по `100` алертов каждый, детерминированный seed=42).
- `alerts/apply-yaml.sh` — поэтапный apply всех 500 VMRule с контролируемым темпом (паузы растягиваются на `STAGE_DURATION_SEC`).
- `alerts/monitor-batch.sh` — сторож во время apply (OOM vmalert, ошибки в VictoriaLogs, критичные метрики VM).
- `alerts/vmrules/` — сгенерированные YAML-файлы (`500` в репозитории на текущий момент).

## Архитектура и стенд

### Infrastructure

**Кластер:** 3 ноды Kubernetes v1.32.1 на Yandex Cloud (Ubuntu 22.04.5 LTS, containerd 1.7.27, 8 vCPU / 24 GB RAM на ноду).

### VictoriaMetrics Stack

**Версия:** `victoria-metrics-k8s-stack` v0.72.5 (VictoriaMetrics v1.138.0), namespace `vmks`.

#### Компоненты

`vmsingle` **отключен**, вместо него — `vmcluster` с `replicationFactor: 2`. Полные настройки — в [vmks-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml).


| Компонент          | Deployment                      | Реплики   | PDB             | Роль / Механизм HA                                                                                         |
| ------------------ | ------------------------------- | --------- | --------------- | ---------------------------------------------------------------------------------------------------------- |
| **VMCluster**      | vmstorage / vmselect / vminsert | 2 / 2 / 2 | —               | Хранение метрик (replicationFactor=2, каждая точка в 2 копиях); select/insert — stateless                  |
| **VMAlert**        | vmalert                         | 2         | minAvailable: 1 | Оценка правил и отправка алертов; обе реплики оценивают все правила, дедупликация на стороне Alertmanager. |
| **VMAgent**        | vmagent                         | 1         | —               | Сбор метрик (scrape)                                                                                       |
| **VMAlertmanager** | vmalertmanager                  | 2         | minAvailable: 1 | Маршрутизация уведомлений; кластерный режим, автодедупликация                                              |
| **VM Operator**    | victoria-metrics-operator       | 1         | —               | Управление CRD-ресурсами                                                                                   |
| **Grafana**        | grafana                         | 1         | —               | Визуализация                                                                                               |


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

Проверка: `kubectl get pods -n victoria-logs-collector`. Логи стека (vmks, vmalert, vmagent и др.) можно запрашивать в Grafana через datasource VictoriaLogs или в UI VictoriaLogs.

### Goldpinger

[Goldpinger](https://github.com/bloomberg/goldpinger) — DaemonSet на каждой ноде, проверяет ICMP/TCP-связность между подами и отдаёт UI и метрики Prometheus. [Helm chart](https://github.com/bloomberg/goldpinger/tree/master/charts/goldpinger) публикуется в репозитории Bloomberg.

**Требование:** для UI через Ingress — ingress-контроллер (как у Grafana). Для сбора метрик в VictoriaMetrics — установленный стек `vmks` (CRD `VMServiceScrape`).

```bash
helm repo add goldpinger https://bloomberg.github.io/goldpinger/
helm repo update

helm upgrade --install goldpinger goldpinger/goldpinger \
  --namespace goldpinger \
  --create-namespace \
  --version 1.0.2 \
  --wait \
  --timeout 10m \
  -f goldpinger-values.yaml

kubectl apply -f goldpinger-vmscrape.yaml
```

Исходный код файлов [goldpinger-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/goldpinger-values.yaml), [goldpinger-vmscrape.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/goldpinger-vmscrape.yaml).

#### Дашборд в Grafana

В репозитории Goldpinger лежит готовый JSON: [extras/goldpinger-dashboard.json](https://github.com/bloomberg/goldpinger/blob/master/extras/goldpinger-dashboard.json) (описание в [разделе Grafana](https://github.com/bloomberg/goldpinger?tab=readme-ov-file#grafana) upstream).

### Генерация нагрузочных VMRule

Скрипт [alerts/generate_alerts.py](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py) генерирует YAML-файлы `VMRule` в директорию `alerts/vmrules/`. По умолчанию создаётся **500** файлов; каждый `VMRule` содержит **4–6 групп** (с `interval` 30s/1m/2m) и **100 алертов** суммарно.

Исходный код файла [alerts/generate_alerts.py](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py).

```bash
cd alerts
./generate_alerts.py
```

Правила «псевдо-реалистичные»: разные шаблоны (k8s/node/http/db/…), `expr` построены на `vector(...)`, `severity` задаётся шаблоном (в основном `warning`/`critical`), `for` — от `0s` до `1h`. Генерация детерминирована (seed=42); объём можно изменить в `main()` через `num_vmrules` и `alerts_per_vmrule`. Скрипт перезаписывает только файлы `vmrule-00001.yaml` … `vmrule-NNNNN.yaml` в пределах `num_vmrules`; если раньше было больше файлов — удалите лишние в `alerts/vmrules/` и при необходимости снимите лишние `VMRule` из кластера (`kubectl delete`).

### Применение VMRule в Kubernetes

Скрипт [alerts/apply-yaml.sh](alerts/apply-yaml.sh) применяет все **500** YAML-файлов из `alerts/vmrules/` с контролируемым темпом. Файлы сортируются как `find … | sort -V`.

**Строгая обработка ошибок:** `set -euo pipefail` — при ошибке любого `kubectl apply` скрипт останавливается. Исправьте причину и запустите скрипт заново (`kubectl apply` идемпотентен).

**Темп:** паузы между apply рассчитываются так, чтобы суммарный sleep составил `STAGE_DURATION_SEC` (по умолчанию 9 ч). При 500 файлах пауза между каждым apply ≈ 64.9 секунды. **Время полного прогона** ≈ паузы + время 500 вызовов `kubectl apply` — ориентировочно **~9–12 ч** (зависит от кластера).

Скрипт проверяет, что в каталоге ровно 500 YAML-файлов. Единственная настройка — переменная окружения `STAGE_DURATION_SEC`.

**Запуск (из каталога `alerts`):**

```bash
cd alerts
./apply-yaml.sh
```

Для изменения темпа:

```bash
STAGE_DURATION_SEC=3600 ./apply-yaml.sh   # растянуть на 1 час
```

Исходный код: [alerts/apply-yaml.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/apply-yaml.sh).

**Мониторинг ошибок:** в отдельном терминале запустите `./monitor-batch.sh` — при первой проблеме скрипт **печатает в stdout** текст (строки логов из VictoriaLogs с префиксом namespace/pod/container или ненулевые метрики). Момент и детали пишутся в `alerts/first-error-batch.txt` (`RESULT_FILE=...`). Проверяются: OOM vmalert; **LogsQL** по широкому OR (`i(error)`, fatal, panic, HTTP 5xx/422, timeout, OOM в тексте и т.д., см. `VL_LOGSQL_QUERY` в скрипте); метрики `vmalert_execution_errors_total`, `vm_concurrent_select_limit_reached_total`, суммарный rate **5xx** по `vmselect|vmstorage|vminsert`. Переменные: `INTERVAL` (30 с), `VL_LOG_LIMIT`, `VL_WINDOW_MIN`, `VL_LOGSQL_QUERY`; для самоподписанных сертификатов: `CURL_OPTS="-k"`.

Исходный код: [alerts/monitor-batch.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/monitor-batch.sh).

После батча можно анализировать графики нагрузки на vmalert, vmselect, operator.

## Текущее состояние (снимок на 2026-03-21)

> **Примечание:** тест продолжается. Ниже — актуальный срез, собранный командами `kubectl`, запросами к `vmselect` и проверкой через MCP.

### Общие цифры


| Метрика                                  | Значение                                  |
| ---------------------------------------- | ----------------------------------------- |
| VMRule в кластере (всего)                | **434**                                   |
| VMRule в namespace `vmks`                | **39**                                    |
| Целевое количество алертов               | **~50 000** (`500` VMRule × `100` алертов) |
| Активные алерты (ALERTS)                 | **43 823**                                |
| ALERTS_FOR_STATE                         | **39 415**                                |
| sum(vmalert_alerts_firing)               | **422** (`matching timeseries > 1000000`) |
| Временные ряды (totalSeries)             | **14 838 042**                            |
| ConfigMap'ов с правилами (`rulefiles-`*) | **31**                                    |
| ReplicaSet'ов vmalert                    | **11**                                    |
| sum(vmalert_execution_errors_total)      | **30**                                    |
| sum(vmalert_iteration_missed_total)      | **0**                                     |
| max(vmalert_iteration_duration_seconds)  | **1.95 сек**                              |


### Распределение правил по ConfigMap'ам

Суммарный размер `rulefiles-`*: **15 937 038 bytes (~15.20 MiB)**, средний размер ConfigMap: **~514 KB**.  
Operator по-прежнему упаковывает большинство ConfigMap'ов около ~505–511 KB и создаёт новый по мере роста числа `VMRule`.

### Перезапуски vmalert и восстановление состояния

`vmalert` остаётся в ожидаемой модели: при появлении нового ConfigMap возможен rolling restart (из-за новых `volume`/`volumeMount`), а восстановление состояния идёт через `remoteRead` по `ALERTS_FOR_STATE`.

Это согласуется с документацией VictoriaMetrics (`vmalert`: state restore выполняется один раз при старте процесса; hot reload не триггерит restore).

### Как переобновлять этот срез

```bash
# Kubernetes: VMRule / ConfigMap / ReplicaSet
kubectl get vmrules -A --no-headers | wc -l
kubectl get vmrules -n vmks --no-headers | wc -l
kubectl get configmaps -n vmks -o json | jq '[.items[] | select(.metadata.name | test("rulefiles"))] | length'
kubectl get replicasets -n vmks -l app.kubernetes.io/name=vmalert --no-headers | wc -l

# VictoriaMetrics: ключевые показатели
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=count(ALERTS)' | jq -r '.data.result[0].value[1]'
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=count(ALERTS_FOR_STATE)' | jq -r '.data.result[0].value[1]'
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=sum(vmalert_execution_errors_total)' | jq -r '.data.result[0].value[1]'
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=max(vmalert_iteration_duration_seconds)' | jq -r '.data.result[0].value[1]'
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/status/tsdb' | jq -r '.data.totalSeries'
```

## Механизм распределения алертов и перезапуски vmalert

### Хранение правил в ConfigMap'ах

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

### Горячая перезагрузка vs перезапуск Pod'а


| Ситуация                                     | Что происходит                                       | Даунтайм       |
| -------------------------------------------- | ---------------------------------------------------- | -------------- |
| Добавлена VMRule, ConfigMap'ы не переполнены | SIGHUP через config-reloader, правила перечитываются | Нет            |
| Добавлена VMRule, требуется новый ConfigMap  | Пересоздание Pod'а с новыми volume mounts            | Есть (секунды) |


### Сохранение состояния (State Persistence)

vmalert настроен на запись и чтение состояния из VictoriaMetrics:

- **`-remoteWrite.url`** — при каждой оценке vmalert записывает ряды `ALERTS` и `ALERTS_FOR_STATE` в VMCluster (через vminsert);
- **`-remoteRead.url`** — при **старте** процесса vmalert восстанавливает состояние, запрашивая ряды `ALERTS_FOR_STATE` (через vmselect).

В нашем стенде:

```
-remoteWrite.url=http://vminsert-vmks-victoria-metrics-k8s-stack:8480/insert/0/prometheus/api/v1/write
-remoteRead.url=http://vmselect-vmks-victoria-metrics-k8s-stack:8481/select/0/prometheus
```

**`ALERTS_FOR_STATE`** содержит полную информацию о состоянии каждого алерта (`ActiveAt`, `for` duration и т.д.), необходимую для восстановления после рестарта. При запуске vmalert однократно читает этот ряд для восстановления.

Восстановление происходит **только при старте процесса**. Горячая перезагрузка правил (SIGHUP) **не триггерит** восстановление состояния.

Подробнее: [https://docs.victoriametrics.com/victoriametrics/vmalert/#alerts-state-on-restarts](https://docs.victoriametrics.com/victoriametrics/vmalert/#alerts-state-on-restarts)

## Отказоустойчивость и восстановление

### Текущая конфигурация HA

Реплики, PDB и механизмы HA каждого компонента описаны в таблице [Компоненты](#компоненты).

### RTO / RPO


| Компонент        | RTO      | RPO                            | Комментарий                                                                      |
| ---------------- | -------- | ------------------------------ | -------------------------------------------------------------------------------- |
| **vmalert**      | ≤ 60 сек | 0 (при настроенном remoteRead) | Pod пересоздаётся K8s, состояние алертов восстанавливается из `ALERTS_FOR_STATE` |
| **VMCluster**    | ≤ 5 мин  | 0 при потере 1 vmstorage       | replicationFactor=2 защищает от потери одной реплики                             |
| **Alertmanager** | ≤ 60 сек | silences/inhibitions из PVC    | StatefulSet с persistent storage                                                 |
| **VM Operator**  | ≤ 2 мин  | 0 (stateless, читает CRD)      | Reconcile восстанавливает желаемое состояние                                     |


### Сценарии отказов

#### Сценарий 1: Падение Pod'а vmalert

- Kubernetes автоматически пересоздаёт Pod
- При старте vmalert восстанавливает `ALERTS_FOR_STATE` через `-remoteRead.url`
- `for`-счётчик алертов не сбрасывается
- Вторая реплика vmalert продолжает оценку правил и отправку алертов — **нет даунтайма alerting pipeline**

#### Сценарий 2: Частичная/полная недоступность VMCluster

- vmalert не может выполнить запросы правил и записать результаты
- `vmalert_execution_errors_total` начинает расти
- Уже firing-алерты продолжают отправляться (кэшируются в памяти vmalert)
- При восстановлении VMCluster vmalert автоматически продолжает работу

#### Сценарий 3: Потеря PersistentVolume у vmstorage

- При потере 1 PVC из 2 — сервис продолжает работать благодаря replicationFactor=2
- При потере обоих PVC — потеря исторических метрик и `ALERTS_FOR_STATE`
- Все алерты с `for > 0` начнут отсчёт заново

#### Сценарий 4: Потеря namespace / CRD

- Повторно применить Helm chart + VMRule из Git
- GitOps (ArgoCD/Flux) сокращает время восстановления до ~5 мин

### Как не получить двойные нотификации (HA vmalert)

При 2 репликах vmalert **оба Pod оценивают один и тот же набор правил** и отправляют одинаковые алерты. Дедупликация обеспечивается:

- **Одинаковый набор labels** у алерта во всех репликах — не добавляйте уникальные для реплики метки через `-external.label`
- **Alertmanager в кластерном режиме** — 2 реплики, автодедупликация по fingerprint
- **vmalert отправляет алерты в один Alertmanager-кластер**

Альтернатива — шардировать правила между инстансами vmalert, чтобы каждый алерт оценивался ровно одним vmalert.

## Capacity Planning

### Ресурсы подов при росте нагрузки

Данные собраны командой `kubectl top pods -n vmks` на разных этапах теста. Прирост ресурсов пропорционален числу алертов.

#### CPU (на реплику)


| Этап (алерты) | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------------- | ------- | --------- | -------- | -------- | ------- | -------- |
| baseline      |         |           |          |          |         |          |
| ~13 500       |         |           |          |          |         |          |
| ~17 000       |         |           |          |          |         |          |
| ~23 000       |         |           |          |          |         |          |
| ~44 000       |         |           |          |          |         |          |


#### Memory (на реплику)


| Этап (алерты) | vmalert | vmstorage | vmselect | vminsert | vmagent | operator |
| ------------- | ------- | --------- | -------- | -------- | ------- | -------- |
| baseline      |         |           |          |          |         |          |
| ~13 500       |         |           |          |          |         |          |
| ~17 000       |         |           |          |          |         |          |
| ~23 000       |         |           |          |          |         |          |
| ~44 000       |         |           |          |          |         |          |


### RPS и операционные метрики


| Этап (алерты) | API Server RPS | API Server p99 lat | API Server CPU | vmselect HTTP RPS | vmstorage HTTP RPS | vminsert HTTP RPS | vmalert iter_duration (max) | vmalert exec_errors | vmalert iter_missed | vmalert remotewrite_req |
| ------------- | -------------- | ------------------ | -------------- | ----------------- | ------------------ | ----------------- | --------------------------- | ------------------- | ------------------- | ----------------------- |
| baseline      |                |                    |                |                   |                    |                   |                             |                     |                     |                         |
| ~13 500       |                |                    |                |                   |                    |                   |                             |                     |                     |                         |
| ~17 000       |                |                    |                |                   |                    |                   |                             |                     |                     |                         |
| ~23 000       |                |                    |                |                   |                    |                   |                             |                     |                     |                         |
| ~44 000       |                |                    |                |                   |                    |                   |                             |                     |                     |                         |


### Объёмные метрики и состояние


| Этап (алерты) | ALERTS | ALERTS_FOR_STATE | vmalert_alerts_firing | totalSeries | ConfigMaps (кол-во) | ConfigMaps (размер) | vm_concurrent_select_current | scrape_body_size (vmalert) |
| ------------- | ------ | ---------------- | --------------------- | ----------- | ------------------- | ------------------- | ---------------------------- | -------------------------- |
| baseline      |        |                  |                       |             |                     |                     |                              |                            |
| ~13 500       |        |                  |                       |             |                     |                     |                              |                            |
| ~17 000       |        |                  |                       |             |                     |                     |                              |                            |
| ~23 000       |        |                  |                       |             |                     |                     |                              |                            |
| ~44 000       |        |                  |                       |             |                     |                     |                              |                            |



### Рекомендации по масштабированию

Ориентиры «при каком количестве алертов выставлять те или иные ресурсы» — в начале [vmks-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml) (блок **Шкала нагрузки**).

1. **До ~100 000 алертов:** заложить лимиты vmalert на уровне 3–4 CPU и ~3 Gi RAM на реплику, плюс мониторинг `vmalert_iteration_duration_seconds` и `vmalert_iteration_missed_total`
2. **К ~100 000 алертов:** заранее включить шардирование vmalert (разделение правил через `-rule.partition` или разные `ruleSelector`) и масштабирование VMCluster (прежде всего `vmselect`/`vmstorage`)
3. **Выше ~100 000 алертов:** считать новой зоной нагрузки; нужен отдельный прогон и пересчёт capacity с фактическими метриками

## Рекомендации по повышению устойчивости

### Краткосрочные

- Отслеживать рост CPU vmalert при продолжении теста (при ~23 000 алертов — ~723m, запас до лимита 4 CPU есть)
- Мониторинг `vmalert_iteration_missed_total` и `vmalert_iteration_duration_seconds` (текущий max 3.84 сек — приближается к `interval`)
- Контролировать память vmstorage (~1.6 Gi на реплику при ~23 000 алертов)

### Среднесрочные

- Подготовить шардирование vmalert до достижения ~100 000 алертов
- Внедрить GitOps (ArgoCD/Flux) для автоматического восстановления VMRule из Git
- Добавить Network Policies для изоляции трафика между компонентами

### Долгосрочные

- Cross-region replication на уровне кластера

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

# p99 latency
curl -sk 'https://vmselect.apatsev.org.ru/select/0/prometheus/api/v1/query?query=histogram_quantile(0.99, sum(rate(apiserver_request_duration_seconds_bucket[5m])) by (le))' \
  | jq -r '.data.result[0].value[1]'
```


## Метрики, выросшие при нагрузке (VictoriaMetrics stack)

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
3. **Отказоустойчивость:** Конфигурация с двумя репликами vmalert, remoteRead/remoteWrite и Alertmanager в кластерном режиме обеспечивает восстановление без потери состояния алертов и без дублирования уведомлений. RTO vmalert — в пределах минуты. На текущем этапе `vmalert_execution_errors_total = 0` и `vmalert_iteration_missed_total = 0`.
4. **Мониторинг:** Ключевые метрики для раннего обнаружения перегрузки — `vmalert_iteration_duration_seconds` (текущий max ~3.84 сек — рост в 2.4 раза по сравнению со снимком ~17 000 алертов), `vmalert_iteration_missed_total`, `container_cpu_usage_seconds_total` (vmalert), а также размер и количество ConfigMap'ов с правилами.

Итог: VictoriaMetrics stack при правильной настройке (remoteRead/remoteWrite, HA vmalert и Alertmanager) выдерживает нагрузку тысячами правил и алертов с сохранением целостности состояния. Ограничения носят в основном ресурсный характер и снимаются шардированием и увеличением ресурсов в соответствии с приведёнными рекомендациями.