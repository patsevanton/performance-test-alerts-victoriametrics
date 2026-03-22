#!/usr/bin/env bash
# =============================================================================
# apply-yaml.sh — поочерёдное применение VMRule-ресурсов через kubectl apply.
#
# Назначение:
#   Находит все YAML-файлы в подкаталоге vmrules/ и применяет их
#   по одному с фиксированной паузой (APPLY_TIMEOUT) между вызовами.
#   Таким образом нагрузка на Kubernetes API и vmalert растягивается
#   на предсказуемое время, что позволяет параллельному скрипту
#   monitor-batch.sh отслеживать деградацию.
#
# Предусловия:
#   - kubectl сконфигурирован и указывает на нужный кластер/namespace;
#   - каталог vmrules/ содержит ровно EXPECTED_COUNT файлов *.yaml/*.yml
#     (генерируются скриптом generate_alerts.py);
#   - утилиты: bash ≥ 4 (mapfile), find, sort, awk.
#
# Поведение при ошибках (set -euo pipefail):
#   - Любая неудачная команда (включая kubectl apply) прерывает скрипт;
#   - Неинициализированная переменная — ошибка;
#   - Ошибка внутри пайплайна — ошибка.
# =============================================================================
set -euo pipefail

# ---- Конфигурация ----

# Каталог самого скрипта (для построения пути к vmrules/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Подкаталог с YAML-файлами VMRule (относительно SCRIPT_DIR)
VMRULES_SUBDIR="vmrules"
VMRULES_DIR="${SCRIPT_DIR}/${VMRULES_SUBDIR}"

# Пауза между последовательными kubectl apply (секунды).
# Определяет общее время этапа: total ≈ APPLY_TIMEOUT × (кол-во файлов − 1).
APPLY_TIMEOUT=30

# Ожидаемое число YAML-файлов; скрипт завершается с ошибкой,
# если фактическое число не совпадает. Защита от неполной генерации.
EXPECTED_COUNT=500

# ---- Вспомогательные функции ----

# Преобразует количество секунд в человекочитаемую строку на русском языке.
# Примеры: 7260 → "2 ч 1 мин", 90 → "1 мин 30 с", 42 → "42 с".
# Реализовано на awk, чтобы избежать зависимости от bc и внешних утилит.
sec_to_ru() {
  awk -v s="$1" 'BEGIN {
    if (s >= 3600) {
      h = int(s / 3600)
      rest = s - h * 3600
      m = int(rest / 60)
      if (m == 0) printf "%d ч", h
      else printf "%d ч %d мин", h, m
    } else if (s >= 60) {
      m = int(s / 60)
      sec = int(s - m * 60 + 0.5)
      if (sec == 60) { m++; sec = 0 }
      printf "%d мин %d с", m, sec
    } else {
      printf "%.0f с", s
    }
  }'
}

# ---- Валидация входных данных ----

# Проверяем существование каталога с правилами
if [ ! -d "$VMRULES_DIR" ]; then
  echo "Каталог не найден: $VMRULES_DIR" >&2
  exit 1
fi

# Собираем список файлов *.yaml / *.yml и сортируем «натурально» (sort -V),
# чтобы порядок был предсказуемым (rule-1.yaml, rule-2.yaml, …, rule-500.yaml).
# mapfile загружает пути в массив files.
echo "Поиск YAML файлов в ${VMRULES_DIR}..."
mapfile -t files < <(find "$VMRULES_DIR" -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V)
total=${#files[@]}

# Пустой каталог — скорее всего забыли запустить generate_alerts.py
if [ "$total" -eq 0 ]; then
  echo "YAML файлы не найдены." >&2
  exit 1
fi

# Количество файлов должно точно совпадать с ожидаемым — защита от
# частичной генерации или случайного попадания лишних файлов.
if [ "$total" -ne "$EXPECTED_COUNT" ]; then
  echo "Ожидалось ${EXPECTED_COUNT} файлов, найдено ${total}." >&2
  exit 1
fi

# ---- Расчёт и вывод прогноза времени ----

sleep_interval="$APPLY_TIMEOUT"

# Общее расчётное время: пауза × (файлов − 1), т.к. после последнего файла паузы нет
total_time_sec=$(awk -v d="$sleep_interval" -v c="$total" 'BEGIN { printf "%.0f", d * (c - 1) }')
total_ru=$(sec_to_ru "$total_time_sec")
sleep_ru=$(sec_to_ru "$sleep_interval")
echo "Найдено ${total} файлов. Таймаут между apply: ~${sleep_ru}, общее время: ~${total_ru}."

# ---- Основной цикл применения ----
# Файлы применяются строго по одному, чтобы имитировать постепенное
# добавление alerting-правил, как это происходит в реальной эксплуатации.
# Между файлами выдерживается пауза sleep_interval, кроме последнего.
for ((i = 0; i < total; i++)); do
  file="${files[i]}"
  index=$((i + 1))

  # Применяем один VMRule-ресурс; при ошибке скрипт завершится (set -e)
  kubectl apply -f "$file"
  echo "  ✓ ${index}/${total}: ${file}"

  # После последнего файла пауза не нужна
  if [ "$index" -lt "$total" ]; then
    sleep "$sleep_interval"
  fi
done

echo "Готово! Применено ${total} VMRule."
