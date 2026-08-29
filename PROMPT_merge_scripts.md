# Задача: объединение scripts/deploy-apps.sh и scripts/fetch_capacity_snapshots.py

## Контекст

Сейчас деплой приложений и сбор снимков capacity разделены:
- `scripts/deploy-apps.sh` — последовательно (`helm upgrade --install --wait --timeout 2m`) ставит app-1..app-1700.
- `scripts/fetch_capacity_snapshots.py` — параллельно опрашивает количество rules в `VMRule` каждые `POLL_INTERVAL` секунд и при достижении порогов из `TARGETS` делает instant-снимок метрик из `QUERIES`.

Источник временных рядов `ALERTS` не используется для определения порогов: их число зависит от результата выражений правил и может отличаться от числа настроенных rules.

Решение: считать пороговое число настроенных alert rules напрямую через Kubernetes API: количество `VMRule`, умноженное на количество rules внутри каждого объекта.

## Что нужно сделать

Объединить логику обоих скриптов в **один** Python-скрипт (заменить `scripts/fetch_capacity_snapshots.py` или создать новый файл — на усмотрение исполнителя; deploy-apps.sh можно оставить как есть или удалить, если объединённый скрипт полностью его подменяет). Исполнитель сам выбирает язык/формат — но сохранить все возможности deploy-apps.sh (чтение `app-names.txt`, параметры `IMAGE_REPO`/`IMAGE_TAG`/`ALERTS_PER_APP`/`BASE_ALERTS_COUNT`/`EXTRA_ALERTS_COUNT`/`START_INDEX`/`TARGET_APPS`/`APP_TENANTS`/`APP_ROUTES`/`APP_HIST_BUCKETS`/`APP_REGION`/`APP_VERSION`/`CHART_PATH`/`NAMES_FILE`, вызов `helm upgrade --install` с теми же `--set` флагами, `--wait`, `--timeout 2m`, `kubectl create namespace`).

### Логика работы объединённого скрипта

1. **Старт** (аналогично `deploy-apps.sh`): чтение `app-names.txt`, формирование списка `apps[START_INDEX-1 : START_INDEX-1+TARGET_APPS]`, печать плана.

2. **Цикл установки app по одному**, без дополнительных пауз между установками. Для каждого успешно установленного app известно его порядковый номер `installed`.

3. **Подсчёт правил** — после каждого успешного deploy запросить `VMRule` через Kubernetes API и посчитать количество rules внутри всех объектов. Для одинаковых объектов это `количество VMRule × количество rules внутри VMRule`, для неодинаковых — точная сумма.

4. **Фиксация порогов** выполняется сразу после получения `rules_count`; отдельная калибровка и запросы к vmselect для подсчёта `ALERTS` не нужны.

6. **Фиксация снимков по порогам** (аналог `fetch_capacity_snapshots.py.main`):
   - При `alerts_count >= TARGETS[next_idx]`:
      - сделать instant-снимок всех метрик из `QUERIES` (`fetch_snapshot(now)` через `ThreadPoolExecutor(max_workers=16)`);
      - в снимок записать `"alerts_count": int(rules_count)`, `"alerts_count_estimated": False`, `"installed": installed`;
     - выждать `MIN_SNAPSHOT_GAP` секунд перед следующей проверкой порога (чтобы rate-метрики устаканились);
     - `next_idx += 1`.

7. **Финал после последнего порога (85000 = 1700*50)**:
   - дождаться установки `TARGET_APPS` приложений (цикл установки завершается сам, доп. ожидания не нужно — `installed == TARGET_APPS` после цикла);
   - выждать `SETTLE_WAIT` секунд (по умолчанию 600);
    - сделать финальный снимок «после через 10 мин установки {TARGET_APPS} app» с фактическим `rules_count` и `"alerts_count_estimated": False`.

9. **Вывод**: `capacity_snapshots.json` и `capacity_snapshots.txt` (рядом со скриптом) — формат как в текущем `fetch_capacity_snapshots.py`. В JSON сохранить последнее состояние подсчёта rules:
   ```json
   "rule_count": {
     "vmrule_count": <int>,
     "rules_per_vmrule": <int>
   }
   ```

### Переменные окружения

Сохранить все из `deploy-apps.sh` и `fetch_capacity_snapshots.py`:
- `CHART_PATH`, `NAMES_FILE`, `IMAGE_REPO`, `IMAGE_TAG`, `ALERTS_PER_APP`, `BASE_ALERTS_COUNT`, `EXTRA_ALERTS_COUNT`, `START_INDEX`, `TARGET_APPS`, `APP_TENANTS`, `APP_ROUTES`, `APP_HIST_BUCKETS`, `APP_REGION`, `APP_VERSION` (из deploy-apps.sh);
- `POLL_INTERVAL` (по умолчанию 10), `MIN_SNAPSHOT_GAP` (120), `SETTLE_WAIT` (600), `VMSELECT_URL`, `RELEASE_NAME` (по умолчанию `vmks`) — из fetch_capacity_snapshots.py.

### Сигнал SIGINT

Сохранить мягкую обработку Ctrl-C: прекратить установку/опрос, выгрузить собранные снимки.

### Что НЕ менять

- `TARGETS`, `QUERIES`, `BASE` (resolve через `terraform output -raw vmselect_url` или env `VMSELECT_URL`), `_SSL` — взять без изменений из `fetch_capacity_snapshots.py`.
- Логику `fetch_one`, `fetch_snapshot`, `qval`, форматтеры (`fmt_cpu_m`, `fmt_mem_mi`, `fmt_rps`, `fmt_p99_sec`, `fmt_sec`, `fmt_count`, `fmt_rel`), `render_table`, `write_outputs` — взять без изменений.
- Параллельность снимков `ThreadPoolExecutor(max_workers=16)` — без изменений.

### Проверка после реализации

- `python3 -m py_compile scripts/<новый_скрипт>.py` — без ошибок.
- `python3 scripts/<новый_скрипт>.py --help` или запуск без аргументов на пустом стенде — должен печатать план установки и количество rules в `VMRule` после deploy (если есть `app-names.txt` и доступен кластер).
- Линтеры/форматтеры, если есть в репозитории — запустить.

### Файлы для изучения перед реализацией

- `scripts/deploy-apps.sh` — параметры и цикл helm install.
- `scripts/fetch_capacity_snapshots.py` — `QUERIES`, `TARGETS`, `fetch_snapshot`, `render_table`, `write_outputs`, логика финального снимка.
- `chart/templates/vmrule.yaml` — какие правила создаются (для понимания `ALERTS_PER_APP`).
- `chart/values.yaml` — `alerts.*` (включены ли правила по умолчанию).
- `vmks-values.yaml` — `evaluationInterval: 1m`, `replicaCount: 2` у vmalert, `scrapeInterval: 20s` у vmagent.
- `AGENTS.md` (корневой) — правила коммитов (префикс `feat` только для Go/Dockerfile/image; bump версий image без изменения Go/Dockerfile не делается). Для этой задачи скорее всего `refactor` или `chore` — исполнитель сам определит по содержанию изменений.
- `AGENTS.md` (`~/.config/opencode/AGENTS.md`) — коммиты существительным/отглагольным существительным.

### Уточнения, которые исполнитель может задать пользователю

- Имя нового файла: заменить `scripts/fetch_capacity_snapshots.py` или создать `scripts/deploy_and_snapshot.py` (или иное), а `deploy-apps.sh` оставить/удалить?
- Удалять ли `scripts/deploy-apps.sh` после объединения или оставить как резервный путь?
