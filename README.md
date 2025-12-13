# Нагрузочное тестирование алертами большим кол-вом алертов в victoriametrics

## Введение — какую проблему решаем


## Установка victoria-metrics-k8s-stack

Добавим Helm репозиторий и установим VictoriaMetrics stack:

```bash
helm repo add vm https://victoriametrics.github.io/helm-charts/
helm repo update

helm upgrade --install vmks vm/victoria-metrics-k8s-stack \
  --namespace vmks --create-namespace \
  --wait --values vmks-values.yaml
```


После установки, Grafana будет доступна по адресу http://grafana.apatsev.org.ru

Получение пароля grafana для admin юзера
```shell
kubectl get secret vmks-grafana -n vmks -o jsonpath='{.data.admin-password}' | base64 --decode; echo
```

Из директории alerts запустите скрипт ./generate_alerts.py который будет генерировать vmrule.


Из директории alerts запустите скрипт `apply-yaml.sh`, который будет применять vmrule в kubernetes и делать аннотации в grafana.

# Создание Service Account через UI:
1. Administration → Users and access → Service accounts
2. "Add service account" → deploy_vmrule
3. Добавьте permissions Editor
4. Нажмите "Add service account token"
5. Выберите "No expiration"
6. Скопируйте токен

### **Механизм распределения алертов и перезапуски vmalert**

VictoriaMetrics Operator хранит все правила оповещений (`VMRule`) в ConfigMap'ах с именем `rulefiles`. Из-за ограничения Kubernetes на размер ConfigMap (~1 MiB) при росте количества правил возникает необходимость их дробления, что приводит к пересозданию Pod'а `vmalert` с сохранением состояния алертов.

#### **Детали процесса:**

1.  **Лимит Kubernetes:** Один ConfigMap не может превышать ~1 MiB.
2.  **Логика Operator'a:** В рамках одного цикла согласования (`reconcile`) Operator собирает **все** существующие `VMRule` и пытается упаковать их в ConfigMap `rulefiles-0`.
3.  **При превышении лимита:** Operator разбивает правила на несколько ConfigMap'ов:
    ```
    vm-<release>-rulefiles-0
    vm-<release>-rulefiles-1
    vm-<release>-rulefiles-2
    ...
    ```
4.  **Последствие для Pod:** Каждый новый ConfigMap требует добавления нового `volume` и `volumeMount` в спецификацию Pod'а `vmalert`. Любое изменение списка томов **принудительно вызывает пересоздание Pod'а**.
5.  **Результат:** При добавлении правила, которое "не влезает" в существующие ConfigMap'ы, создается `rulefiles-N`, Pod удаляется и создается заново с обновленным списком томов.

Operator всегда работает с полным набором правил. Он не обновляет ConfigMap'ы "по месту", а пересобирает их заново и при необходимости меняет их количество.

**Сохранение состояния (State Persistence):**

Чтобы предотвратить потерю состояния vmalert по умолчанию настрен на запись и чтение состояния во внешнее хранилище (VictoriaMetrics) через флаги:

*   **`-remoteWrite.url`** (URL до VictoriaMetrics или `vminsert`): `vmalert` будет **сохранять** состояние алертов при каждой оценке в виде временных рядов `ALERTS` и `ALERTS_FOR_STATE`, используя протокол remote-write.
*   **`-remoteRead.url`** (URL до VictoriaMetrics или `vmselect`): При **старте** процесс `vmalert` попытается **восстановить** состояние, запросив ряды `ALERTS_FOR_STATE`.

Подробнее здесь: https://docs.victoriametrics.com/victoriametrics/vmalert/#alerts-state-on-restarts

**Оба флага обязательны для корректного восстановления.** Восстановление происходит только один раз при запуске процесса. Горячая перезагрузка конфигурации правил (`SIGHUP`) не триггерит восстановление состояния.

#### **Как отслеживать текущее состояние ConfigMap'ов**

Используйте скрипт ниже, чтобы увидеть размер ConfigMap'ов.

```bash
kubectl get configmaps -n <your-namespace> -o json | \
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

**Пример вывода и его интерпретация:**
```
vm-vmks-victoria-metrics-k8s-stack-rulefiles-0   980.45 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-1   850.10 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-2   120.50 KB
```
