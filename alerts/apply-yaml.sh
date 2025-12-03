#!/bin/bash

GRAFANA_URL="http://grafana.apatsev.org.ru"
GRAFANA_DASHBOARD_ID="1"   # если нужен конкретный dashboard — укажи ID или убери это поле
GRAFANA_TAG="vmrule-deploy"

echo -n "Введите Grafana API токен: "
read -s GRAFANA_TOKEN
echo ""

echo "Проверка доступа к Grafana..."
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" "$GRAFANA_URL/api/annotations" > /dev/null
if [ $? -ne 0 ]; then
    echo "❌ Ошибка: не удалось подключиться к Grafana. Проверьте токен или URL."
    exit 1
fi
echo "✓ Доступ к Grafana подтверждён."

echo "Поиск YAML файлов в директории vmrules..."

# Собираем и сортируем список файлов
mapfile -t files < <(find vmrules -type f \( -name "*.yaml" -o -name "*.yml" \) | sort -V)

total=${#files[@]}

if [ "$total" -eq 0 ]; then
    echo "YAML файлы не найдены."
    exit 1
fi

echo "Найдено $total YAML файлов (отсортировано):"
printf '%s\n' "${files[@]}"

send_grafana_annotation() {
    local file="$1"
    local now_ts=$(date +%s000) # миллисекунды

    echo " → Отправка аннотации в Grafana..."

    curl -s -X POST "$GRAFANA_URL/api/annotations" \
        -H "Authorization: Bearer $GRAFANA_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{
            \"time\": $now_ts,
            \"tags\": [\"$GRAFANA_TAG\"],
            \"text\": \"Deploy VMRULE: $file\"
        }" > /dev/null

    if [ $? -eq 0 ]; then
        echo " ✓ Аннотация отправлена в Grafana."
    else
        echo " ✗ Ошибка при отправке аннотации."
    fi
}

# Проходим по массиву
for ((i=0; i<total; i++)); do
    file="${files[$i]}"
    index=$((i+1))

    echo "Применяю $index из $total: $file"
    kubectl apply -f "$file"

    if [ $? -eq 0 ]; then
        echo "✓ Успешно применен: $file"
        send_grafana_annotation "$file"
    else
        echo "✗ Ошибка при применении: $file"
        # exit 1
    fi

    if [ "$index" -lt "$total" ]; then
        echo "Ждём 5 минут перед следующим применением..."
        sleep 300
    fi
done

echo "Готово!"
