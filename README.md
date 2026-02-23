# Нагрузочное тестирование VictoriaMetrics большим количеством алертов

## Цель

Исследовать поведение VictoriaMetrics stack при нагрузке большим количеством правил оповещений (`VMRule`) в Kubernetes. В частности:

- понять, как VictoriaMetrics Operator распределяет правила по ConfigMap'ам при превышении лимита ~1 MiB;
- выяснить, при каких условиях происходит пересоздание Pod'а `vmalert` и как это влияет на состояние алертов;
- проверить механизм сохранения и восстановления состояния алертов через `remoteWrite`/`remoteRead`;
- определить практические пороги масштабируемости и дать рекомендации по эксплуатации.

## Стенд

**Кластер:** 3 ноды Kubernetes v1.32.1 на Yandex Cloud (Ubuntu 22.04.5 LTS, containerd 1.7.27).

**Стек:** `victoria-metrics-k8s-stack` v0.71.1 (VictoriaMetrics v1.136.0), установленный через Helm в namespace `vmks`.

Компоненты стека:

| Компонент | Pod | Роль |
|-----------|-----|------|
| VMSingle | `vmsingle-vmks-...-p675c` | Хранение метрик (single-node) |
| VMAlert | `vmalert-vmks-...-5zc72` | Оценка правил и отправка алертов |
| VMAgent | `vmagent-vmks-...-88prr` | Сбор метрик (scrape) |
| VMAlertmanager | `vmalertmanager-vmks-...-0` | Маршрутизация уведомлений |
| VM Operator | `vmks-victoria-metrics-operator-...-cgkvk` | Управление CRD-ресурсами |
| Grafana | `vmks-grafana-...-dhtf7` | Визуализация |

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

Файл `vmks-values.yaml` включает Grafana с ingress на `grafana.apatsev.org.ru` и плагин `victoriametrics-logs-datasource`.

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

---

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
NAME                                CPU     MEMORY
vmalert-vmks-...-5zc72              170m    281Mi
vmsingle-vmks-...-p675c             88m     394Mi
vmagent-vmks-...-88prr              29m     88Mi
vmalertmanager-vmks-...-0           85m     52Mi
vmks-victoria-metrics-operator-...  22m     68Mi
vmks-grafana-...                    9m      278Mi
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

---

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

---

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

---

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
