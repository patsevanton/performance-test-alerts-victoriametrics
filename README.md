# Нагрузочное тестирование VictoriaMetrics большим количеством алертов

## Цель

Исследовать поведение VictoriaMetrics stack при нагрузке большим количеством правил оповещений (`VMRule`) в Kubernetes. В частности:

- понять, как VictoriaMetrics Operator распределяет правила по ConfigMap'ам при превышении лимита ~1 MiB;
- выяснить, при каких условиях происходит пересоздание Pod'а `vmalert` и как это влияет на состояние алертов;
- проверить механизм сохранения и восстановления состояния алертов через `remoteWrite`/`remoteRead`;
- определить практические пороги масштабируемости и дать рекомендации по эксплуатации.

## Архитектура и Стенд

### Infrastructure

**Кластер:** 3 ноды Kubernetes v1.32.1 на Yandex Cloud (Ubuntu 22.04.5 LTS, containerd 1.7.27).

### VictoriaMetrics Stack

**Версия:** `victoria-metrics-k8s-stack` v0.71.1 (VictoriaMetrics v1.136.0), namespace `vmks`.

#### Компоненты

| Компонент | Deployment | Реплики | Роль |
|-----------|------------|---------|------|
| **VMCluster** | vmstorage / vmselect / vminsert | 2 / 2 / 2 | Хранение метрик (replicationFactor=2) |
| **VMAlert** | vmalert | 2 | Оценка правил и отправка алертов |
| **VMAgent** | vmagent | 1 | Сбор метрик (scrape) |
| **VMAlertmanager** | vmalertmanager | 2 | Маршрутизация уведомлений (кластерный режим) |
| **VM Operator** | victoria-metrics-operator | 1 | Управление CRD-ресурсами |
| **Grafana** | grafana | 1 | Визуализация |

Ключевые настройки HA (файл [vmks-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml)):
- `vmsingle` **отключен**, вместо него — `vmcluster` с `replicationFactor: 2`
- vmalert: 2 реплики, CPU limit 1, memory 2Gi, PDB `minAvailable: 1`
- alertmanager: 2 реплики, PDB `minAvailable: 1`

## Установка

### victoria-metrics-k8s-stack

```bash
helm repo add vm https://victoriametrics.github.io/helm-charts/
helm repo update

helm upgrade --install vmks vm/victoria-metrics-k8s-stack \
  --namespace vmks --create-namespace \
  --version 0.71.1 \
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
  --wait \
  --timeout 15m \
  -f victoria-logs-collector-values.yaml
```

Исходный код файла [victoria-logs-collector-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/victoria-logs-collector-values.yaml)

Проверка: `kubectl get pods -n victoria-logs-collector`. Логи стека (vmks, vmalert, vmagent и др.) можно запрашивать в Grafana через datasource VictoriaLogs или в UI VictoriaLogs.

### Chaos Mesh

[Chaos Mesh](https://chaos-mesh.org/) — платформа для chaos engineering в Kubernetes. Можно использовать для проверки отказоустойчивости стенда (pod-kill, network partition, stress и т.д.).

**Установка:**

```bash
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo update

helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh \
  --create-namespace \
  -f chaos-mesh/chaos-mesh-values.yaml \
  --version 2.8.1 \
  --wait
```

Исходный код файла [chaos-mesh/chaos-mesh-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chaos-mesh/chaos-mesh-values.yaml).

Проверка: `kubectl get pods -n chaos-mesh`.

**Сбор метрик Chaos Mesh в VictoriaMetrics K8s Stack:**

```bash
kubectl apply -f chaos-mesh/chaos-mesh-vmservicescrape.yaml
```

Исходный код файла [chaos-mesh/chaos-mesh-vmservicescrape.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chaos-mesh/chaos-mesh-vmservicescrape.yaml).

**Доступ к Dashboard (RBAC + токен):**

```bash
kubectl apply -f chaos-mesh/chaos-mesh-rbac.yaml
sleep 3
kubectl get secret chaos-mesh-admin-token -n chaos-mesh -o jsonpath='{.data.token}' | base64 -d; echo
```

Исходный код файла [chaos-mesh/chaos-mesh-rbac.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chaos-mesh/chaos-mesh-rbac.yaml).

Токен вставить в Chaos Mesh Dashboard. Ingress: `chaos-dashboard.apatsev.org.ru` (из [chaos-mesh/chaos-mesh-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/chaos-mesh/chaos-mesh-values.yaml)).

### Генерация нагрузочных VMRule

Скрипт [`alerts/generate_alerts.py`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py) генерирует YAML-файлы `VMRule` в директорию `alerts/vmrules/`. По умолчанию создаётся **10 000** файлов; каждый `VMRule` содержит **4–6 групп** (с `interval` 30s/1m/2m) и **20 алертов** суммарно.

Исходный код файла [alerts/generate_alerts.py](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py).

```bash
cd alerts
./generate_alerts.py
```

Правила «псевдо-реалистичные»: разные шаблоны (k8s/node/http/db/…), `expr` построены на `vector(...)`, `severity` задаётся шаблоном (в основном `warning`/`critical`), `for` — от `0s` до `1h`. Генерация детерминирована (seed=42); объём можно изменить в `main()` через `num_vmrules` и `alerts_per_vmrule`.

### Применение VMRule в Kubernetes

Применение разбито на **три этапа** с разным темпом (и возможностью снимать графики нагрузки между этапами). Используйте три скрипта по порядку:

| Этап | Скрипт | Диапазон файлов | Интервал |
|------|--------|-----------------|----------|
| Начальный | `apply-yaml-stage1.sh` | 1–400 | 5 с |
| Средний | `apply-yaml-stage2.sh` | 401–1222 | 30 с |
| Завершающий | `apply-yaml-stage3.sh` | 1223–10000 | 90 с |

#### Создание токена Grafana

Чтобы скрипты этапов могли писать аннотации в Grafana, нужен API-токен с правами редактора.

**Ручное создание токена:**

1. Откройте Grafana в браузере (например, [grafana.apatsev.org.ru](http://grafana.apatsev.org.ru)).
2. Войдите под учётной записью с правами администратора (логин `admin`, пароль — из секрета `vmks-grafana`, см. раздел [victoria-metrics-k8s-stack](#victoria-metrics-k8s-stack)).
3. В левом меню: **Administration** (иконка шестерёнки) → **Service accounts**.
4. Нажмите **Add service account**, задайте имя (например, `apply-yaml-annotations`), роль **Editor** → **Create**. Откройте созданный аккаунт → вкладка **Tokens** → **Add service account token**, имя токена (например, `annotations`) → **Generate token**. Скопируйте токен — он показывается один раз.
5. Экспортируйте переменные перед запуском скриптов этапов (подставьте свой URL и токен):

```bash
export GRAFANA_URL="http://grafana.apatsev.org.ru"
export GRAFANA_TOKEN="glsa_xxxxxxxx"
```

Затем запустите скрипты по порядку (см. блок команд ниже).

Если `GRAFANA_URL` или `GRAFANA_TOKEN` не заданы, скрипты работают как раньше, но аннотации не создаются.

**Как включить аннотации на дашборде (новая Grafana):**

Аннотации создаются скриптами через API и хранятся во встроенном хранилище Grafana (не в VictoriaMetrics). Чтобы они отображались на графиках:

1. Откройте нужный дашборд (например, **VictoriaMetrics - vmalert**).
2. Вверху справа нажмите **Dashboard settings** (иконка шестерёнки).
3. В левой колонке выберите **Annotations** → **New annotation**.
4. Заполните:
   - **Name:** например, `apply-yaml-annotations`.
   - **Data source:** выберите **Grafana** (встроенные аннотации). Не выбирайте VictoriaMetrics — наши аннотации создаются через API и лежат в Grafana.
   - **Filter by tags:** при необходимости укажите теги через запятую: `apply-yaml-stage1`, `apply-yaml-stage2`, `apply-yaml-stage3`. Можно оставить пустым — тогда показываются все аннотации с дашборда.
   - **Enabled** — включено (галочка).
   - **Color** — цвет маркеров (например, красный).
   - **Show in** — **All panels**, чтобы аннотации были видны на всех панелях.
5. Нажмите **Back to list**, затем **Save dashboard** (синяя кнопка вверху справа).

После этого на графиках появятся вертикальные маркеры в моменты старта и окончания каждого этапа (теги `apply-yaml-stage1`, `apply-yaml-stage2`, `apply-yaml-stage3`).

**Запуск (из каталога `alerts`):**

```bash
cd alerts
./apply-yaml-stage1.sh
# после этапа 1 — анализ, при необходимости мониторинг ошибок
./apply-yaml-stage2.sh
# после этапа 2 — анализ
./apply-yaml-stage3.sh
```

Исходный код файлов: [alerts/apply-yaml-stage1.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/apply-yaml-stage1.sh), [alerts/apply-yaml-stage2.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/apply-yaml-stage2.sh), [alerts/apply-yaml-stage3.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/apply-yaml-stage3.sh).

Скрипты при старте и по завершении создают **аннотации в Grafana** (время начала и окончания этапа), чтобы на графиках было видно, когда какой этап выполнялся. Для этого задайте переменные окружения (см. [Создание токена Grafana](#создание-токена-grafana)).

**Мониторинг появления ошибок во время этапа 1:** в отдельном терминале запустите `./monitor-stage1.sh`. Скрипт каждые 30 с опрашивает VictoriaLogs (victorialogs.apatsev.org.ru) и vmselect (vmselect.apatsev.org.ru) — логи с «error» и метрики `vmalert_execution_errors_total`, `vm_concurrent_select_limit_reached_total`, 5xx на vmselect. При первом обнаружении ошибок момент записывается в `alerts/first-error-stage1.txt`. Интервал задаётся переменной `INTERVAL` (по умолчанию 30), для самоподписанных сертификатов: `CURL_OPTS="-k"`.

Исходный код файла [alerts/monitor-stage1.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/monitor-stage1.sh).

**Этап 2:** аналогично запустите `./monitor-stage2.sh` — момент первой ошибки записывается в `alerts/first-error-stage2.txt`.

Исходный код файла [alerts/monitor-stage2.sh](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/monitor-stage2.sh).

После каждого этапа можно анализировать графики нагрузки на vmalert, vmselect, operator.

## Текущее состояние (тест в процессе)

> **Примечание:** тест ещё выполняется — скрипты этапов продолжают применять VMRule. Данные ниже — снимок на момент ~660 применённых VMRule.

### Общие цифры

| Метрика | Значение |
|---------|----------|
| VMRule в кластере (всего) | **~660** (39 системных + ~620 нагрузочных) |
| Целевое количество VMRule | 10 000 нагрузочных |
| Активные алерты (ALERTS) | **~15 600** |
| Временные ряды (totalSeries) | **~2 100 000** |
| ConfigMap'ов с правилами | **12** |
| Перезапусков vmalert (ReplicaSet'ов) | **11** (10 пересозданий) |

### Распределение правил по ConfigMap'ам

```
vm-vmks-victoria-metrics-k8s-stack-rulefiles-0    509.68 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-1    509.87 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-2    509.48 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-3    511.17 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-4    503.78 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-5    508.20 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-6    510.25 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-7    507.15 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-8    503.29 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-9    511.71 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-10   510.66 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-11   205.20 KB
```

Суммарный размер: **~5.8 MB**. Operator заполняет каждый ConfigMap до ~510 KB, затем создаёт следующий. Последний (`rulefiles-11`) ещё не полон — тест продолжается.

### Перезапуски vmalert

Метрика `sum(ALERTS)` может **временно падать** во время перезапуска pod'ов vmalert: пока поды не выполнят первую итерацию и remote write, в VictoriaMetrics перестают поступать свежие ряды ALERTS, сумма падает до нуля или близко к нему, затем снова растёт. Это сбой **отображения** (нет свежих записей), а не потеря самих алертов.

**Как проверить, что алерты в целостности и сохранности:**

- **До и после рестарта** — сравнить `count(ALERTS)` или `sum(ALERTS)` через 1–2 минуты после рестарта с типичным значением до него: число должно вернуться к прежнему порядку (восстановление из `ALERTS_FOR_STATE`).
- **Состояние в vmalert** — запрос `sum(vmalert_alerts_firing)` по репликам vmalert: показывает, сколько алертов vmalert считает firing в памяти; после восстановления должно совпадать с ожидаемым.
- **Сохранённое состояние в VictoriaMetrics** — `count(ALERTS_FOR_STATE)` до рестарта: это то, что vmalert прочитает при старте; при следующем рестарте новое состояние снова запишется через remote write.
- **Корректность восстановления** — отсутствие роста `vmalert_execution_errors_total` и возврат `sum(ALERTS)` к прежнему уровню означают, что remoteRead прошёл успешно и vmalert импортировал алерты из VictoriaMetrics.

За время применения Pod'ы `vmalert` пересоздавались **10 раз** (11 ReplicaSet'ов), с интервалом **~7 мин** между пересозданиями:

```
vmalert-vmks-...-6f96546d44   2026-02-23T11:34:36Z   (начальный)
vmalert-vmks-...-7d7f4dc989   2026-02-23T11:41:09Z   (+7 мин)
vmalert-vmks-...-6bb546d486   2026-02-23T11:47:39Z   (+7 мин)
vmalert-vmks-...-8586b77975   2026-02-23T11:55:09Z   (+7 мин)
vmalert-vmks-...-6b4f6c8ff6   2026-02-23T12:02:39Z   (+7 мин)
vmalert-vmks-...-797dfb4b77   2026-02-23T12:09:02Z   (+7 мин)
vmalert-vmks-...-67fc8b8b74   2026-02-23T12:16:31Z   (+7 мин)
vmalert-vmks-...-6857d97579   2026-02-23T12:23:07Z   (+7 мин)
vmalert-vmks-...-7bd57bd9b    2026-02-23T12:30:44Z   (+8 мин)
vmalert-vmks-...-664798cd67   2026-02-23T12:37:10Z   (+6 мин)
vmalert-vmks-...-6666f56748   2026-02-23T12:44:47Z   (+8 мин, текущий)
```

Каждое пересоздание происходит при добавлении нового ConfigMap — требуется новый `volume` + `volumeMount`, что вызывает rolling restart Pod'а. Интервал ~7 мин ≈ применение ~80 VMRule по 5 сек.

### Потребление ресурсов

```
NAME                                                        CPU(cores)   MEMORY(bytes)
vmalert-...-2lf85  (реплика 1)                              920m         577Mi
vmalert-...-rlp6d  (реплика 2)                              941m         566Mi
vmagent-...                                                 34m          76Mi
vmalertmanager-...-0                                        90m          51Mi
vmalertmanager-...-1                                        95m          64Mi
vminsert-...-db2cp                                          7m           51Mi
vminsert-...-z52f8                                          16m          81Mi
vmselect-...-0                                              147m         116Mi
vmselect-...-1                                              83m          121Mi
vmstorage-...-0                                             128m         872Mi
vmstorage-...-1                                             123m         808Mi
vmks-victoria-metrics-operator-...                          198m         128Mi
vmks-grafana-...                                            9m           271Mi
vmks-kube-state-metrics-...                                 4m           21Mi
vmks-prometheus-node-exporter (×3)                          2m           9Mi
```

**vmalert** потребляет **~930m CPU из 1000m limit (93%)** и ~570Mi из 2Gi памяти (28%). CPU снова становится узким местом при дальнейшем росте нагрузки.

**VM Operator** потребляет **198m CPU** — reconcile-цикл по 660 VMRule уже заметно нагружает Operator.

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

| Ситуация | Что происходит | Даунтайм |
|----------|---------------|----------|
| Добавлена VMRule, ConfigMap'ы не переполнены | SIGHUP через config-reloader, правила перечитываются | Нет |
| Добавлена VMRule, требуется новый ConfigMap | Пересоздание Pod'а с новыми volume mounts | Есть (секунды) |

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

Подробнее: https://docs.victoriametrics.com/victoriametrics/vmalert/#alerts-state-on-restarts

## Отказоустойчивость и восстановление

### Текущая конфигурация HA

| Компонент | Реплик | PDB | Механизм HA |
|-----------|--------|-----|-------------|
| vmalert | 2 | minAvailable: 1 | Обе реплики оценивают все правила; дедупликация на стороне Alertmanager |
| VMAlertmanager | 2 | minAvailable: 1 | Кластерный режим, автодедупликация уведомлений |
| VMCluster (vmstorage) | 2 | — | replicationFactor=2, каждая точка в 2 копиях |
| VMCluster (vmselect) | 2 | — | Stateless, любая реплика обслуживает запросы |
| VMCluster (vminsert) | 2 | — | Stateless, любая реплика принимает запись |

### RTO / RPO

| Компонент | RTO | RPO | Комментарий |
|-----------|-----|-----|-------------|
| **vmalert** | ≤ 60 сек | 0 (при настроенном remoteRead) | Pod пересоздаётся K8s, состояние алертов восстанавливается из `ALERTS_FOR_STATE` |
| **VMCluster** | ≤ 5 мин | 0 при потере 1 vmstorage | replicationFactor=2 защищает от потери одной реплики |
| **Alertmanager** | ≤ 60 сек | silences/inhibitions из PVC | StatefulSet с persistent storage |
| **VM Operator** | ≤ 2 мин | 0 (stateless, читает CRD) | Reconcile восстанавливает желаемое состояние |

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

| Момент / этап | VMRule (всего) | Активные алерты (ALERTS) | ConfigMap'ов | CPU vmalert (реплика) | Memory vmalert (реплика) | CPU Operator | Примечание |
|---------------|----------------|---------------------------|--------------|------------------------|---------------------------|--------------|------------|
| Начало (baseline) | _—_ | _—_ | _—_ | _—_ | _—_ | _—_ | До массового apply |
| После stage1 (~400) | 400 | ~9 500 | 7 | ~560m | ~350Mi | ~120m | Оценочно по экстраполяции с 660 VMRule; замерить: `kubectl top pods -n vmks`, `count(ALERTS)` |
| После stage2 (~1222) | _—_ | _—_ | _—_ | _—_ | _—_ | _—_ | |
| Целевая (10k) | _—_ | _—_ | _—_ | _—_ | _—_ | _—_ | При полной нагрузке |

Используемые ресурсы (пример для одного среза): `kubectl top pods -n vmks`; метрики — `count(ALERTS)`, `count(ALERTS_FOR_STATE)`, `vmalert_alerts_firing`, размер ConfigMap'ов (см. [Полезные команды](#полезные-команды-для-мониторинга)).

### Наблюдаемые данные при ~660 VMRule / ~12 500 нагрузочных алертов

| Метрика | Значение |
|---------|----------|
| CPU vmalert (каждая реплика) | ~930m из 1000m (93%) |
| Memory vmalert (каждая реплика) | ~570Mi из 2Gi (28%) |
| CPU VM Operator | 198m |
| CPU vmstorage (каждая реплика) | ~125m |
| Memory vmstorage (каждая реплика) | ~840Mi |
| ConfigMap'ов | 12 × ~510 KB |
| Временные ряды | ~2 100 000 |

### Экстраполяция

| Метрика | ~660 VMRule | Прогноз на 5 000 | Прогноз на 10 000 |
|---------|------------|-------------------|-------------------|
| Активные алерты | ~15 600 | ~100 000 | ~200 000 |
| CPU vmalert (реплика) | ~930m | ~7 | ~14 |
| Memory vmalert (реплика) | ~570Mi | ~4.3Gi | ~8.6Gi |
| ConfigMap'ов | 12 | ~90 | ~180 |
| Временные ряды | 2.1M | ~16M | ~32M |

Вывод: при линейной экстраполяции **10 000 VMRule (200 000 алертов) потребуют серьёзного шардирования vmalert** — один инстанс не справится ни по CPU, ни по памяти. Также потребуется масштабирование VMCluster (vmselect/vmstorage) из-за роста числа временных рядов.

### Рекомендации по масштабированию

Ориентиры «при каком количестве алертов выставлять те или иные ресурсы» — в начале [vmks-values.yaml](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/vmks-values.yaml) (блок **Шкала нагрузки**).

1. **До 2 000 VMRule (~40 000 алертов):** увеличить CPU limit vmalert до 2–4 CPU
2. **2 000–5 000 VMRule:** шардирование vmalert (несколько инстансов с разделением правил через `-rule.partition` или разные `ruleSelector`)
3. **5 000+ VMRule:** полное шардирование vmalert + масштабирование VMCluster + выделенный Alertmanager cluster + оптимизация reconcile Operator'а

## Рекомендации по повышению устойчивости

### Краткосрочные

- [ ] Увеличить CPU limit vmalert (уже 93% при 660 VMRule)
- [ ] Мониторинг `vmalert_iteration_missed_total` и `vmalert_iteration_duration_seconds`

### Среднесрочные

- [ ] Шардирование vmalert при росте выше 2 000 VMRule
- [ ] Внедрить GitOps (ArgoCD/Flux) для автоматического восстановления VMRule из Git
- [ ] Добавить Network Policies для изоляции трафика между компонентами

### Долгосрочные

- [ ] Cross-region replication на уровне кластера
- [ ] Chaos Engineering: использовать [Chaos Mesh](https://github.com/patsevanton/performance-test-alerts-victoriametrics#chaos-mesh-опционально) для валидации процедур восстановления (pod-kill vmalert/vmstorage, network partition и т.д.)

## Полезные команды для мониторинга

**Топ метрик при росте нагрузки:** см. [Метрики, выросшие при нагрузке](#метрики-выросшие-при-нагрузке-victoriametrics-stack) ниже — vmalert, vmselect, vmstorage, vminsert, vmagent, Operator, ресурсы подов и примеры запросов ко всем ключевым метрикам.

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

curl -s 'http://vmselect-vmks-victoria-metrics-k8s-stack:8481/select/0/prometheus/api/v1/query?query=count(ALERTS)' \
  | jq '.data.result[0].value[1]'
```

### Длительность итераций vmalert

```bash
curl -s 'http://vmselect-vmks-victoria-metrics-k8s-stack:8481/select/0/prometheus/api/v1/query?query=max(vmalert_iteration_duration_seconds)' \
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

---

# Метрики, выросшие при нагрузке (VictoriaMetrics stack)

Оценки даны для роста от малой нагрузки до **~660 VMRule** (~15 600 алертов, ~2,1 млн рядов). Базовый URL запросов: `http://vmselect-vmks-victoria-metrics-k8s-stack:8481/select/0/prometheus`.

---

## 1. VMAlert (vmalert)

- **vmalert_iteration_duration_seconds** — выросла в **десятки раз** (оценка всех групп за одну итерацию занимает существенную долю interval).
- **vmalert_iteration_missed_total** — при перегрузке растёт; при 660 VMRule может оставаться 0, при дальнейшем росте — рост в **разы**.
- **vmalert_execution_errors_total** — при сбоях vmselect/vminsert рост от 0 до **единиц–десятков** в час.
- **vmalert_alerts_firing** / **vmalert_alerts_pending** — растут **пропорционально числу правил** (~15 600 при 660 VMRule).
- **vmalert_remotewrite_requests_total** — рост примерно **в 2–3 раза** от числа групп (запись ALERTS и ALERTS_FOR_STATE при каждой итерации).
- **vmalert_remoteread_requests_total** — скачок при каждом рестарте vmalert (один большой запрос при старте).
- **container_cpu_usage_seconds_total** (vmalert) — при 660 VMRule **~93% лимита (1 CPU)**; относительно пустого старта рост в **10+ раз**.
- **container_memory_working_set_bytes** (vmalert) — **~2–4 раза** (с ~200–300 Mi до ~570 Mi).

---

## 2. VMSelect

- **vm_concurrent_select_current** — среднее значение выросло в **2–5 раз** (много запросов от vmalert на eval и от remoteRead при рестартах).
- **vm_concurrent_select_limit_reached_total** — при приближении к лимиту рост от 0 до **единиц–сотен** в час.
- **vm_concurrent_select_limit_timeout_total** — при перегрузке рост от 0; в норме 0.
- **vm_select_request_duration_seconds** — p99 вырос в **2–4 раза** (тяжёлые запросы по ALERTS_FOR_STATE и правилам).
- **vm_http_requests_total** (job=vmselect) — запросы к select выросли **пропорционально числу групп и интервалам** (в **десятки раз**).

---

## 3. VMStorage

- **vm_rows** / **vm_rows_inserted_total** — рост **пропорционально числу рядов** (~2,1 млн при 660 VMRule; от нуля — в **тысячи раз**).
- **vm_storage_blocks** — рост в **разы** с ростом объёма данных.
- **vm_cache_*_requests_total** / **vm_cache_*_misses_total** — объём запросов вырос в **разы**; miss rate может вырасти в **1,5–2 раза** при нехватке кэша.
- **vm_http_requests_total** (job=vmstorage) — запросы от vmselect выросли в **десятки раз**.

---

## 4. VMInsert

- **vm_http_requests_total** (job=vminsert, path insert) — рост в **2–3 раза** от числа групп vmalert (remote write при каждой итерации).
- **vm_insert_request_duration_seconds** — при росте объёма записи p99 может вырасти в **1,5–3 раза**.
- **vm_insert_requests_total** — рост **пропорционально** записи (в **десятки раз** относительно малой нагрузки).

---

## 5. VMAgent

- **scrape_series_added** (target=vmalert) — выросло в **десятки раз** (размер /metrics vmalert растёт с числом правил и алертов).
- **scrape_body_size_bytes** (target=vmalert) — рост в **10–20+ раз** (при 660 VMRule уже сотни KB – единицы MB; при 1100+ VMRule может превысить maxScrapeSize 16 MB).
- **scrape_samples_scraped** (job=vmalert) — рост **пропорционально** числу метрик vmalert (в **десятки раз**).

---

## 6. Victoria Metrics Operator

- **process_cpu_seconds_total** (job=operator) — при 660 VMRule **~198m**; относительно малого числа VMRule рост в **5–15 раз** (reconcile по всем VMRule и сборке ConfigMap).
- **process_resident_memory_bytes** (job=operator) — рост в **2–4 раза** с ростом числа VMRule.

---

## 7. Kubernetes / ресурсы подов

- **container_cpu_usage_seconds_total** (vmalert) — см. раздел 1; **container_cpu** для vmselect, vmstorage, vminsert — рост в **2–4 раза** при той же нагрузке.
- **container_memory_working_set_bytes** (vmstorage) — при 660 VMRule **~800–870 Mi** на реплику; рост в **2–3 раза** от старта.
- **container_memory_working_set_bytes** (vmselect) — рост в **1,5–2 раза**.

---

## 8. Алерты и объём данных

- **count(ALERTS)** — при 660 VMRule **~15 600**; рост от 0 до этого значения (фактически **на порядки**).
- **count(ALERTS_FOR_STATE)** — того же порядка, что и ALERTS; рост **пропорционально** числу алертов.
- **totalSeries** (через API/tsdb) — при 660 VMRule **~2,1 млн** рядов; рост от нуля в **тысячи раз**.

---

## Как считать прирост

- Для счётчиков: `increase(metric_name[1h])` или сравнение с периодом до нагрузки.
- Для gauge (CPU, память, длительность): сравнение средних/перцентилей «до» и «после» по тому же окну.
- Список имён метрик: `GET /api/v1/label/__name__/values`, затем фильтр по префиксу (`vmalert_*`, `vm_concurrent_select_*` и т.д.).
- Ресурсы подов: `kubectl top pods -n vmks`.

---

## Заключение и выводы

Проведённое нагрузочное тестирование VictoriaMetrics stack большим количеством VMRule подтвердило заявленные цели и позволило сформулировать практические выводы.

### Достигнутые результаты

- **Распределение правил по ConfigMap'ам:** Operator стабильно дробит правила при приближении к лимиту ~1 MiB: каждый ConfigMap заполняется до ~510 KB, затем создаётся следующий. При ~660 VMRule наблюдается 12 ConfigMap'ов (~5,8 MB суммарно). Механизм предсказуем и масштабируется линейно.
- **Перезапуски vmalert:** Каждое появление нового ConfigMap приводит к пересозданию Pod'а vmalert (из-за добавления volume/volumeMount). При темпе применения ~80 VMRule за ~7 минут интервал между рестартами составил ~7 мин. Горячая перезагрузка (SIGHUP) применяется только при обновлении существующих ConfigMap'ов без добавления новых.
- **Сохранение состояния:** Механизм remoteWrite/remoteRead (`ALERTS`, `ALERTS_FOR_STATE`) работает корректно: после рестарта vmalert восстанавливает состояние алертов из VictoriaMetrics, счётчики `for` не сбрасываются, потери алертов не происходит. Временное падение `sum(ALERTS)` во время рестарта — следствие задержки первой итерации, а не потери данных.
- **Пороги масштабируемости:** При ~660 VMRule (~15 600 алертов, ~2,1 млн рядов) vmalert выходит на 93% CPU limit (1 CPU) и ~28% памяти (2 Gi). VM Operator потребляет ~198m CPU. Линейная экстраполяция на 10 000 VMRule указывает на необходимость шардирования vmalert и масштабирования VMCluster.

### Основные выводы

1. **Операционная модель:** При массовом добавлении VMRule следует учитывать периодические рестарты vmalert (по одному на каждый новый ConfigMap). Для production целесообразно применять правила батчами или через GitOps с контролируемым темпом, чтобы не создавать лишние ConfigMap'ы подряд и снизить частоту рестартов.
2. **Ресурсы:** CPU vmalert — первое узкое место; до 2 000 VMRule достаточно увеличить CPU limit. При росте выше 2 000–5 000 VMRule необходимо шардирование vmalert и масштабирование VMCluster.
3. **Отказоустойчивость:** Конфигурация с двумя репликами vmalert, remoteRead/remoteWrite и Alertmanager в кластерном режиме обеспечивает восстановление без потери состояния алертов и без дублирования уведомлений. RTO vmalert — в пределах минуты.
4. **Мониторинг:** Ключевые метрики для раннего обнаружения перегрузки — `vmalert_iteration_duration_seconds`, `vmalert_iteration_missed_total`, `container_cpu_usage_seconds_total` (vmalert), а также размер и количество ConfigMap'ов с правилами.

Итог: VictoriaMetrics stack при правильной настройке (remoteRead/remoteWrite, HA vmalert и Alertmanager) выдерживает нагрузку тысячами правил и алертов с сохранением целостности состояния. Ограничения носят в основном ресурсный характер и снимаются шардированием и увеличением ресурсов в соответствии с приведёнными рекомендациями.
