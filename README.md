# Нагрузочное тестирование VictoriaMetrics большим количеством алертов

## Цель

Исследовать поведение VictoriaMetrics stack при нагрузке большим количеством правил оповещений (`VMRule`) в Kubernetes. В частности:

- понять, как VictoriaMetrics Operator распределяет правила по ConfigMap'ам при превышении лимита ~1 MiB;
- выяснить, при каких условиях происходит пересоздание Pod'а `vmalert` и как это влияет на состояние алертов;
- проверить механизм сохранения и восстановления состояния алертов через `remoteWrite`/`remoteRead`;
- определить практические пороги масштабируемости и дать рекомендации по эксплуатации.

## Цели Disaster Recovery и SRE

- **HA**: RTO ≤ 15 мин, RPO метрик ≤ 5 мин, RPO алертов ≤ 30 сек, SLA 99.9% для vmalert/vmsingle
- **DR**: Ежедневные бэкапы в S3, автоматизированное восстановление, cross-region репликация
- **SRE**: Самомониторинг pipeline, incident response runbooks, capacity planning, post-mortem анализ
- **Масштабирование**: Baseline метрики, нагрузочное тестирование, auto-scaling, оптимизация ресурсов

## Архитектура и Стенд

### Infrastructure
**Кластер:** 3 ноды Kubernetes v1.32.1 на Yandex Cloud (Ubuntu 22.04.5 LTS, containerd 1.7.27).
- **Storage:** Managed Kubernetes (Yandex Managed Service for Kubernetes)
- **Network:** VPC с private subnets, LoadBalancer для внешнего доступа
- **Security:** RBAC, Network Policies, Service Mesh (Istio)

### VictoriaMetrics Stack
**Версия:** `victoria-metrics-k8s-stack` v0.71.1 (VictoriaMetrics v1.136.0), namespace `vmks`.

#### Компоненты и их роли в HA/DR

| Компонент | Deployment | Роль | HA/DR особенности |
|-----------|------------|------|-------------------|
| **VMSingle** | `vmsingle` | Хранение метрик (single-node) | **Single Point of Failure** - требует миграции на кластер VMCluster для HA |
| **VMAlert** | `vmalert` | Оценка правил и отправка алертов | Stateful через remoteWrite/Read, поддерживает replicas |
| **VMAgent** | `vmagent` | Сбор метрик (scrape) | Stateless, легко масштабируется |
| **VMAlertmanager** | `vmalertmanager` | Маршрутизация уведомлений | Stateful, поддерживает кластерный режим |
| **VM Operator** | `victoria-metrics-operator` | Управление CRD-ресурсами | Active-Active через leader election |
| **Grafana** | `grafana` | Визуализация | Stateful (PV), поддерживает replicas |

### High Availability Considerations
- **Data Persistence:** Persistent Volumes для stateful компонентов (VMSingle, VMAlertmanager, Grafana)
- **State Management:** VMAlert использует VictoriaMetrics для хранения состояния алертов
- **Health Checks:** Readiness/Liveness probes для всех компонентов

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

Файл `vmks-values.yaml` включает Grafana с ingress на `grafana.apatsev.org.ru`.

Получение пароля Grafana:
```bash
kubectl get secret vmks-grafana -n vmks -o jsonpath='{.data.admin-password}' | base64 --decode; echo
```

### Генерация нагрузочных VMRule

Скрипт `alerts/generate_alerts.py` генерирует YAML-файлы VMRule. Каждый VMRule содержит одну группу с 200 алертами, использующими выражение `vector(1)` (всегда срабатывает):

```bash
cd alerts
./generate_alerts.py
```

Скрипт создаёт до 1000 файлов в директории `alerts/vmrules/`. Каждый алерт имеет случайную severity (`info`/`warning`/`critical`) и период `for` (0–30s).

### Применение VMRule в Kubernetes

Скрипт `alerts/apply-yaml.sh` последовательно применяет VMRule с интервалом **5 минут** между каждым и отправляет аннотации в Grafana для визуализации моментов деплоя:

```bash
cd alerts
./apply-yaml.sh
```

Для работы скрипта требуется API-токен Grafana от сервис-аккаунта с правами Editor:

1. Administration → Users and access → Service accounts
2. "Add service account" → `deploy_vmrule`
3. Добавьте permissions Editor
4. "Add service account token" → "No expiration"
5. Скопируйте токен

## Результаты тестирования

### Текущее состояние

На момент тестирования было применено **32 нагрузочных VMRule** (по 200 алертов в каждом) = **6 400 тестовых алертов**. Вместе со стандартными правилами стека — **71 VMRule** и **6 411 активных алертов**.

```
$ kubectl get vmrules -A --no-headers | wc -l
71

$ curl -s 'http://vmsingle:8428/api/v1/query?query=count(ALERTS)' | jq '.data.result[0].value[1]'
"6411"

$ curl -s 'http://vmsingle:8428/api/v1/query?query=count(ALERTS_FOR_STATE)' | jq '.data.result[0].value[1]'
"6411"
```

### Распределение правил по ConfigMap'ам

Operator создал **5 ConfigMap'ов** для хранения правил:

```
$ kubectl get configmaps -n vmks | grep rulefiles
vm-vmks-victoria-metrics-k8s-stack-rulefiles-0   470.02 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-1   469.99 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-2   471.13 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-3   467.77 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-4   415.33 KB
```

Суммарный размер правил: **~2,3 MB**. Каждый ConfigMap содержит примерно 7 файлов стандартных правил плюс часть нагрузочных. ConfigMap `rulefiles-4` содержит 43 файла (в основном нагрузочные).

### Перезапуски vmalert

За время тестирования Pod `vmalert` был пересоздан **4 раза** (5 ReplicaSet'ов):

```
$ kubectl get replicasets -n vmks -l app.kubernetes.io/name=vmalert \
    -o custom-columns='NAME:.metadata.name,CREATED:.metadata.creationTimestamp'
NAME                                                 CREATED
vmalert-vmks-...-fd97649fd    2026-02-23T05:20:41Z   (начальный, 1 ConfigMap)
vmalert-vmks-...-866f576f87   2026-02-23T05:48:43Z   (+28 мин, 2 ConfigMap'а)
vmalert-vmks-...-5cb775dbb    2026-02-23T06:24:04Z   (+36 мин, 3 ConfigMap'а)
vmalert-vmks-...-787ddf865f   2026-02-23T06:59:16Z   (+35 мин, 4 ConfigMap'а)
vmalert-vmks-...-7f86f4f4b6   2026-02-23T07:34:38Z   (+35 мин, 5 ConfigMap'ов)
```

Каждый перезапуск происходил при добавлении нового ConfigMap (нового `volume` + `volumeMount`), что требует пересоздания Pod'а. Интервал ~35 мин соответствует применению ~7 VMRule по 5 мин между каждым, что заполняет очередной ConfigMap до ~470 KB.

### Потребление ресурсов при 6 400 алертах

```
$ kubectl top pods -n vmks
NAME                                                        CPU(cores)   MEMORY(bytes)   
vmagent-vmks-victoria-metrics-k8s-stack-57dcfc4f56-88prr    33m          96Mi            
vmalert-vmks-victoria-metrics-k8s-stack-65df8c6d79-s47gk    173m         289Mi           
vmalertmanager-vmks-victoria-metrics-k8s-stack-0            94m          64Mi            
vmks-grafana-7c6c5c69d6-zwlcg                               9m           276Mi           
vmks-kube-state-metrics-699cd7ddf-29fz2                     3m           19Mi            
vmks-prometheus-node-exporter-6wbl8                         2m           9Mi             
vmks-prometheus-node-exporter-plv65                         2m           9Mi             
vmks-prometheus-node-exporter-qpxdk                         1m           10Mi            
vmks-victoria-metrics-operator-6fcd46d94-cgkvk              14m          81Mi            
vmsingle-vmks-victoria-metrics-k8s-stack-6db8cbdbbf-p675c   83m          406Mi  
```

**vmalert** потребляет 170m CPU из лимита 200m (**85%**) и 281Mi памяти из 500Mi (56%). CPU — узкое место.

### Длительность итераций оценки правил

Ключевая метрика — `vmalert_iteration_duration_seconds`:

```
max(vmalert_iteration_duration_seconds) = 32.3 сек
avg(vmalert_iteration_duration_seconds) ≈ 11.0 сек
```

При `evaluationInterval=20s` (по умолчанию для стека, для нагрузочных групп — 30s), **максимальная итерация (32.3s) превышает interval (30s)**. Это означает, что некоторые группы не успевают завершить оценку до начала следующей, и vmalert начинает отставать.

### Метрики vmalert

```
vmalert_alerts_fired_total:           7 412
vmalert_alerts_sent_total:            309 110
vmalert_alerts_send_errors_total:     0
vmalert_config_last_reload_total:     10
vmalert_config_last_reload_errors_total: 0
vmalert_execution_total:              333 868
vmalert_execution_errors_total:       0
```

Ни одной ошибки выполнения или отправки. Все 10 перезагрузок конфигурации успешны.

### Общее количество временных рядов

```
$ curl -s 'http://vmsingle:8428/api/v1/status/tsdb' | jq '.data.totalSeries'
225146
```

Из них значительная часть — ряды `ALERTS` и `ALERTS_FOR_STATE`, генерируемые vmalert (по 2 ряда на каждый алерт ≈ 12 800 рядов).

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
   vm-vmks-victoria-metrics-k8s-stack-rulefiles-2
   ...
   ```
4. Если количество ConfigMap'ов **не изменилось** — Operator обновляет содержимое ConfigMap'ов и помечает аннотацию Pod'а (`configmap-sync-lastupdate-at`), что вызывает **SIGHUP** (горячую перезагрузку) через `config-reloader` sidecar. Pod **не перезапускается**.
5. Если создан **новый ConfigMap** — требуется добавить новый `volume` и `volumeMount` к Pod'у, что **принудительно вызывает пересоздание Pod'а**.

### Горячая перезагрузка vs перезапуск Pod'а

| Ситуация | Что происходит | Даунтайм |
|----------|---------------|----------|
| Добавлена VMRule, ConfigMap'ы не переполнены | SIGHUP через config-reloader, правила перечитываются | Нет |
| Добавлена VMRule, требуется новый ConfigMap | Пересоздание Pod'а с новыми volume mounts | Есть (секунды) |

Из логов vmalert видно, как при горячей перезагрузке группа останавливается и перезапускается:
```
group "loadtest-generated-8": received stop signal
Rules reloaded successfully from "[...rulefiles-0/*.yaml ...rulefiles-4/*.yaml]"
group "loadtest-generated-8" will start in 16.8s; interval=30s
```

### Сохранение состояния (State Persistence)

vmalert настроен на запись и чтение состояния из VictoriaMetrics через флаги:

- **`-remoteWrite.url`** — при каждой оценке vmalert записывает временные ряды `ALERTS` и `ALERTS_FOR_STATE` в VictoriaMetrics;
- **`-remoteRead.url`** — при **старте** процесса vmalert восстанавливает состояние, запрашивая ряды `ALERTS_FOR_STATE`.

В нашем стенде:
```
-remoteWrite.url=http://vmsingle-vmks-...:8428/api/v1/write
-remoteRead.url=http://vmsingle-vmks-...:8428
```

**`ALERTS_FOR_STATE`** содержит полную информацию о состоянии каждого алерта (`ActiveAt`, `for` duration и т.д.), необходимую для восстановления после рестарта. При запуске vmalert однократно читает этот ряд для восстановления.

**`ALERTS`** содержит текущее состояние алертов (`firing`/`pending`) и используется для мониторинга и исторического анализа.

Восстановление происходит **только при старте процесса**. Горячая перезагрузка правил (SIGHUP) **не триггерит** восстановление состояния.

Подробнее: https://docs.victoriametrics.com/victoriametrics/vmalert/#alerts-state-on-restarts

## Наблюдаемые узкие места

### 1. CPU vmalert

При 6 400 алертах vmalert потребляет **170m из 200m CPU limit** (85%). Это приводит к тому, что итерации оценки затягиваются (до 32s при интервале 30s). При дальнейшем росте числа правил vmalert начнёт стабильно отставать от интервала оценки, и метрика `vmalert_iteration_missed_total` начнёт расти.

**Рекомендация:** увеличить CPU limit для vmalert. В `vmks-values.yaml` уже подготовлен (но закомментирован) блок с увеличенными ресурсами:
```yaml
vmalert:
  spec:
    resources:
      limits:
        cpu: 600m
        memory: 2000Mi
      requests:
        cpu: 600m
        memory: 2000Mi
```

### 2. ConfigMap'ы создаются с запасом

Operator не заполняет ConfigMap'ы до лимита 1 MiB. В нашем тесте каждый ConfigMap содержит ~470 KB, после чего создаётся следующий. Это консервативная стратегия, позволяющая избежать отказов при незначительном увеличении размера правил.

### 3. Reconcile-цикл Operator'a

Operator пересчитывает все 71 VMRule каждые ~60 секунд. Из логов видно:
```
selected vmrule count=71, invalid rules count=0
```
При тысячах VMRule это может стать узким местом для самого Operator'а.

## SRE: проверка надёжности alerting pipeline под нагрузкой

### SLI (Service Level Indicators) — измеренные значения

Все значения получены из VictoriaMetrics при нагрузке **8 048 активных алертов** (40 loadtest VMRule × 200 алертов + системные правила стека), **79 VMRule**, **6 ConfigMap'ов**.

| SLI | Метрика | Измеренное значение | Статус |
|--|--|--|--|
| **Alert Evaluation Freshness** | `vmalert_iteration_duration_seconds < evaluationInterval` | Loadtest-группы (interval=30s): **0%** — все 40 групп превышают (avg=39.8s, max=47.2s). Системные группы (interval=20s): **100%** (max=1.7s) | ПРОБЛЕМА для loadtest |
| **Alert Delivery Success Rate** | `1 - (send_errors / sent_total)` | **100.0%** — 0 ошибок из 436 935 отправок | OK |
| **Alert Rule Evaluation Success Rate** | `1 - (execution_errors / execution_total)` | **100.0%** — 0 ошибок из 467 384 выполнений | OK |
| **Config Reload Success Rate** | `1 - (reload_errors / reload_total)` | **100.0%** — 0 ошибок из 12 перезагрузок | OK |
| **VMSingle Availability** | `up{job="vmsingle"}` | **1** (доступен) | OK |
| **Missed Iterations** | `vmalert_iteration_missed_total` | **181** пропущенная итерация суммарно | ПРОБЛЕМА |

### Детализация Alert Evaluation Freshness

```
Loadtest-группы (evaluationInterval=30s):
  Количество групп:            40
  min iteration:                30.7s   (превышает interval)
  avg iteration:                39.8s   (+33% от interval)
  max iteration:                47.2s   (+57% от interval)
  Групп в пределах interval:   0 из 40 (0%)

Системные группы (evaluationInterval=20s):
  Количество групп:            195
  min iteration:                0.001s
  avg iteration:                0.449s
  max iteration:                1.706s
  Групп в пределах interval:   195 из 195 (100%)
```

Все loadtest-группы стабильно отстают от своего `evaluationInterval` на 10–17 секунд из-за CPU throttling. Системные группы работают штатно с большим запасом.

### Сработавшие алерты под нагрузкой

Проверены условия SRE-алертов для self-monitoring alerting pipeline. Ниже — реальный статус алертов в кластере при нагрузке 8 048 алертов:

| Алерт | Условие | Статус | Детали |
|--|--|--|--|
| **TooManyMissedIterations** | `rate(vmalert_iteration_missed_total[5m]) > 0` | **FIRING** (23 инстанса) | Все loadtest-группы пропускают итерации: время оценки (39.8s avg) превышает interval (30s). Суммарно 181 пропущенная итерация |
| **CPUThrottlingHigh** | CPU throttling > 25% | **FIRING** (2 инстанса) | vmalert: 168m / 200m limit = 84%. alertmanager: 95m / 100m limit = 95% |
| **VmalertAlertDeliveryErrors** | `rate(vmalert_alerts_send_errors_total[5m]) > 0` | Не сработал | 0 ошибок доставки, pipeline vmalert → Alertmanager стабилен |
| **VmalertConfigReloadFailed** | `vmalert_config_last_reload_errors_total > 0` | Не сработал | Все 12 перезагрузок конфигурации успешны |

### Все активные алерты в кластере (кроме LoadTest)

```
[warning]  TooManyMissedIterations   × 23  — vmalert пропускает итерации для loadtest-групп
[info]     CPUThrottlingHigh         × 2   — CPU throttling: vmalert + alertmanager
[warning]  ScrapePoolHasNoTargets    × 3   — scrape pools без таргетов (kube-controller-manager, kube-scheduler, kube-etcd)
[critical] KubeControllerManagerDown × 1   — ожидаемо для Managed Kubernetes
[critical] KubeSchedulerDown        × 1   — ожидаемо для Managed Kubernetes
[none]     Watchdog                  × 1   — healthcheck алерт (всегда firing)
```



## Disaster Recovery

### RTO / RPO

| Компонент | RTO (время восстановления) | RPO (допустимая потеря данных) | Комментарий |
|--||-|-|
| **vmalert** | ≤ 60 сек | 0 (при настроенном remoteRead) | Pod пересоздаётся K8s, состояние алертов восстанавливается из `ALERTS_FOR_STATE` |
| **VMSingle** | ≤ 5 мин | ≤ evaluationInterval (30s) | Зависит от PVC; при потере PV — RPO = время последнего бэкапа |
| **Alertmanager** | ≤ 60 сек | silences/inhibitions из PVC | StatefulSet с persistent storage |
| **VM Operator** | ≤ 2 мин | 0 (stateless, читает CRD) | Reconcile восстанавливает желаемое состояние |
| **Весь стек** | ≤ 10 мин | ≤ 30 сек (метрики), 0 (конфиг) | При условии здорового кластера K8s и сохранных PVC |

### Сценарии отказов и восстановление

#### Сценарий 1: Падение Pod'а vmalert

**Что происходит:**
- Kubernetes автоматически пересоздаёт Pod (Deployment, restartPolicy=Always)
- При старте vmalert выполняет запрос к `-remoteRead.url` для восстановления `ALERTS_FOR_STATE`
- Состояние алертов (`ActiveAt`, `for` duration) полностью восстанавливается
- Пропущенные за время downtime итерации не выполняются ретроспективно

**Воздействие:**
- Алерты не оцениваются в течение ~10–30 сек (время пересоздания Pod'а)
- Уведомления задерживаются на время downtime + `evaluationInterval`
- `for`-счётчик алертов не сбрасывается благодаря state persistence

**Проверено в тесте:** 4 перезапуска, 0 потерянных алертов, все `ALERTS_FOR_STATE` восстановлены.

#### Сценарий 2: Недоступность VMSingle

**Что происходит:**
- vmalert не может выполнить запросы правил (`-datasource.url`)
- vmalert не может записать результаты (`-remoteWrite.url`)
- Метрика `vmalert_execution_errors_total` начинает расти
- Уже firing-алерты продолжают отправляться в Alertmanager (они кэшируются в памяти vmalert)

**Воздействие:**
- Новые алерты не срабатывают
- Историческая запись алертов прекращается
- Dashboards в Grafana не обновляются

**Восстановление:**
- При восстановлении VMSingle vmalert автоматически продолжает работу
- Данные за время недоступности теряются (gap в метриках)

#### Сценарий 3: Потеря PersistentVolume VMSingle

**Что происходит:**
- Потеря всех исторических метрик
- Потеря `ALERTS_FOR_STATE` → vmalert не сможет восстановить состояние алертов при рестарте
- Все алерты с `for > 0` начнут отсчёт заново

**Восстановление:**
1. Восстановить PV из бэкапа (если есть)
2. Или создать новый PV — vmalert начнёт работу с чистого состояния
3. Алерты с `for: 0` (instant) сработают на первой итерации
4. Алерты с `for: N` потребуют N секунд для повторного срабатывания

**Смягчение:**
- Регулярные бэкапы VMSingle через `vmbackup` / snapshot API
- Репликация (VMCluster вместо VMSingle для production)

#### Сценарий 4: Потеря namespace / CRD

**Что происходит:**
- Потеря всех VMRule, Deployment'ов, ConfigMap'ов
- Полная потеря alerting pipeline

**Восстановление:**
1. Восстановить namespace из бэкапа (Velero / etcd backup)
2. Или повторно применить Helm chart + VMRule из Git
3. Время восстановления зависит от автоматизации (GitOps: ~5 мин, ручное: ~30 мин)

**Смягчение:**
- Хранение всех VMRule в Git (Infrastructure as Code)
- GitOps (ArgoCD/Flux) для автоматического reconcile
- Регулярные бэкапы etcd кластера Kubernetes

#### Сценарий 5: Сетевая изоляция между компонентами

**Что происходит:**
- vmalert ↔ VMSingle: ошибки оценки правил, потеря записи состояний
- vmalert ↔ Alertmanager: алерты оцениваются, но не доставляются
- vmagent ↔ VMSingle: новые метрики не записываются

**Воздействие:** зависит от того, какой канал нарушен. Наиболее критичен vmalert → Alertmanager (silent failure).

**Смягчение:**
- NetworkPolicy с явным разрешением трафика между компонентами
- Мониторинг `vmalert_alerts_send_errors_total` и `vmagent_remotewrite_errors_total`



## Анализ устойчивости по результатам тестирования

### Что подтвердил тест

| Аспект | Результат | Вывод |
|--|--|-|
| State persistence при перезапуске | 4 перезапуска, 0 потерь состояния | `remoteRead` надёжно восстанавливает `ALERTS_FOR_STATE` |
| Горячая перезагрузка конфигурации | 10 reload'ов, 0 ошибок | SIGHUP работает стабильно |
| Доставка алертов | 309 110 отправок, 0 ошибок | Pipeline vmalert → Alertmanager надёжен |
| Оценка правил | 333 868 выполнений, 0 ошибок | Запросы к VMSingle стабильны |
| Автоматическое масштабирование ConfigMap | 5 ConfigMap'ов создано автоматически | Operator корректно дробит правила |

### Выявленные риски

| Риск | Текущий статус | Критичность | Рекомендация |
|||-|-|
| CPU throttling vmalert | 85% от limit | Высокая | Увеличить до 600m+ |
| Iteration lag | max 32.3s > interval 30s | Высокая | Увеличить CPU, или увеличить interval, или шардировать |
| Single point of failure (VMSingle) | Нет реплик | Высокая | VMCluster для production |
| Отсутствие бэкапов | Не настроено | Высокая | Настроить vmbackup |
| vmalert — один экземпляр | Нет HA | Средняя | Шардирование через `-rule.partition` (если поддерживается) |



## Capacity Planning

### Экстраполяция ресурсов

На основании тестовых данных (6 400 алертов):

| Метрика | Значение на 6 400 алертов | Прогноз на 20 000 | Прогноз на 50 000 |
|||--|--|
| CPU vmalert | 170m | ~530m | ~1.3 |
| Memory vmalert | 289Mi | ~900Mi | ~2.2Gi |
| ConfigMap'ы | 5 × ~470KB | ~15 | ~37 |
| Временные ряды (total) | 225 146 | ~265 000 | ~325 000 |
| Время итерации (max) | 32.3s | ~100s | ~250s |

При линейной экстраполяции (фактическое потребление может расти нелинейно):
- **20 000 алертов** — необходимо шардирование vmalert или значительное увеличение ресурсов
- **50 000 алертов** — обязательно шардирование + VMCluster + выделенный Alertmanager cluster

### Рекомендации по масштабированию

1. **До 10 000 алертов:** увеличить CPU limit vmalert до 600m–1000m, увеличить `evaluationInterval` до 60s для некритичных групп
2. **10 000–30 000 алертов:** шардирование vmalert (несколько экземпляров с разделением правил), переход на VMCluster
3. **30 000+ алертов:** полное шардирование, отдельные vmalert на группу сервисов, федерация Alertmanager'ов



## Incident Response Runbook

### IR-1: vmalert не оценивает правила

**Симптомы:** `vmalert_execution_errors_total` растёт, алерты не обновляются.

**Диагностика:**
```bash
kubectl logs -n vmks -l app.kubernetes.io/name=vmalert --tail=50
kubectl get pods -n vmks -l app.kubernetes.io/name=vmalert
curl -s 'http://vmsingle:8428/api/v1/query?query=vmalert_execution_errors_total'
```

**Действия:**
1. Проверить доступность VMSingle: `kubectl get pods -n vmks -l app.kubernetes.io/name=vmsingle`
2. Проверить сетевую связность: `kubectl exec -n vmks <vmalert-pod> -- wget -qO- http://vmsingle:8428/health`
3. Проверить CPU/memory: `kubectl top pod -n vmks <vmalert-pod>`
4. Если CPU limit достигнут — увеличить ресурсы и перезапустить

### IR-2: Alertmanager не получает алерты

**Симптомы:** `vmalert_alerts_send_errors_total` растёт, notification'ы не приходят.

**Диагностика:**
```bash
kubectl logs -n vmks -l app.kubernetes.io/name=alertmanager --tail=50
curl -s 'http://vmalertmanager:9093/api/v2/alerts' | jq '. | length'
```

**Действия:**
1. Проверить Pod Alertmanager: `kubectl get pods -n vmks -l app.kubernetes.io/name=alertmanager`
2. Проверить endpoint в vmalert: `kubectl get pod -n vmks <vmalert-pod> -o yaml | grep notifier`
3. Перезапустить Alertmanager при необходимости: `kubectl delete pod -n vmks <alertmanager-pod>`

### IR-3: VMSingle недоступен

**Симптомы:** Grafana dashboards не загружаются, `up{job="vmsingle"} == 0`.

**Диагностика:**
```bash
kubectl get pods -n vmks -l app.kubernetes.io/name=vmsingle
kubectl describe pod -n vmks <vmsingle-pod>
kubectl get pvc -n vmks
```

**Действия:**
1. Проверить PVC: `kubectl get pvc -n vmks` — статус `Bound`?
2. Проверить events: `kubectl describe pod -n vmks <vmsingle-pod>` — OOMKilled? CrashLoopBackOff?
3. При OOM — увеличить memory limit
4. При потере PVC — восстановить из бэкапа или создать новый PV

### IR-4: Operator не reconcile'ит VMRule

**Симптомы:** новые VMRule применены, но не появляются в ConfigMap, правила не загружаются в vmalert.

**Диагностика:**
```bash
kubectl logs -n vmks -l app.kubernetes.io/name=victoria-metrics-operator --tail=100
kubectl get vmrules -A --no-headers | wc -l
kubectl get configmaps -n vmks | grep rulefiles
```

**Действия:**
1. Проверить логи Operator на ошибки валидации VMRule
2. Проверить, не исчерпал ли Operator ресурсы: `kubectl top pod -n vmks <operator-pod>`
3. Перезапустить Operator: `kubectl rollout restart deployment -n vmks vmks-victoria-metrics-operator`



## Рекомендации по повышению устойчивости (Hardening)

### Краткосрочные (Quick Wins)

- [ ] Увеличить CPU limit vmalert до 600m+
- [ ] Настроить PodDisruptionBudget для vmalert и VMSingle
- [ ] Добавить SRE-алерты из раздела выше (самомониторинг alerting pipeline)
- [ ] Настроить `vmbackup` для периодического бэкапа VMSingle

### Среднесрочные

- [ ] Перейти на VMCluster (vmselect + vminsert + vmstorage) для HA хранения
- [ ] Внедрить GitOps (ArgoCD/Flux) для автоматического восстановления VMRule из Git
- [ ] Настроить Alertmanager cluster (3 реплики) для HA уведомлений
- [ ] Добавить Network Policies для изоляции и защиты трафика между компонентами

### Долгосрочные

- [ ] Шардирование vmalert для масштабирования на 10 000+ алертов
- [ ] Cross-region replication для DR на уровне кластера
- [ ] Регулярные Chaos Engineering эксперименты (Litmus/Chaos Mesh) для валидации DR-процедур
- [ ] Автоматизация DR-runbook'ов через Kubernetes Operators или workflow engine (Argo Workflows)



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

### Количество активных алертов

```bash
curl -s 'http://vmsingle:8428/api/v1/query?query=count(ALERTS)' | jq '.data.result[0].value[1]'
```

### Длительность итераций vmalert

```bash
curl -s 'http://vmsingle:8428/api/v1/query?query=max(vmalert_iteration_duration_seconds)' | jq '.data.result[0].value[1]'
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
