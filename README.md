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

### **Перераспределение алертов по ConfigMap с названием `rulefiles`**

При увеличении количества алертов

1. **Kubernetes накладывает жёсткий лимит на размер одного ConfigMap — ~1 MB**.
2. Operator **собирает ВСЕ VMRule** в один reconcile-цикл.
3. Когда суммарный размер правил превышает лимит:

   * Operator **разбивает правила на несколько ConfigMap**:

     ```
     vm-<release>-rulefiles-0
     vm-<release>-rulefiles-1
     vm-<release>-rulefiles-2
     ...
     ```
4. **Каждый новый ConfigMap = новый volume + volumeMount** в Pod `vmalert`.
5. **Любое изменение volume/volumeMount → Kubernetes ОБЯЗАН пересоздать Pod**.
6. В момент reconcile:

   * старый Pod удаляется
   * новый Pod создаётся с обновлённым списком ConfigMap’ов

## Важный момент

Alerts state on restarts
vmalert holds alerts state in the memory. Restart of the vmalert process will reset the state of all active alerts in the memory. To prevent vmalert from losing the state on restarts configure it to persist the state to the remote database via the following flags:

-remoteWrite.url - URL to VictoriaMetrics (Single) or vminsert (Cluster). vmalert will persist alerts state to the configured address in the form of time series ALERTS and ALERTS_FOR_STATE via remote-write protocol. These time series can be queried from VictoriaMetrics just as any other time series. The state will be persisted to the configured address on each evaluation.
-remoteRead.url - URL to VictoriaMetrics (Single) or vmselect (Cluster). vmalert will try to restore alerts state from the configured address by querying time series with name ALERTS_FOR_STATE. The restore happens only once when vmalert process starts, and only for the configured rules. Config hot reload doesn’t trigger state restore.
Both flags are required for proper state restoration. Restore process may fail if time series are missing in configured -remoteRead.url, weren’t updated in the last 1h (controlled by -remoteRead.lookback) or received state doesn’t match current vmalert rules configuration. vmalert marks successfully restored rules with restored label in web UI .



Он:

* всегда получает **полный набор правил**
* просто **оператор делит их на несколько ConfigMap**
* и **пересоздаёт Pod**, чтобы примонтировать их

## Почему перезапуск происходит примерно на 1200–1400 алертах

* Средний размер одного alert-правила ≈ **700–900 байт**
* 200 alerts ≈ **140–180 KB**
* ~5–6 файлов → ≈ 1 MB
* при добавлении следующего VMRule:

  * создаётся **новый `rulefiles-N`**
  * Pod **пересоздаётся**

## Как увидеть configmaps и сколько они занимают

Используй этот скрипт — он покажет **размер каждого ConfigMap с rulefiles**:

```bash
kubectl get configmaps -n vmks -o json | \
jq -r '.items[] | {
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
}' | sort -k2 -hr | grep rulefiles
```

Пример вывода:

```
vm-vmks-victoria-metrics-k8s-stack-rulefiles-3   511.65 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-2   471.31 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-0   470.02 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-1   469.89 KB
vm-vmks-victoria-metrics-k8s-stack-rulefiles-4    99.29 KB
```

Как только появляется `rulefiles-4`, Pod **гарантированно будет пересоздан**.
