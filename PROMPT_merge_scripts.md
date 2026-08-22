# Задача: объединение scripts/deploy-apps.sh и scripts/fetch_capacity_snapshots.py

## Контекст

Сейчас деплой приложений и сбор снимков capacity разделены:
- `scripts/deploy-apps.sh` — последовательно (без пауз, `helm upgrade --install --wait=false`) ставит app-1..app-1700, по ~9с на релиз.
- `scripts/fetch_capacity_snapshots.py` — параллельно опрашивает `count(ALERTS)` у vmselect каждые `POLL_INTERVAL` секунд и при достижении порогов из `TARGETS` делает instant-снимок метрик из `QUERIES`.

Проблема: на росте числа алертов `count(ALERTS)` упирается в таймауты vmselect (120с) из-за того, что vmstorage/vmselect перегружены оценкой ~1700 VMRule и ingestion'ом `ALERTS`/`ALERTS_FOR_STATE`. Скрипт фиксирует `query failed: timed out` и не двигается по порогам.

Решение: заменить непрерывный опрос `count(ALERTS)` на **оценку** через число установленных app, откалиброванную двумя реальными замерами `count(ALERTS)` на малой нагрузке (где таймаутов нет).

## Что нужно сделать

Объединить логику обоих скриптов в **один** Python-скрипт (заменить `scripts/fetch_capacity_snapshots.py` или создать новый файл — на усмотрение исполнителя; deploy-apps.sh можно оставить как есть или удалить, если объединённый скрипт полностью его подменяет). Исполнитель сам выбирает язык/формат — но сохранить все возможности deploy-apps.sh (чтение `app-names.txt`, параметры `IMAGE_REPO`/`IMAGE_TAG`/`ALERTS_PER_APP`/`BASE_ALERTS_COUNT`/`EXTRA_ALERTS_COUNT`/`START_INDEX`/`TARGET_APPS`/`APP_TENANTS`/`APP_ROUTES`/`APP_HIST_BUCKETS`/`APP_REGION`/`APP_VERSION`/`CHART_PATH`/`NAMES_FILE`, вызов `helm upgrade --install` с теми же `--set` флагами, `--wait=false`, `--timeout 2m`, `kubectl create namespace`).

### Логика работы объединённого скрипта

1. **Старт** (аналогично `deploy-apps.sh`): чтение `app-names.txt`, формирование списка `apps[START_INDEX-1 : START_INDEX-1+TARGET_APPS]`, печать плана.

2. **Цикл установки app по одному**, без пауз между установками (как `deploy-apps.sh`). Для каждого установленного app известно его порядковый номер `installed` (счётчик цикла, не `kubectl get pod` — helm install с `--wait=false` не гарантирует Ready пода, но счётчик релизов детерминирован).

3. **Калибровка коэффициента `k`** — два реальных запроса `count(ALERTS)` к vmselect:
   - при `installed == 10`: запросить `count(ALERTS)`, вычислить `k1 = count(ALERTS) / (10 * ALERTS_PER_APP)`.
   - при `installed == 20`: запросить `count(ALERTS)`, вычислить `k2 = count(ALERTS) / (20 * ALERTS_PER_APP)`.
   - После app-20: `k = (k1 + k2) / 2`. Если один из замеров не удался (таймаут/нет данных), использовать удвоенный успешный. Если оба не удались — `k = 1.0` с предупреждением в stderr.

   Для запросов калибровки использовать `fetch_one(now, "count(ALERTS)")` с тем же `timeout=120` (на 10–20 app алертов ~500–1000, таймаутов не будет). Повторить до 3 раз с паузой `POLL_INTERVAL` при ошибке.

4. **Оценка числа алертов после app-20**:
   ```python
   alerts_count = installed * ALERTS_PER_APP * k
   ```
   `count(ALERTS)` больше не запрашивается (кроме двух точек калибровки).

5. **До app-20** (app-1..app-19): оценку не считать, снимки не делать. Пороги `TARGETS` меньше `20*ALERTS_PER_APP` игнорируются до калибровки; их зафиксирует после калибровки отдельный проход по пропущенным порогам (см. п.7).

6. **Фиксация снимков по порогам** (аналог `fetch_capacity_snapshots.py.main`):
   - При `alerts_count >= TARGETS[next_idx]`:
     - сделать instant-снимок всех метрик из `QUERIES` (`fetch_snapshot(now)` через `ThreadPoolExecutor(max_workers=16)`) — это тяжёлые, но не `count(ALERTS)` запросы;
     - в снимок записать `"alerts_count": int(alerts_count)`, `"alerts_count_estimated": True`, `"k": k`, `"installed": installed`;
     - выждать `MIN_SNAPSHOT_GAP` секунд перед следующей проверкой порога (чтобы rate-метрики устаканились);
     - `next_idx += 1`.

7. **Пропущенные пороги < 20*ALERTS_PER_APP** (порог 500 = 10*50): после завершения калибровки (app-20) сделать ретроспективный снимок для каждого ещё не достигнутого порога из `TARGETS` со значением `< 20*ALERTS_PER_APP`. Метрики снимать в момент app-20 (текущее состояние vmselect/vmstorage), в `alerts_count` записать оценку `installed*ALERTS_PER_APP*k`, пометить `"retrospective": True`, `"alerts_count_estimated": True`. Если таких порогов нет (все >= 20*50) — шаг пропускается.

8. **Финал после последнего порога (85000 = 1700*50)**:
   - дождаться установки `TARGET_APPS` приложений (цикл установки завершается сам, доп. ожидания не нужно — `installed == TARGET_APPS` после цикла);
   - выждать `SETTLE_WAIT` секунд (по умолчанию 600);
   - сделать финальный снимок «после через 10 мин установки {TARGET_APPS} app» с `"alerts_count": int(TARGET_APPS * ALERTS_PER_APP * k)`, `"alerts_count_estimated": True`.

9. **Вывод**: `capacity_snapshots.json` и `capacity_snapshots.txt` (рядом со скриптом) — формат как в текущем `fetch_capacity_snapshots.py`. Дополнительно в JSON включить поля калибровки:
   ```json
   "calibration": {
     "k1": <float>, "k1_installed": 10, "k1_count_alerts": <int>,
     "k2": <float>, "k2_installed": 20, "k2_count_alerts": <int>,
     "k": <float>
   }
   ```

### Переменные окружения

Сохранить все из `deploy-apps.sh` и `fetch_capacity_snapshots.py`:
- `CHART_PATH`, `NAMES_FILE`, `IMAGE_REPO`, `IMAGE_TAG`, `ALERTS_PER_APP`, `BASE_ALERTS_COUNT`, `EXTRA_ALERTS_COUNT`, `START_INDEX`, `TARGET_APPS`, `APP_TENANTS`, `APP_ROUTES`, `APP_HIST_BUCKETS`, `APP_REGION`, `APP_VERSION` (из deploy-apps.sh);
- `POLL_INTERVAL` (по умолчанию 10), `MIN_SNAPSHOT_GAP` (120), `SETTLE_WAIT` (600), `VMSELECT_URL`, `RELEASE_NAME` (по умолчанию `vmks`) — из fetch_capacity_snapshots.py.

### Сигнал SIGINT

Сохранить мягкую обработку Ctrl-C: прекратить установку/опрос, выгрузить собранные снимки.

### Что НЕ менять

- `TARGETS`, `QUERIES`, `COUNT_QUERY`, `BASE` (resolve через `terraform output -raw vmselect_url` или env `VMSELECT_URL`), `_SSL` — взять без изменений из `fetch_capacity_snapshots.py`.
- Логику `fetch_one`, `fetch_snapshot`, `qval`, форматтеры (`fmt_cpu_m`, `fmt_mem_mi`, `fmt_rps`, `fmt_p99_sec`, `fmt_sec`, `fmt_count`, `fmt_rel`), `render_table`, `write_outputs` — взять без изменений.
- Параллельность снимков `ThreadPoolExecutor(max_workers=16)` — без изменений.

### Проверка после реализации

- `python3 -m py_compile scripts/<новый_скрипт>.py` — без ошибок.
- `python3 scripts/<новый_скрипт>.py --help` или запуск без аргументов на пустом стенде — должен печатать план установки и значение `k` после app-10/app-20 (если есть `app-names.txt` и доступен vmselect).
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
