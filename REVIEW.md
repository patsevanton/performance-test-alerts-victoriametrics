# Отчёт о проверке проекта performance-test-alerts-victoriametrics

Сверка выполнена с helm-чартами из `/home/user/github/VictoriaMetrics/helm-charts` (версии: victoria-metrics-k8s-stack 0.90.2, victoria-logs-cluster 0.2.8, victoria-logs-collector 0.3.7). Версии в README и значениях совпадают с актуальными. Ниже — найденные неточности, ошибки и замечания.

## Критичные ошибки (нарушение правил/соглашений, противоречия)

1. **`vmks-values.yaml:31-33` — некорректный комментарий про replicationFactor.** Комментарий: `replicationFactor = числу vmstorage: при потере 1 ноды данные остаются доступны через реплики`. Это не так: `replicationFactor` определяет, на сколько реплик писать, а не сколько их. Потеря `RF-1` нод не теряет данные только если `RF ≤ N`. Комментарий же утверждает, что при RF=2 потеря одной ноды всегда безопасна — это неверно для случая, когда N=2 и RF=2 (теряется кворум записи). Формулировка вводит в заблуждение и противоречит докам VictoriaMetrics.

2. **`vmks-values.yaml:226-236` — `victoria-metrics-operator` без `spec:`.** В схеме подчарта victoria-metrics-operator ресурсы задаются на верхнем уровне (`resources`, `priorityClassName` — `charts/victoria-metrics-operator/values.yaml:309,329`), а в `vmks-values.yaml` они указаны на верхнем уровне (без `spec:`), что совпадает со схемой. Однако в README `README.md:17` перечислены компоненты «operator», а `victoria-metrics-operator` настроен с одной репликой без PDB — это отмечено в `TODO.md:46`, но не отражено в README. Несоответствий схеме нет, но `operator.replicaCount` не задан (по умолчанию 1, SPOF) — противоречит заявленной отказоустойчивости.

3. **`victoria-logs-cluster-values.yaml:46` — `retentionPeriod: 1d`.** По докам VictoriaLogs минимальный retention — 24h (`1d` = 24h, формально ОК), но дефолт чарта — `7d` (`charts/victoria-logs-cluster/values.yaml:753`). Для стенда допустимо, но стоит отметить, что `1d` — на грани минимума; при кратковременных сбоях vxstorage данные могут быть потеряны быстрее ожидаемого.

4. **`scripts/deploy-apps.sh:11` vs `chart/values.yaml:79` — несоответствие числа алертов.** В `deploy-apps.sh` `BASE_ALERTS_COUNT=10`, `ALERTS_PER_APP=50`, значит `EXTRA_ALERTS_COUNT = 40`. В `chart/values.yaml:79` `alerts.extra.count: 90` (по умолчанию). При запуске `helm upgrade` скрипт передаёт `--set alerts.extra.count=$EXTRA_ALERTS_COUNT` (40), переопределяя дефолт чарта. **Но в `README.md:83` сказано «50 000 алертов» и «40 дополнительных» — это сходится только если запущен скрипт.** Сам по себе `chart/values.yaml` даёт 10+90=100 алертов на app. Это несоответствие между дефолтом чарта и документацией/скриптом — пользователь, развернувший чарт вручную, получит не 50, а 100 алертов на app.

5. **`README.md:83` — ссылка на несуществующий скрипт `deploy-apps-day*.sh`.** В тексте: `общее число задаётся ALERTS_PER_APP в deploy-apps-day*.sh`. Такого скрипта в `scripts/` нет — есть только `deploy-apps.sh`. Опечатка/устаревшая ссылка.

6. **`README.md:85` — арифметика: 1350 × 50 = 67 500, а не 50 000.** В README: «1350 VMRule (50 000 алертов)». Но 1350 × 50 = 67 500. Значение «50 000 алертов» противоречит и `fetch_capacity_snapshots.py:79` (`TARGETS` включает 67500), и `EXPECTED_MAX_ALERTS = TARGET_APPS * ALERTS_PER_APP = 67500`. Где-то ошибка: либо число apps, либо alerts/app, либо итог в README.

## Неточности средней тяжести

7. **`victoria-logs-collector-values.yaml:5-12` — заголовок `VL-Ignore-Fields` в виде списка.** По схеме чарта `remoteWrite[].headers` — это map string→string (`charts/victoria-logs-collector/values.yaml:28`). В values заголовку `VL-Ignore-Fields` присвоен YAML-список из 3 элементов. Helm/шаблонизатор это либо проигнорирует, либо вызовет ошибку рендера — заголовок должен быть строкой (например, comma-separated). Это потенциальная ошибка рендера чарта.

8. **`vmks-values.yaml:60-61, 94-95` — противоречие в описании HTTP-кодов.** В комментарии к vmstorage: «дефолт maxConcurrentRequests=2 даёт **503**», а к vmselect — «даёт **429**». По докам VictoriaMetrics: vmstorage возвращает 429 при `search.maxConcurrentRequests`, а vmselect — 503. Коды перепутаны местами в комментариях.

9. **`vmks-values.yaml` отсутствует блок `defaultRules.groups`** из AGENTS.md (правила для Yandex Managed K8s). Согласно `AGENTS.md`, при установке vmks в Yandex Managed K8s должны быть отключены scrape-job и recording-правила для kube-controller-manager, kube-scheduler, kube-etcd. В `vmks-values.yaml` этого нет, хотя кластер в `k8s.tf` — Yandex Managed K8s. Это прямое нарушение инфраструктурных правил из AGENTS.md:
   - `defaultRules.groups.etcd.enabled: false` — отсутствует
   - `defaultRules.groups.kubernetes-system-scheduler.enabled: false` — отсутствует
   - `defaultRules.groups.kubernetes-system-controller-manager.enabled: false` — отсутствует
   - `defaultRules.groups.kube-scheduler.rules.enabled: false` — отсутствует
   - `kubeControllerManager.enabled: false` — отсутствует
   - `kubeScheduler.enabled: false` — отсутствует
   - `kubeEtcd.enabled: false` — отсутствует

10. **`vmks-values.yaml:191-192` — `vmalert.spec.evaluationInterval: "1m"`.** В README `README.md:366` утверждается `interval 30s` («при interval 30 s»). Но в values стоит `1m`. Несоответствие между README и конфигом.

11. **`vmks-values.yaml:68-71, 106-108` — комментарий про `dedup.minScrapeInterval: 20s`.** Сказано «20s = дефолт chart'а vmagent.spec.scrapeInterval». Действительно, в `charts/victoria-metrics-k8s-stack/values.yaml:1981` `scrapeInterval: 20s`. Но в `vmks-values.yaml` `vmagent.spec.scrapeInterval` не переопределён — значит дефолт 20s. Комментарий корректен, но это хрупкая связь: если кто-то переопределит `scrapeInterval`, нужно не забыть поменять `dedup.minScrapeInterval` в двух местах. Не ошибка, но стоит отметить.

12. **`chart/templates/vmrule.yaml:148-183` — `ExtraAlert` используют `rate/histogram_quantile/avg_over_time`, а не `or vector(...)`.** В `README.md:344-353` утверждается, что 90,7% правил (45 368 из 50 000) используют `(real_query) or vector(fallback)`, а 9,3% — `absent(...)`. Но в шаблоне `vmrule.yaml` **ни одного правила с `or vector(...)` или `absent(...)` нет** — все правила используют прямые выражения (`rate(...) > threshold`, `histogram_quantile(...)` и т.д.). Это серьёзное противоречие между README (на котором построен раздел «Важная оговорка») и фактическим кодом чарта. Либо README описывает другой прогон/версию чарта, либо vmrule.yaml был изменён после прогона.

## Мелкие замечания

13. **`app/go.mod:3` и `app/Dockerfile:1` — Go 1.26.** Актуальная стабильная версия Go (на Aug 2026) — 1.24. `go 1.26` в go.mod и `golang:1.26-trixie` в Dockerfile могут не существовать или быть будущей версией. Стоит проверить, существует ли этот тег на Docker Hub. Если это сделано намеренно (rolling), ок, но это нестандартно.

14. **`app/main.go:46` — `rand.Seed(time.Now().UnixNano())`.** Начиная с Go 1.20 `rand.Seed` устарел (deprecated) — глобальный генератор seeding'ается автоматически. Предупреждение компилятора. Не ошибка, но устаревший код.

15. **`chart/templates/_helpers.tpl` — `golden-signal-app.name = .Release.Name`.** Имя VMRule/VMServiceScrape = имя release. Но в `fetch_capacity_snapshots.py:96-118` pod-паттерны жёстко захардкожены как `vmalert-vmks-victoria-metrics-k8s-stack-.*` — это зависит от release name `vmks` (из `README.md:28`). Если пользователь установит vmks с другим release name, скрипт сломается. Не ошибка, но хрупкость.

16. **`k8s.tf:97-99` — комментарий «8 vCPU / 8GB» против `cores = 4, memory = 8`.** Комментарий в строке 95-96 говорит про «8 vCPU / 8GB», но в `resources` указано `cores = 4`. Комментарий устарел/не соответствует коду (отмечено `# уменьшить` рядом).

17. **`k8s.tf:65` — `size = 25` с комментарием про 16 нод.** Комментарий упоминает «scale до 16 нод» (строка 96), но `size = 25`. Несоответствие между комментариями.

18. **`README.md:144-149` — таблица ресурсов: 1m CPU requests на pod, но `chart/values.yaml:18` — `cpu: 1m`.** Совпадает. Но `memory: 8Mi` requests → 1350 × 8Mi = 10,8 GiB, а в таблице указано «~7.8 GiB». Арифметическая ошибка: 1350 × 8 = 10 800 Mi ≈ 10,55 GiB, не 7,8 GiB. (Возможно, в таблице устаревшие данные при другом значении requests.)

19. **`victoria-logs-collector-values.yaml:27-29` — `podMonitor.vm: true`.** По схеме чарта (`charts/victoria-logs-collector/values.yaml:226`) `podMonitor.vm: false` по умолчанию, установка `true` создаёт VMPodScrape вместо PodMonitor. Корректно для работы с VM operator, но требует, чтобы CRD VMPodScrape уже существовал (устанавливается с vmks). В `README.md:46` сказано про требование CRD VMServiceScrape, но для collector нужен VMPodScrape — небольшое уточнение.

20. **`.gitignore:40` — `scripts/__pycache__/fetch_capacity_snapshots.cpython-313.pyc`**, но в репо есть `scripts/__pycache__/` с этим файлом. Стоило игнорировать весь `__pycache__/` целиком (`scripts/__pycache__/`), а не конкретный файл — иначе при смене версии Python появится новый pyc и попадёт в коммит.

21. **`vmks-values.yaml` нет `templateFiles` / `config` для alertmanager.** Используется дефолтный config с receiver `blackhole` (`charts/victoria-metrics-k8s-stack/values.yaml:1658-1708`). Алерты никуда не отправляются — для нагрузочного теста допустимо, но в README `README.md:335` сказано «Alertmanager в кластерном режиме корректно восстанавливает состояние алертов после рестарта без потерь и без дублирования уведомлений». Без настроенных receivers «уведомления» не отправляются в принципе — утверждение о «дублировании уведомлений» не проверялось.

22. **`TODO.md:15` — `network-hdd` → `k8s.tf:98`**. TODO ссылается на `k8s.tf:98`, но `type = "network-hdd"` сейчас в `k8s.tf:102` (сдвиг из-за правок). Мелочь, но ссылка устарела. Аналогично `TODO.md:11` ссылается на `k8s.tf:77-79`, но `preemptible = true` сейчас в `k8s.tf:82`.

## Итого

Наиболее значимые проблемы:
- **#12** — полное противоречие между README (раздел «Важная оговорка» про `or vector(...)`) и фактическим `chart/templates/vmrule.yaml`. Это ставит под сомнение выводы из теста.
- **#6, #18** — арифметические ошибки в README (67 500 vs 50 000 алертов; 7,8 vs 10,5 GiB).
- **#9** — нарушение явных правил из AGENTS.md для Yandex Managed K8s (не отключены control-plane scrape/rules).
- **#7** — потенциальная ошибка рендера чарта victoria-logs-collector (список в строковом заголовке).
- **#4, #5** — несоответствие дефолтов чарта скрипту/документации; ссылка на несуществующий скрипт.
- **#1, #8, #10** — некорректные/противоречивые комментарии в values и README.
