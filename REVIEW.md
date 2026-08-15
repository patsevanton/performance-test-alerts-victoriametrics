# Отчёт о проверке проекта performance-test-alerts-victoriametrics

Сверка выполнена с helm-чартами из `/home/user/github/VictoriaMetrics/helm-charts` (версия victoria-metrics-k8s-stack 0.90.2). Версии в README и значениях совпадают с актуальными. Ниже — найденные неточности, ошибки и замечания.

## Критичные ошибки (нарушение правил/соглашений, противоречия)


## Неточности средней тяжести

## Мелкие замечания

15. **`chart/templates/_helpers.tpl` — `golden-signal-app.name = .Release.Name`.** Имя VMRule/VMServiceScrape = имя release. Но в `fetch_capacity_snapshots.py:96-118` pod-паттерны жёстко захардкожены как `vmalert-vmks-victoria-metrics-k8s-stack-.*` — это зависит от release name `vmks` (из `README.md:28`). Если пользователь установит vmks с другим release name, скрипт сломается. Не ошибка, но хрупкость.

21. **`vmks-values.yaml` нет `templateFiles` / `config` для alertmanager.** Используется дефолтный config с receiver `blackhole` (`charts/victoria-metrics-k8s-stack/values.yaml:1658-1708`). Алерты никуда не отправляются — для нагрузочного теста допустимо, но в README `README.md:335` сказано «Alertmanager в кластерном режиме корректно восстанавливает состояние алертов после рестарта без потерь и без дублирования уведомлений». Без настроенных receivers «уведомления» не отправляются в принципе — утверждение о «дублировании уведомлений» не проверялось.

22. **`TODO.md:15` — `network-hdd` → `k8s.tf:98`**. TODO ссылается на `k8s.tf:98`, но `type = "network-hdd"` сейчас в `k8s.tf:102` (сдвиг из-за правок). Мелочь, но ссылка устарела. Аналогично `TODO.md:11` ссылается на `k8s.tf:77-79`, но `preemptible = true` сейчас в `k8s.tf:82`.

## Итого

Наиболее значимые проблемы:
- **#12** — полное противоречие между README (раздел «Важная оговорка» про `or vector(...)`) и фактическим `chart/templates/vmrule.yaml`. Это ставит под сомнение выводы из теста.
- **#9** — нарушение явных правил из AGENTS.md для Yandex Managed K8s (не отключены control-plane scrape/rules).
- **#4, #5** — несоответствие дефолтов чарта скрипту/документации; ссылка на несуществующий скрипт.
- **#1, #8, #10** — некорректные/противоречивые комментарии в values и README.
