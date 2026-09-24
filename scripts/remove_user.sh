#!/bin/bash

CONTAINER="amnezia-awg2"
USERNAME=$1

if [ -z "$USERNAME" ]; then
    echo "❌ Ошибка: Не указано имя пользователя." >&2
    exit 1
fi

# Автоматически определяем рабочий интерфейс (awg0 или wg0)
INTERFACE=$(docker exec -i $CONTAINER sh -c "awg show interfaces 2>/dev/null | tr -d '\r'" | awk '{print $1}')
if [ -z "$INTERFACE" ]; then
    INTERFACE="awg0"
fi

CONF_PATH=$(docker exec -i $CONTAINER find / -name "$INTERFACE.conf" -o -name wg0.conf -o -name awg0.conf -type f 2>/dev/null | head -n 1 | tr -d '\r')

if [ -z "$CONF_PATH" ]; then
    echo "❌ Ошибка: Конфиг не найден." >&2
    exit 1
fi

# ИЗВЛЕКАЕМ КЛЮЧИ: Убрали удаление \n, теперь ключи идут списком, если их несколько
PUB_KEYS=$(docker exec -i $CONTAINER grep -A 2 "# Peer: $USERNAME" "$CONF_PATH" 2>/dev/null | grep -i "PublicKey" | cut -d '=' -f2- | tr -d '\r')

if [ -z "$PUB_KEYS" ]; then
    echo "⚠️ Ключ для $USERNAME не найден в файле. Возможно, он уже был вырезан."
else
    # ЗАПУСКАЕМ ЦИКЛ: Убиваем каждую найденную сессию по отдельности
    for PK in $PUB_KEYS; do
        # Очищаем ключ от случайных пробелов
        PK=$(echo "$PK" | xargs)
        if [ -n "$PK" ]; then
            echo "🗑 Отключаю сессию ключа $PK на интерфейсе $INTERFACE..."
            docker exec -i $CONTAINER awg set $INTERFACE peer "$PK" remove
            if [ $? -eq 0 ]; then
                echo "✅ Сессия сброшена."
            else
                echo "❌ Ошибка сброса сессии."
            fi
        fi
    done
fi

# Вырезаем ВСЕ блоки пользователя из файла конфигурации разом
echo "📄 Вычищаю данные из файла $CONF_PATH..."
docker exec -i $CONTAINER sed -i "/# Peer: $USERNAME/,/AllowedIPs/d" "$CONF_PATH"

echo "🏁 Скрипт завершил работу. Пользователь $USERNAME полностью уничтожен."
