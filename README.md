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

**Что делает:** Скрипт автоматически собирает "снимки" (замеры) загрузки системы VictoriaMetrics в заранее выбранные моменты времени — примерно при 500, 5000, 10000, ... 50000 активных алертах. Для этого он запрашивает метрики напрямую у vmselect через Prometheus API — такие как загрузка CPU, используемая память подов (в пространстве имён `vmks`), нагрузка на kube-apiserver и количество HTTP-запросов к компонентам vmselect/vmstorage/vminsert.

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

Ниже — только те показатели, которые в этом сценарии **реально растут** вместе с числом активных `ALERTS` и на которые **имеет смысл смотреть в первую очередь** при планировании ёмкости и отладке. Сравнение **~500** → **~50 000** `ALERTS` — по [таблицам Capacity Planning](#ресурсы-подов-при-росте-нагрузки) и [RPS](#rps-и-операционные-метрики). Метрики, которые в успешном прогоне остаются нулевыми или почти не меняются (ошибки eval, RPS к vmstorage), сюда не включены.

В столбце **«Порядок роста (прогон)»** для CPU и памяти указан **прирост на 1000 активных `ALERTS`** (линейная оценка по разнице между срезами ~50k и ~500, делённая на 49,5 тыс.): милликоры (**m**) и мебибайты (**MiB**). Для RPS и прочих величин — свои единицы на 1000 `ALERTS` или качественное описание.

### vmalert

| Метрика | Зачем смотреть | Порядок роста (прогон) |
| --- | --- | --- |
| `container_cpu_usage_seconds_total` (pod vmalert) | Главный индикатор вычислительной нагрузки на оценку правил | **~28 m** CPU на 1000 `ALERTS` |
| `container_memory_working_set_bytes` (pod vmalert) | Память под реестр алертов и экспорт метрик | **~23 MiB** на 1000 `ALERTS` |
| `vmalert_iteration_duration_seconds` | Насколько итерация близко к `interval` группы (риск пропусков при росте) | До **~3,8 с** max при ~23k алертов ([заключение](#заключение-и-выводы)); смотреть перцентили |
| `vmalert_remotewrite_requests_total` | Запись `ALERTS` / `ALERTS_FOR_STATE` в кластер | Растёт с числом групп и итераций; `rate()` |
| `vmalert_alerts_firing` / `vmalert_alerts_pending` | Фактическая нагрузка по срабатывающим правилам | Масштабируется с объёмом firing/pending; при агрегации — `max`/`avg by`, не голый `sum` по репликам |

### vmselect и запросы к TSDB

| Метрика | Зачем смотреть | Порядок роста (прогон) |
| --- | --- | --- |
| `vm_http_requests_total{job="vmselect"}` | Основной поток чтения при eval и запросах правил | **~40** req/s на 1000 `ALERTS` |
| `vm_concurrent_select_current` | Параллельная нагрузка на select | Заметно растёт с RPS vmselect (без фиксированного множителя в таблицах) |
| `vm_select_request_duration_seconds` | Хвост латентности при тяжёлых запросах (в т.ч. `ALERTS_FOR_STATE`) | Смотреть p95/p99 на графике |

### vmstorage (данные и память)

| Метрика | Зачем смотреть | Порядок роста (прогон) |
| --- | --- | --- |
| `container_memory_working_set_bytes` (pod vmstorage) | RAM под кэш и данные; типичное узкое место по памяти | **~49 MiB** на 1000 `ALERTS` |
| `container_cpu_usage_seconds_total` (pod vmstorage) | CPU на слияние и обслуживание данных | **~3,9 m** CPU на 1000 `ALERTS` |
| `vm_rows` / `vm_rows_inserted_total`, `vm_storage_blocks` | Объём рядов и данных на диске | Рост на порядки по мере заполнения стенда |

### vminsert

| Метрика | Зачем смотреть | Порядок роста (прогон) |
| --- | --- | --- |
| `vm_http_requests_total{job="vminsert"}` | Поток remote write (в т.ч. от vmalert) | **~0,1** req/s на 1000 `ALERTS` — умеренно относительно select |

### vmagent (скрейп vmalert)

| Метрика | Зачем смотреть | Порядок роста (прогон) |
| --- | --- | --- |
| `scrape_body_size_bytes` / `scrape_samples_scraped` (target vmalert) | Раздувание `/metrics` vmalert при росте числа алертов | **На порядки**; проверять лимиты `maxScrapeSize` и память агента |

### VictoriaMetrics Operator

| Метрика | Зачем смотреть | Порядок роста (прогон) |
| --- | --- | --- |
| `process_cpu_seconds_total` / CPU пода operator | Reconcile большого числа `VMRule` и ConfigMap'ов | **~3,3 m** CPU на 1000 `ALERTS` |
| `process_resident_memory_bytes` / память пода | Рост модели правил в памяти процесса | **~5,4 MiB** на 1000 `ALERTS` |

## Заключение и выводы

Нагрузочное тестирование VictoriaMetrics stack выполнено **на полном объёме**: все сгенерированные `VMRule` применены к кластеру (см. [применение VMRule](#применение-vmrule-в-kubernetes)), измерения в [Capacity Planning](#capacity-planning) и в разделе метрик отражают стационар после наращивания нагрузки до **~50 000** активных `ALERTS` (вместе с промежуточными срезами от ~500). Ниже — итоги этого завершённого прогона.

### Достигнутые результаты

- **Отказоустойчивость:** Механизм remoteWrite/remoteRead (`ALERTS`, `ALERTS_FOR_STATE`) подтверждён на практике: после рестарта vmalert восстанавливает состояние алертов из VictoriaMetrics, счётчики `for` не сбрасываются, потери алертов не наблюдались. `vmalert_execution_errors_total = 0` и `vmalert_iteration_missed_total = 0` на отчётном прогоне. Временное падение `sum(ALERTS)` во время рестарта — следствие задержки первой итерации, а не потери данных. **Перезапуски vmalert** при поэтапном применении правил: каждое появление нового ConfigMap приводило к пересозданию Pod'а (volume/volumeMount); интервал между рестартами ~13–15 мин, за период массового apply — 15 ReplicaSet'ов (14 пересозданий). Горячая перезагрузка (SIGHUP) возможна при обновлении существующих ConfigMap'ов без добавления новых.
- **Потребление ресурсов и масштабируемость:** На финальном срезе (~50k `ALERTS`, см. таблицы) vmalert — порядка **~1,4** ядра CPU и **~1,2 Gi** RAM на реплику в среднем по поду, в пределах лимитов (4 CPU / 4 Gi в `vmks-values.yaml`). На промежуточном срезе (~23k алертов, ~4,79 млн рядов) `max(vmalert_iteration_duration_seconds)` достигала **~3,84 с** — важный ориентир относительно `interval` 30s. Дальнейший рост к **~100k** алертов в эксплуатации разумно планировать с шардированием vmalert и масштабированием VMCluster. **ConfigMap'ы:** Operator стабильно дробит правила у лимита ~1 MiB; при ~23k алертов фиксировалось 17 ConfigMap'ов (~8,5 MB суммарно) — механизм предсказуем.
- **Нагрузка на API server:** По срезам Capacity Planning при росте `count(ALERTS)` с ~500 до ~50 000 RPS API server оставался умеренным (**~11–14** req/s в среднем по окнам), p99 задержки **34–119 ms**, CPU kube-apiserver **~70–110m** — нагрузка на control plane заметна, но в этом прогоне не выступала главным узким местом по сравнению с vmalert/vmselect/vmstorage.

### Основные выводы

1. **Отказоустойчивость:** Конфигурация с двумя репликами vmalert, remoteRead/remoteWrite и Alertmanager в кластерном режиме после полного развёртывания правил показала восстановление без потери состояния алертов и без дублирования уведомлений. RTO vmalert — в пределах минуты (детали — в комментариях к `vmks-values.yaml`). Итоговые показатели: `vmalert_execution_errors_total = 0`, `vmalert_iteration_missed_total = 0`. Для **следующих** массовых изменений набора VMRule стоит по-прежнему закладывать периодические рестарты vmalert (по одному на каждый новый ConfigMap) и при необходимости использовать батчи или GitOps с темпом — чтобы не создавать лишние ConfigMap'ы подряд.
2. **Потребление ресурсов:** На **~50k** алертов запас по CPU vmalert относительно лимита 4 CPU ещё есть; при целевых **~100k** алертов — ориентир на лимиты 3–4 CPU на реплику, шардирование vmalert и масштабирование VMCluster. vmstorage на пике — порядка **~2,6 Gi** RAM на реплику по таблице; это нужно закладывать в планирование нод.
3. **Нагрузка на API server:** Зафиксированный рост числа VMRule и активности reconcile Operator'а увеличивал RPS и задержки apiserver умеренно относительно роста алертов; в эксплуатации имеет смысл держать под контролем `apiserver_request_total`, `apiserver_request_duration_seconds` и CPU kube-apiserver наряду с метриками data plane.
4. **Мониторинг:** Для раннего обнаружения перегрузки остаются ключевыми `vmalert_iteration_duration_seconds`, `vmalert_iteration_missed_total`, `container_cpu_usage_seconds_total` (vmalert), а также размер и число ConfigMap'ов с правилами.

**Итог:** после применения всего набора правил подтверждены приоритеты исследования — **отказоустойчивость** (целостность состояния и HA) сохраняется при десятках тысяч правил; **ресурсы** и **нагрузка на API server** в измеренном диапазоне растут предсказуемо. Дальнейшее масштабирование упирается в шардирование vmalert, ресурсы VMCluster и осознанный темп будущих изменений конфигурации правил, а не в незавершённость текущего теста.