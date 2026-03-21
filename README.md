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
- `alerts/apply-yaml-batch-01.sh` + `alerts/apply-yaml-lib.sh` — поэтапный apply с длинным stage-duration и аннотациями в Grafana.
- `alerts/monitor-batch.sh` — сторож во время apply (OOM vmalert, ошибки в VictoriaLogs, критичные метрики VM).
- `alerts/vmrules/` — сгенерированные YAML-файлы (`500` в репозитории на текущий момент).

## Архитектура и Стенд

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

Исходный код файла [victoria-logs-cluster-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/victoria-logs-cluster-values.yaml)

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

Исходный код файла [victoria-logs-collector-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/victoria-logs-collector-values.yaml)

Проверка: `kubectl get pods -n victoria-logs-collector`. Логи стека (vmks, vmalert, vmagent и др.) можно запрашивать в Grafana через datasource VictoriaLogs или в UI VictoriaLogs.

### Goldpinger

[Goldpinger](https://github.com/bloomberg/goldpinger) — DaemonSet на каждой ноде, проверяет ICMP/TCP-связность между подами и отдаёт UI и метрики Prometheus. [Helm-чарт](https://github.com/bloomberg/goldpinger/tree/master/charts/goldpinger) публикуется в репозитории Bloomberg.

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

Скрипт `[alerts/generate_alerts.py](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py)` генерирует YAML-файлы `VMRule` в директорию `alerts/vmrules/`. По умолчанию создаётся **500** файлов; каждый `VMRule` содержит **4–6 групп** (с `interval` 30s/1m/2m) и **100 алертов** суммарно.

Исходный код файла [alerts/generate_alerts.py](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py).

```bash
cd alerts
./generate_alerts.py
```

Правила «псевдо-реалистичные»: разные шаблоны (k8s/node/http/db/…), `expr` построены на `vector(...)`, `severity` задаётся шаблоном (в основном `warning`/`critical`), `for` — от `0s` до `1h`. Генерация детерминирована (seed=42); объём можно изменить в `main()` через `num_vmrules` и `alerts_per_vmrule`. Скрипт перезаписывает только файлы `vmrule-00001.yaml` … `vmrule-NNNNN.yaml` в пределах `num_vmrules`; если раньше было больше файлов — удалите лишние в `alerts/vmrules/` и при необходимости снимите лишние `VMRule` из кластера (`kubectl delete`).

### Применение VMRule в Kubernetes

Список файлов в `alerts/vmrules/` строится как `find … | sort -V` — **глобальный порядок** (индексы 1…N в таблице ниже — позиция в этом списке).

**Без сохранения прогресса:** каждый запуск скрипта батча снова проходит **весь** его диапазон с первого файла. Если была ошибка — остановились, исправили, запустили тот же скрипт ещё раз (`kubectl apply` идемпотентен).

**Темп:** между apply паузы так, чтобы суммарный sleep на батч был ≈ `STAGE_DURATION_SEC` (по умолчанию 9 ч). **Время полного прогона** = эти паузы плюс время всех `kubectl apply` (для батча 01: ~~9 ч пауз + сотни вызовов API — ориентировочно **~~9–12 ч** суммарно, точнее зависит от кластера).


| Батч | Скрипт                   | Глобальные индексы (1…N) |
| ---- | ------------------------ | ------------------------ |
| 01   | `apply-yaml-batch-01.sh` | 1–500                    |


Расчёт на **500** файлов из `generate_alerts.py`; при другом `num_vmrules` поправьте `APPLY_INDEX_END` в `apply-yaml-batch-01.sh` или добавьте батчи по образцу (в скрипте задаются `APPLY_BATCH_ID`, `APPLY_INDEX_START`, `APPLY_INDEX_END`).

Общая логика: `[alerts/apply-yaml-lib.sh](alerts/apply-yaml-lib.sh)`. Быстро применить всё подряд: `[alerts/apply-yaml.sh](alerts/apply-yaml.sh)` (интервал 5 с).

#### Создание токена Grafana

Чтобы скрипты батчей могли писать аннотации в Grafana, нужен API-токен с правами редактора.

**Ручное создание токена:**

1. Откройте Grafana в браузере (например, [grafana.apatsev.org.ru](http://grafana.apatsev.org.ru)).
2. Войдите под учётной записью с правами администратора (логин `admin`, пароль — из секрета `vmks-grafana`, см. раздел [victoria-metrics-k8s-stack](#victoria-metrics-k8s-stack)).
3. В левом меню: **Administration** (иконка шестерёнки) → **Service accounts**.
4. Нажмите **Add service account**, задайте имя (например, `apply-yaml-annotations`), роль **Editor** → **Create**. Откройте созданный аккаунт → вкладка **Tokens** → **Add service account token**, имя токена (например, `annotations`) → **Generate token**. Скопируйте токен — он показывается один раз.
5. Экспортируйте переменные перед запуском (подставьте свой URL и токен):

```bash
export GRAFANA_URL="http://grafana.apatsev.org.ru"
export GRAFANA_TOKEN="glsa_xxxxxxxx"
```

Затем запустите скрипты по порядку (см. блок команд ниже).

Если `GRAFANA_TOKEN` не задан, скрипт запросит его в терминале; без токена аннотации не создаются.

**Как включить аннотации на дашборде (новая Grafana):**

Аннотации создаются скриптами через API и хранятся во встроенном хранилище Grafana (не в VictoriaMetrics). Чтобы они отображались на графиках:

1. Откройте нужный дашборд (например, **VictoriaMetrics - vmalert**).
2. Вверху справа нажмите **Dashboard settings** (иконка шестерёнки).
3. В левой колонке выберите **Annotations** → **New annotation**.
4. Заполните:
  - **Name:** например, `apply-yaml-annotations`.
  - **Data source:** выберите **Grafana** (встроенные аннотации). Не выбирайте VictoriaMetrics — наши аннотации создаются через API и лежат в Grafana.
  - **Filter by tags:** при необходимости укажите тег `apply-yaml-batch01`. Можно оставить пустым — тогда показываются все аннотации с дашборда.
  - **Enabled** — включено (галочка).
  - **Color** — цвет маркеров (например, красный).
  - **Show in** — **All panels**, чтобы аннотации были видны на всех панелях.
5. Нажмите **Back to list**, затем **Save dashboard** (синяя кнопка вверху справа).

После этого на графиках появятся вертикальные маркеры в моменты старта и окончания батча (тег `apply-yaml-batch01`).

**Запуск (из каталога `alerts`):**

```bash
cd alerts
./apply-yaml-batch-01.sh
```

Один полный прогон батча 01 при настройках по умолчанию занимает **примерно 9–12 ч** (≈9 ч на паузы между apply плюс время 500 применений; при медленном API может быть дольше).

Исходный код: [alerts/apply-yaml-lib.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/apply-yaml-lib.sh), [alerts/apply-yaml-batch-01.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/apply-yaml-batch-01.sh).

Скрипты батча при старте и по завершении создают **аннотации в Grafana** (начало/конец батча). Для этого задайте переменные окружения (см. [Создание токена Grafana](#создание-токена-grafana)).

**Мониторинг ошибок:** в отдельном терминале запустите `./monitor-batch.sh` — при первой проблеме скрипт **печатает в stdout** текст (строки логов из VictoriaLogs с префиксом namespace/pod/container или ненулевые метрики). Момент и детали пишутся в `alerts/first-error-batch.txt` (`RESULT_FILE=...`). Проверяются: OOM vmalert; **LogsQL** по широкому OR (`i(error)`, fatal, panic, HTTP 5xx/422, timeout, OOM в тексте и т.д., см. `VL_LOGSQL_QUERY` в скрипте); метрики `vmalert_execution_errors_total`, `vm_concurrent_select_limit_reached_total`, суммарный rate **5xx** по `vmselect|vmstorage|vminsert`. Переменные: `INTERVAL` (30 с), `VL_LOG_LIMIT`, `VL_WINDOW_MIN`, `VL_LOGSQL_QUERY`; для самоподписанных сертификатов: `CURL_OPTS="-k"`.

Исходный код: [alerts/monitor-batch.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/monitor-batch.sh).

После батча можно анализировать графики нагрузки на vmalert, vmselect, operator.

## Текущее состояние (снимок на 2026-03-21)

> **Примечание:** тест продолжается. Ниже — актуальный срез, собранный командами `kubectl`, запросами к `vmselect` и проверкой через MCP.

### Общие цифры


| Метрика                                  | Значение       |
| ---------------------------------------- | -------------- |
| VMRule в кластере (всего)                | **389**        |
| VMRule в namespace `vmks`                | **39**         |
| Целевое количество алертов               | **~1 000 000** |
| Активные алерты (ALERTS)                 | **42 214**     |
| ALERTS_FOR_STATE                         | **34 996**     |
| sum(vmalert_alerts_firing)               | **0**          |
| Временные ряды (totalSeries)             | **12 196 362** |
| ConfigMap'ов с правилами (`rulefiles-`*) | **28**         |
| ReplicaSet'ов vmalert                    | **11**         |
| sum(vmalert_execution_errors_total)      | **10**         |
| sum(vmalert_iteration_missed_total)      | **0**          |
| max(vmalert_iteration_duration_seconds)  | **5.24 сек**   |


### Распределение правил по ConfigMap'ам

Суммарный размер `rulefiles-`*: **14 177 327 bytes (~13.52 MiB)**, средний размер ConfigMap: **~506 KB**.  
Operator по-прежнему упаковывает большинство ConfigMap около ~505–511 KB и создаёт новый по мере роста числа `VMRule`.

### Перезапуски vmalert и восстановление состояния

`vmalert` остаётся в ожидаемой модели: при появлении нового ConfigMap возможен rolling restart (из-за новых `volume`/`volumeMount`), а восстановление состояния идёт через `remoteRead` по `ALERTS_FOR_STATE`.

Это согласуется с документацией VictoriaMetrics (`vmalert`: state restore выполняется один раз при старте процесса; hot reload не триггерит restore).

### Потребление ресурсов (срез `kubectl top pods -n vmks`)

Ключевые потребители на текущем этапе:

- `vmalert` (каждая реплика): **~1250–1464m CPU**, **~867–1020Mi RAM**
- `vmstorage` (каждая реплика): **~210–236m CPU**, **~2.6–2.8Gi RAM**
- `vmselect` (каждая реплика): **~251–392m CPU**, **~265–283Mi RAM**
- `victoria-metrics-operator`: **~96m CPU / ~197Mi RAM**

### Проверка через MCP (VictoriaLogs / VictoriaMetrics)

- `user-victorialogs` MCP в рабочем состоянии: запросы `LogsQL` выполняются, видны логи `vmks`.
- Через MCP VictoriaLogs подтверждены периодические `422` для запроса `sum(increase(vmalert_alerting_rules_errors_total[5m])) without(id) > 0` с причиной: `matching timeseries exceeds 1000000` (лимит `search.maxUniqueTimeseries`).
- `user-victoriametrics` MCP сейчас указывает на `http://vmsingle.apatsev.org.ru/...` и в этом стенде недоступен (`no such host`), так как используется `vmcluster` + `vmselect`.
- Для метрик в текущем состоянии используем прямые запросы к ingress `https://vmselect.apatsev.org.ru/...` (см. команды ниже).

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

- `**-remoteWrite.url`** — при каждой оценке vmalert записывает ряды `ALERTS` и `ALERTS_FOR_STATE` в VMCluster (через vminsert);
- `**-remoteRead.url**` — при **старте** процесса vmalert восстанавливает состояние, запрашивая ряды `ALERTS_FOR_STATE` (через vmselect).

В нашем стенде:

```
-remoteWrite.url=http://vminsert-vmks-victoria-metrics-k8s-stack:8480/insert/0/prometheus/api/v1/write
-remoteRead.url=http://vmselect-vmks-victoria-metrics-k8s-stack:8481/select/0/prometheus
```

`**ALERTS_FOR_STATE**` содержит полную информацию о состоянии каждого алерта (`ActiveAt`, `for` duration и т.д.), необходимую для восстановления после рестарта. При запуске vmalert однократно читает этот ряд для восстановления.

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

### Пороги масштабируемости (заполнять по мере нагрузки)

Таблица для фиксации наблюдений на разных этапах теста (кол-во VMRule, алертов, ресурсы). Нагрузка только начата — значения ниже будут обновляться.


| Момент / этап          | VMRule (всего) | Активные алерты (ALERTS) | ConfigMap'ов | CPU vmalert (реплика) | Memory vmalert (реплика) | CPU Operator | CPU kube-apiserver | Примечание                                                                 |
| ---------------------- | -------------- | ------------------------ | ------------ | --------------------- | ------------------------ | ------------ | ------------------ | -------------------------------------------------------------------------- |
| Начало (baseline)      | *—*            | *—*                      | *—*          | *—*                   | *—*                      | *—*          | *—*                | До массового apply                                                         |
| Снимок ~13 500 алертов | **161**        | **~13 500**              | **10**       | **~409m**             | **~373Mi**               | **51m**      | *—*                | 39 системных + 122 нагрузочных; ошибок 0, missed 0                         |
| Снимок ~17 000 алертов | **203**        | **~17 000**              | **13**       | **~545m**             | **~508Mi**               | **52m**      | *—*                | 39 системных + 164 нагрузочных; ошибок 0, missed 0                         |
| Снимок ~23 000 алертов | **252**        | **~22 973**              | **17**       | **~723m**             | **~676Mi**               | **52m**      | *—*                | 39 системных + 213 нагрузочных; ошибок 0, missed 0, max iteration 3.84 сек |
| Снимок ~42 000 алертов | **389**        | **~42 214**              | **28**       | **~1250–1464m**       | **~867–1020Mi**          | **96m**      | **~94m**           | API Server из dashboard `Kubernetes / System / API Server`; `apiserver` RPS ~13.1, p99 ~60s |


Используемые ресурсы (пример для одного среза): `kubectl top pods -n vmks`; метрики — `count(ALERTS)`, `count(ALERTS_FOR_STATE)`, `vmalert_alerts_firing`, размер ConfigMap'ов (см. [Полезные команды](#полезные-команды-для-мониторинга)).

Данные трёх точек (~13 500, ~17 000 и ~23 000 алертов) подтверждают линейный рост: CPU vmalert вырос с ~409m → ~545m → ~723m, память — с ~373Mi → ~508Mi → ~676Mi. Прирост пропорционален числу алертов; max iteration duration выросла с 1.57 до 3.84 сек.

### Наблюдаемые данные при ~23 000 активных алертов


| Метрика                                 | Значение                            |
| --------------------------------------- | ----------------------------------- |
| CPU vmalert (каждая реплика)            | ~723m (средн.)                      |
| Memory vmalert (каждая реплика)         | ~676Mi                              |
| CPU VM Operator                         | 52m                                 |
| CPU vmstorage (каждая реплика)          | ~159m                               |
| Memory vmstorage (каждая реплика)       | ~1 638Mi                            |
| ConfigMap'ов                            | 17 × ~505–509 KB (последний 346 KB) |
| Временные ряды                          | ~4 793 000                          |
| max(vmalert_iteration_duration_seconds) | 3.84 сек                            |
| vmalert_execution_errors_total          | 0                                   |
| vmalert_iteration_missed_total          | 0                                   |


**Ресурсы нод кластера:**


| Нода       | CPU   | CPU% | Memory | Memory% |
| ---------- | ----- | ---- | ------ | ------- |
| cl1...iror | 1545m | 19%  | 2394Mi | 11%     |
| cl1...otib | 773m  | 9%   | 3426Mi | 16%     |
| cl1...yruq | 1236m | 15%  | 4414Mi | 21%     |


Кластер загружен на 11–21% по памяти и 9–19% по CPU — запас для дальнейшего роста нагрузки есть, но загрузка растёт.

### Экстраполяция


| Метрика                  | ~23 000 алертов (факт) | ~84 000 алертов | ~420 000 алертов | ~840 000 алертов |
| ------------------------ | ---------------------- | --------------- | ---------------- | ---------------- |
| VMRule                   | ~252                   | ~920            | ~4 600           | ~9 200           |
| CPU vmalert (реплика)    | ~723m                  | ~2.6            | ~13.2            | ~26.4            |
| Memory vmalert (реплика) | ~676Mi                 | ~2.5Gi          | ~12.3Gi          | ~24.7Gi          |
| ConfigMap'ов             | 17                     | ~62             | ~310             | ~621             |
| Временные ряды           | 4.79M                  | ~17.5M          | ~87.6M           | ~175.2M          |


Вывод: при линейной экстраполяции **~840 000 алертов потребуют серьёзного шардирования vmalert** — один инстанс не справится ни по CPU, ни по памяти. Уже при ~84 000 алертов CPU vmalert превысит типичный лимит в 2–4 CPU. `max(vmalert_iteration_duration_seconds)` при ~23 000 алертов уже достигает 3.84 сек — при дальнейшем росте итерация может не укладываться в `interval`, что приведёт к росту `vmalert_iteration_missed_total`. Также потребуется масштабирование VMCluster (vmselect/vmstorage) из-за роста числа временных рядов.

### Рекомендации по масштабированию

Ориентиры «при каком количестве алертов выставлять те или иные ресурсы» — в начале [vmks-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml) (блок **Шкала нагрузки**).

1. **До ~170 000 алертов:** увеличить CPU limit vmalert до 2–4 CPU
2. **~170 000–420 000 алертов:** шардирование vmalert (несколько инстансов с разделением правил через `-rule.partition` или разные `ruleSelector`)
3. **420 000+ алертов:** полное шардирование vmalert + масштабирование VMCluster + выделенный Alertmanager cluster + оптимизация reconcile Operator'а

## Рекомендации по повышению устойчивости

### Краткосрочные

- Отслеживать рост CPU vmalert при продолжении теста (при ~23 000 алертов — ~723m, запас до лимита 4 CPU есть)
- Мониторинг `vmalert_iteration_missed_total` и `vmalert_iteration_duration_seconds` (текущий max 3.84 сек — приближается к `interval`)
- Контролировать память vmstorage (~1.6 Gi на реплику при ~23 000 алертов)

### Среднесрочные

- Шардирование vmalert при росте выше ~170 000 алертов
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

### Потребление ресурсов

```bash
kubectl top pods -n vmks
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


# Метрики, выросшие при нагрузке (VictoriaMetrics stack)

Оценки даны для роста от малой нагрузки до **~23 000 алертов** (~252 VMRule, ~4,79 млн рядов). Базовый URL запросов: `http://vmselect-vmks-victoria-metrics-k8s-stack:8481/select/0/prometheus`.


## 1. VMAlert (vmalert)

- **vmalert_iteration_duration_seconds** — выросла в **десятки раз** (оценка всех групп за одну итерацию занимает существенную долю interval).
- **vmalert_iteration_missed_total** — при перегрузке растёт; при ~15 000 алертов может оставаться 0, при дальнейшем росте — рост в **разы**.
- **vmalert_execution_errors_total** — при сбоях vmselect/vminsert рост от 0 до **единиц–десятков** в час.
- **vmalert_alerts_firing** / **vmalert_alerts_pending** — растут **пропорционально числу правил** (~23 000 ALERTS, sum(vmalert_alerts_firing) ~36 883 по 2 репликам).
- **vmalert_remotewrite_requests_total** — рост примерно **в 2–3 раза** от числа групп (запись ALERTS и ALERTS_FOR_STATE при каждой итерации).
- **vmalert_remoteread_requests_total** — скачок при каждом рестарте vmalert (один большой запрос при старте).
- **container_cpu_usage_seconds_total** (vmalert) — при ~~23 000 алертов **~~723m** (в среднем на реплику); относительно пустого старта рост в **несколько раз**, запас до лимита 4 CPU.
- **container_memory_working_set_bytes** (vmalert) — **~2.5–3 раза** (с ~200–300 Mi до ~676 Mi).


## 2. VMSelect

- **vm_concurrent_select_current** — среднее значение выросло в **2–5 раз** (много запросов от vmalert на eval и от remoteRead при рестартах).
- **vm_concurrent_select_limit_reached_total** — при приближении к лимиту рост от 0 до **единиц–сотен** в час.
- **vm_concurrent_select_limit_timeout_total** — при перегрузке рост от 0; в норме 0.
- **vm_select_request_duration_seconds** — p99 вырос в **2–4 раза** (тяжёлые запросы по ALERTS_FOR_STATE и правилам).
- **vm_http_requests_total** (job=vmselect) — запросы к select выросли **пропорционально числу групп и интервалам** (в **десятки раз**).


## 3. VMStorage

- **vm_rows** / **vm_rows_inserted_total** — рост **пропорционально числу рядов** (~4,79 млн при ~23 000 алертов; от нуля — в **тысячи раз**).
- **vm_storage_blocks** — рост в **разы** с ростом объёма данных.
- **vm_cache_*_requests_total** / **vm_cache_*_misses_total** — объём запросов вырос в **разы**; miss rate может вырасти в **1,5–2 раза** при нехватке кэша.
- **vm_http_requests_total** (job=vmstorage) — запросы от vmselect выросли в **десятки раз**.


## 4. VMInsert

- **vm_http_requests_total** (job=vminsert, path insert) — рост в **2–3 раза** от числа групп vmalert (remote write при каждой итерации).
- **vm_insert_request_duration_seconds** — при росте объёма записи p99 может вырасти в **1,5–3 раза**.
- **vm_insert_requests_total** — рост **пропорционально** записи (в **десятки раз** относительно малой нагрузки).


## 5. VMAgent

- **scrape_series_added** (target=vmalert) — выросло в **десятки раз** (размер /metrics vmalert растёт с числом правил и алертов).
- **scrape_body_size_bytes** (target=vmalert) — рост в **10–20+ раз** (при ~23 000 алертов уже сотни KB; при ~50 000+ алертов может превысить maxScrapeSize 16 MB).
- **scrape_samples_scraped** (job=vmalert) — рост **пропорционально** числу метрик vmalert (в **десятки раз**).


## 6. Victoria Metrics Operator

- **process_cpu_seconds_total** (job=operator) — при ~~23 000 алертов **~~52m**; относительно малой нагрузки рост в **2–5 раз** (reconcile по всем правилам и сборке ConfigMap).
- **process_resident_memory_bytes** (job=operator) — **~173Mi** при ~23 000 алертов; рост в **1.5–2 раза** с ростом числа алертов.


## 7. Kubernetes / ресурсы подов

- **container_cpu_usage_seconds_total** (vmalert) — см. раздел 1; **container_cpu** для vmselect, vmstorage, vminsert — рост в **2–4 раза** при той же нагрузке.
- **container_memory_working_set_bytes** (vmstorage) — при ~~23 000 алертов **~~1 613–1 662 Mi** на реплику; рост в **3–5 раз** от старта.
- **container_memory_working_set_bytes** (vmselect) — **~221–257Mi**; рост в **1,5–3 раза**.


## 8. Алерты и объём данных

- **count(ALERTS)** — при ~~23 000 алертов **~~22 973**; рост от 0 до этого значения (фактически **на порядки**).
- **count(ALERTS_FOR_STATE)** — **~21 298**; того же порядка, что и ALERTS; рост **пропорционально** числу алертов.
- **totalSeries** (через API/tsdb) — при ~~23 000 алертов **~~4,79 млн** рядов; рост от нуля в **тысячи раз**.


## Как считать прирост

- Для счётчиков: `increase(metric_name[1h])` или сравнение с периодом до нагрузки.
- Для gauge (CPU, память, длительность): сравнение средних/перцентилей «до» и «после» по тому же окну.
- Список имён метрик: `GET /api/v1/label/__name__/values`, затем фильтр по префиксу (`vmalert_`*, `vm_concurrent_select_*` и т.д.).
- Ресурсы подов: `kubectl top pods -n vmks`.


## Заключение и выводы

Проведённое нагрузочное тестирование VictoriaMetrics stack большим количеством VMRule подтвердило заявленные цели и позволило сформулировать практические выводы.

### Достигнутые результаты

- **Распределение правил по ConfigMap'ам:** Operator стабильно дробит правила при приближении к лимиту ~~1 MiB: каждый ConfigMap заполняется до ~505–509 KB, затем создаётся следующий. При ~23 000 алертов наблюдается 17 ConfigMap'ов (~~8,5 MB суммарно). Механизм предсказуем и масштабируется линейно.
- **Перезапуски vmalert:** Каждое появление нового ConfigMap приводит к пересозданию Pod'а vmalert (из-за добавления volume/volumeMount). Интервал между рестартами составил ~13–15 мин. За время теста зафиксировано 15 ReplicaSet'ов (14 пересозданий). Горячая перезагрузка (SIGHUP) применяется только при обновлении существующих ConfigMap'ов без добавления новых.
- **Сохранение состояния:** Механизм remoteWrite/remoteRead (`ALERTS`, `ALERTS_FOR_STATE`) работает корректно: после рестарта vmalert восстанавливает состояние алертов из VictoriaMetrics, счётчики `for` не сбрасываются, потери алертов не происходит. `vmalert_execution_errors_total = 0` и `vmalert_iteration_missed_total = 0` подтверждают стабильную работу. Временное падение `sum(ALERTS)` во время рестарта — следствие задержки первой итерации, а не потери данных.
- **Пороги масштабируемости:** При ~~23 000 алертов (~~4,79 млн рядов) vmalert потребляет ~723m CPU и ~676Mi памяти на реплику — нагрузка заметная, но в пределах лимитов (4 CPU / 4 Gi). VM Operator потребляет ~52m CPU. `max(vmalert_iteration_duration_seconds)` достигла 3.84 сек — важный индикатор приближения к пределу при интервалах 30s. Линейная экстраполяция на ~840 000 алертов указывает на необходимость шардирования vmalert и масштабирования VMCluster.

### Основные выводы

1. **Операционная модель:** При массовом добавлении VMRule следует учитывать периодические рестарты vmalert (по одному на каждый новый ConfigMap). Для production целесообразно применять правила батчами или через GitOps с контролируемым темпом, чтобы не создавать лишние ConfigMap'ы подряд и снизить частоту рестартов.
2. **Ресурсы:** При ~23 000 алертов CPU vmalert ~723m — запас до лимита 4 CPU есть. До ~170 000 алертов достаточно увеличить CPU limit. При росте выше ~170 000–420 000 алертов необходимо шардирование vmalert и масштабирование VMCluster. vmstorage потребляет ~1.6 Gi RAM на реплику — следует учитывать при планировании ресурсов нод.
3. **Отказоустойчивость:** Конфигурация с двумя репликами vmalert, remoteRead/remoteWrite и Alertmanager в кластерном режиме обеспечивает восстановление без потери состояния алертов и без дублирования уведомлений. RTO vmalert — в пределах минуты. На текущем этапе `vmalert_execution_errors_total = 0` и `vmalert_iteration_missed_total = 0`.
4. **Мониторинг:** Ключевые метрики для раннего обнаружения перегрузки — `vmalert_iteration_duration_seconds` (текущий max ~3.84 сек — рост в 2.4 раза по сравнению со снимком ~17 000 алертов), `vmalert_iteration_missed_total`, `container_cpu_usage_seconds_total` (vmalert), а также размер и количество ConfigMap'ов с правилами.

Итог: VictoriaMetrics stack при правильной настройке (remoteRead/remoteWrite, HA vmalert и Alertmanager) выдерживает нагрузку тысячами правил и алертов с сохранением целостности состояния. Ограничения носят в основном ресурсный характер и снимаются шардированием и увеличением ресурсов в соответствии с приведёнными рекомендациями.