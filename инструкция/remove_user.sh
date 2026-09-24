#!/bin/bash

CONTAINER="amnezia-awg2"
USERNAME=$1

if [ -z "$USERNAME" ]; then
    echo "❌ Ошибка: Не указано имя пользователя." >&2
    exit 1
fi

CONF_PATH=$(docker exec -i $CONTAINER find / -name awg0.conf -o -name wg0.conf -type f 2>/dev/null | head -n 1 | tr -d '\r')

if [ -z "$CONF_PATH" ]; then
    echo "❌ Ошибка: Конфигурационный файл не найден." >&2
    exit 1
fi

# Находим PublicKey пользователя по его юзернейму в комментариях
PUB_KEY=$(docker exec -i $CONTAINER grep -A 2 "# Peer: $USERNAME" "$CONF_PATH" 2>/dev/null | grep -i "PublicKey" | cut -d '=' -f2 | tr -d ' \r\n')

if [ -z "$PUB_KEY" ]; then
    echo "⚠️ Ключ для $USERNAME не найден. Возможно, он уже удален."
else
    # Удаляем на горячую из активных сессий awg0
    docker exec -i $CONTAINER awg set awg0 peer "$PUB_KEY" remove
fi

# Полностью вырезаем блок конфигурации пользователя из файла
docker exec -i $CONTAINER sed -i "/# Peer: $USERNAME/,/AllowedIPs/d" "$CONF_PATH"

echo "✅ Пользователь $USERNAME успешно удален."