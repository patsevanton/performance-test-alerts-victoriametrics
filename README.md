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

Ключевые настройки HA (файл `vmks-values.yaml`):
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

Файл `vmks-values.yaml` включает Grafana с ingress на [grafana.apatsev.org.ru](https://grafana.apatsev.org.ru).

Получение пароля Grafana:
```bash
kubectl get secret vmks-grafana -n vmks -o jsonpath='{.data.admin-password}' | base64 --decode; echo
```

### Генерация нагрузочных VMRule

Скрипт [`alerts/generate_alerts.py`](https://github.com/patsevanton/performance-test-alerts-victoriametrics/blob/main/alerts/generate_alerts.py) генерирует YAML-файлы `VMRule` в директорию `alerts/vmrules/`. По умолчанию создаётся **10 000** файлов; каждый `VMRule` содержит **4–6 групп** (с `interval` 30s/1m/2m) и **20 алертов** суммарно.

```bash
cd alerts
./generate_alerts.py
```

Правила «псевдо-реалистичные»: разные шаблоны (k8s/node/http/db/…), `expr` построены на `vector(...)`, `severity` задаётся шаблоном (в основном `warning`/`critical`), `for` — от `0s` до `1h`. Генерация детерминирована (seed=42); объём можно изменить в `main()` через `num_vmrules` и `alerts_per_vmrule`.

### Применение VMRule в Kubernetes

Скрипт `alerts/apply-yaml.sh` последовательно применяет VMRule с интервалом **5 секунд** между каждым:

```bash
cd alerts
./apply-yaml.sh
```

Скрипт проходит все 10 000 файлов. Полное применение занимает ~14 часов.

## Текущее состояние (тест в процессе)

> **Примечание:** тест ещё выполняется — `apply-yaml.sh` продолжает применять VMRule. Данные ниже — снимок на момент ~660 применённых VMRule.

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
2. Operator пытает упаковать правила в ConfigMap `rulefiles-0`.
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
- [ ] Chaos Engineering эксперименты для валидации процедур восстановления

## Полезные команды для мониторинга

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
