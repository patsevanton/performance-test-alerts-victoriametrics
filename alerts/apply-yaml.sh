#!/bin/bash
echo "Поиск YAML файлов в директории vmrules..."

# Находим все .yaml и .yml файлы в директории vmrules и поддиректориях
find vmrules -name "*.yaml" -o -name "*.yml" | while read -r file; do
    echo "Применяю: $file"
    kubectl apply -f "$file"
    
    # Проверка успешности выполнения
    if [ $? -eq 0 ]; then
        echo "✓ Успешно применен: $file"
    else
        echo "✗ Ошибка при применении: $file"
        # Можно добавить exit 1 для остановки при ошибке
        # exit 1
    fi
done

echo "Готово!"