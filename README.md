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

Из директории alerts запустите скрипт ./generate_alerts.py

# Создание Service Account через UI:
1. Administration → Users and access → Service accounts
2. "Add service account" → deploy_vmrule
3. Добавьте permissions Editor
4. Нажмите "Add service account token"
5. Выберите "No expiration"
6. Скопируйте токен

**Краткая инструкция по добавлению аннотации в Grafana:**

1. **Откройте панель управления (Dashboard)**  
   Перейдите в нужный дашборд, нажмите ⚙️ (Settings) → **Annotations**.

2. **Добавьте новую аннотацию**  
   Нажмите **+ New Annotation**.

3. **Заполните основные настройки:**  
   - **Name** — название аннотации (например, `vmrule-deploy`).  
   - **Data source** — выберите источник данных (например, `-- Grafana --` для встроенных).  
   - **Query** — оставьте **Annotations & Alerts**.  
   - **Filter by Tags** — можно указать теги для фильтрации (опционально).

4. **Настройте внешний вид:**  
   - **Color** — выберите цвет маркеров событий.  
   - **Show in** — выберите **All panels**, чтобы аннотация отображалась на всех графиках.

5. **Дополнительные опции (опционально):**  
   - **Hidden** — скрыть аннотацию из интерфейса (можно включить/выключить в дашборде).  
   - **Max link** — настройка ссылки (если нужно).  
   - **Tags** — укажите теги в формате `ключ:значение` для фильтрации событий.

6. **Сохраните**  
   Нажмите **Save** внизу формы, затем **Save dashboard**.

**Готово!** Аннотация будет появляться на графиках при обновлении дашборда.

## Заключение
