#!/bin/bash

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

# Проходим по массиву
for ((i=0; i<total; i++)); do
    file="${files[$i]}"
    index=$((i+1))

    echo "Применяю $index из $total: $file"
    kubectl apply -f "$file"

    if [ $? -eq 0 ]; then
        echo "✓ Успешно применен: $file"
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
