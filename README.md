# Нагрузочное тестирование алертами большим кол-вом алертов в victoriametrics

## Введение — какую проблему решаем


## Установка victoria-metrics-k8s-stack

Добавим Helm репозиторий и установим VictoriaMetrics stack:

```bash
helm repo add vm https://victoriametrics.github.io/helm-charts/
helm repo update

helm upgrade --install vmks vm/victoria-metrics-k8s-stack \
  --namespace vmks --create-namespace \
  --values vmks-values.yaml
```


После установки, Grafana будет доступна по адресу http://grafana.apatsev.org.ru

Получение пароля grafana для admin юзера
```shell
kubectl get secret vmks-grafana -n vmks -o jsonpath='{.data.admin-password}' | base64 --decode
```

## Заключение
