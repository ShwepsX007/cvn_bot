#!/bin/bash

CONTAINER="amnezia-awg2"
USERNAME=$1

if [ -z "$USERNAME" ]; then
    echo "❌ Ошибка: Не указано имя пользователя." >&2
    exit 1
fi

# ПРЕВЕНТИВНАЯ ОЧИСТКА: Уничтожаем зависшие «призрачные» конфиги
bash /root/remove_user.sh "$USERNAME" >/dev/null 2>&1

# Автоматически находим конфигурационный файл внутри контейнера
CONF_PATH=$(docker exec -i $CONTAINER find / -name awg0.conf -o -name wg0.conf -type f 2>/dev/null | head -n 1 | tr -d '\r')

if [ -z "$CONF_PATH" ]; then
    echo "❌ Ошибка: Конфигурационный файл awg0.conf не найден в контейнере." >&2
    exit 1
fi

# 1. Генерируем ключи клиента
PRIV_KEY=$(docker exec -i $CONTAINER awg genkey 2>/dev/null | tr -d '\r')
PUB_KEY=$(echo "$PRIV_KEY" | docker exec -i $CONTAINER awg pubkey 2>/dev/null | tr -d '\r')
PSK=$(docker exec -i $CONTAINER awg genpsk 2>/dev/null | tr -d '\r')

# 2. Получаем сетевые данные сервера
SERVER_PUBKEY=$(docker exec -i $CONTAINER awg show awg0 public-key 2>/dev/null | tr -d '\r')
SERVER_PORT=$(docker exec -i $CONTAINER awg show awg0 listen-port 2>/dev/null | tr -d '\r')
SERVER_IP=$(curl -s4 icanhazip.com 2>/dev/null | tr -d '\r')

# 3. Высчитываем свободный IP для клиента в подсети awg0
SUBNET=$(docker exec -i $CONTAINER ip -4 addr show awg0 2>/dev/null | grep -oP '(?<=inet )\d+\.\d+\.\d+' | head -n 1 | tr -d '\r')
LAST_OCTET=$(docker exec -i $CONTAINER awg show awg0 allowed-ips 2>/dev/null | awk '{print $2}' | grep -oP '\d+(?=/32)' | sort -n | tail -n 1 | tr -d '\r')

if [ -z "$LAST_OCTET" ]; then
    LAST_OCTET=2
else
    LAST_OCTET=$((LAST_OCTET + 1))
fi

CLIENT_IP="${SUBNET}.${LAST_OCTET}"

# 4. Копируем ВСЕ параметры обфускации из секции [Interface] конфига сервера.
#    AmneziaWG 1.5/2.0/3.x добавил параметры (I1-I5, S3/S4, HeaderProtectionKey,
#    ContentPaddingAddition, таймеры, RandomTrailers и т.д.), и клиент ОБЯЗАН получить
#    их все — иначе handshake не проходит (именно так ломалось при переходе на AWG 3.0).
#    Поэтому больше не держим жесткий список: копируем все строки "ключ = значение"
#    из [Interface], кроме служебных полей самого интерфейса.
IFACE_BLOCK=$(docker exec -i $CONTAINER sh -c "awk '/^\[Interface\]/{f=1;next} /^\[/{if(f)exit} f' $CONF_PATH" 2>/dev/null | tr -d '\r')
# ВАЖНО: убираем ВСЕ возвраты каретки (\r) у обфускационных параметров.
# Если хотя бы один \r попадет в клиентский [Interface], AmneziaWG молча не
# подхватит параметр, хендшейк не пройдёт, а QR-код с \r внутри строк плохо
# сканируется камерой телефона (и импорт по QR тоже сломан).
OBF_PARAMS=$(printf '%s\n' "$IFACE_BLOCK" | tr -d '\r' | awk -F= '
    {
        key=$1; gsub(/^[ \t]+|[ \t]+$/, "", key); key=tolower(key)
        if (key != "" && key !~ /^(privatekey|address|listenport|dns|mtu|table|fwmark|preup|postup|predown|postdown|saveconfig)$/) {
            line=$0; sub(/^[ \t]+/, "", line); sub(/[ \t]+$/, "", line); print line
        }
    }')

if [ -z "$OBF_PARAMS" ]; then
    echo "⚠️ Внимание: в [Interface] сервера не найдено параметров обфускации (простой WireGuard?)." >&2
fi

# 5. Добавляем пира в память интерфейса «на лету»
docker exec -i $CONTAINER sh -c "echo '$PSK' > /tmp/psk.tmp && awg set awg0 peer '$PUB_KEY' preshared-key /tmp/psk.tmp allowed-ips '$CLIENT_IP/32' && rm /tmp/psk.tmp"

# 6. Сохраняем пира в файл конфигурации для персистентности при перезапуске контейнера
docker exec -i $CONTAINER sh -c "cat >> $CONF_PATH <<EOF

# Peer: $USERNAME
[Peer]
PublicKey = $PUB_KEY
PresharedKey = $PSK
AllowedIPs = $CLIENT_IP/32
EOF"

# 7. Выводим готовый клиентский конфиг в stdout (его перехватит бот)
cat <<EOF
[Interface]
Address = $CLIENT_IP/32
DNS = 1.1.1.1, 1.0.0.1
PrivateKey = $PRIV_KEY
$OBF_PARAMS

[Peer]
PublicKey = $SERVER_PUBKEY
PresharedKey = $PSK
Endpoint = $SERVER_IP:$SERVER_PORT
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 20
EOF