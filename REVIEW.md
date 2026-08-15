# Отчёт о проверке проекта performance-test-alerts-victoriametrics

Сверка выполнена с helm-чартами из `/home/user/github/VictoriaMetrics/helm-charts` (версия victoria-metrics-k8s-stack 0.90.2). Версии в README и значениях совпадают с актуальными. Ниже — найденные неточности, ошибки и замечания.

## Критичные ошибки (нарушение правил/соглашений, противоречия)


## Неточности средней тяжести

12. **`chart/templates/vmrule.yaml:148-183` — `ExtraAlert` используют `rate/histogram_quantile/avg_over_time`, а не `or vector(...)`.** В `README.md:344-353` утверждается, что 90,7% правил (45 368 из 50 000) используют `(real_query) or vector(fallback)`, а 9,3% — `absent(...)`. Но в шаблоне `vmrule.yaml` **ни одного правила с `or vector(...)` или `absent(...)` нет** — все правила используют прямые выражения (`rate(...) > threshold`, `histogram_quantile(...)` и т.д.). Это серьёзное противоречие между README (на котором построен раздел «Важная оговорка») и фактическим кодом чарта. Либо README описывает другой прогон/версию чарта, либо vmrule.yaml был изменён после прогона.

## Мелкие замечания

14. **`app/main.go:46` — `rand.Seed(time.Now().UnixNano())`.** Начиная с Go 1.20 `rand.Seed` устарел (deprecated) — глобальный генератор seeding'ается автоматически. Предупреждение компилятора. Не ошибка, но устаревший код.

15. **`chart/templates/_helpers.tpl` — `golden-signal-app.name = .Release.Name`.** Имя VMRule/VMServiceScrape = имя release. Но в `fetch_capacity_snapshots.py:96-118` pod-паттерны жёстко захардкожены как `vmalert-vmks-victoria-metrics-k8s-stack-.*` — это зависит от release name `vmks` (из `README.md:28`). Если пользователь установит vmks с другим release name, скрипт сломается. Не ошибка, но хрупкость.

18. **`README.md:144-149` — таблица ресурсов: 1m CPU requests на pod, но `chart/values.yaml:18` — `cpu: 1m`.** Совпадает. Но `memory: 8Mi` requests → 1350 × 8Mi = 10,8 GiB, а в таблице указано «~7.8 GiB». Арифметическая ошибка: 1350 × 8 = 10 800 Mi ≈ 10,55 GiB, не 7,8 GiB. (Возможно, в таблице устаревшие данные при другом значении requests.)

20. **`.gitignore:40` — `scripts/__pycache__/fetch_capacity_snapshots.cpython-313.pyc`**, но в репо есть `scripts/__pycache__/` с этим файлом. Стоило игнорировать весь `__pycache__/` целиком (`scripts/__pycache__/`), а не конкретный файл — иначе при смене версии Python появится новый pyc и попадёт в коммит.

21. **`vmks-values.yaml` нет `templateFiles` / `config` для alertmanager.** Используется дефолтный config с receiver `blackhole` (`charts/victoria-metrics-k8s-stack/values.yaml:1658-1708`). Алерты никуда не отправляются — для нагрузочного теста допустимо, но в README `README.md:335` сказано «Alertmanager в кластерном режиме корректно восстанавливает состояние алертов после рестарта без потерь и без дублирования уведомлений». Без настроенных receivers «уведомления» не отправляются в принципе — утверждение о «дублировании уведомлений» не проверялось.

22. **`TODO.md:15` — `network-hdd` → `k8s.tf:98`**. TODO ссылается на `k8s.tf:98`, но `type = "network-hdd"` сейчас в `k8s.tf:102` (сдвиг из-за правок). Мелочь, но ссылка устарела. Аналогично `TODO.md:11` ссылается на `k8s.tf:77-79`, но `preemptible = true` сейчас в `k8s.tf:82`.

## Итого

Наиболее значимые проблемы:
- **#12** — полное противоречие между README (раздел «Важная оговорка» про `or vector(...)`) и фактическим `chart/templates/vmrule.yaml`. Это ставит под сомнение выводы из теста.
- **#6, #18** — арифметические ошибки в README (67 500 vs 50 000 алертов; 7,8 vs 10,5 GiB).
- **#9** — нарушение явных правил из AGENTS.md для Yandex Managed K8s (не отключены control-plane scrape/rules).
- **#4, #5** — несоответствие дефолтов чарта скрипту/документации; ссылка на несуществующий скрипт.
- **#1, #8, #10** — некорректные/противоречивые комментарии в values и README.
