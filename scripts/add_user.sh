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
LAST_OCTET=$(docker exec -i $CONTAINER awg show awg0 allowed-ips 2>/dev/null | awk '{print $2}' | grep -oP '\d+(?=/32)' | sort -n | tail -n 1)

if [ -z "$LAST_OCTET" ]; then
    LAST_OCTET=2
else
    LAST_OCTET=$((LAST_OCTET + 1))
fi

CLIENT_IP="${SUBNET}.${LAST_OCTET}"

# 4. Получаем параметры обфускации AmneziaWG из конфига сервера
get_val() {
    docker exec -i $CONTAINER grep -i "^$1" "$CONF_PATH" 2>/dev/null | cut -d '=' -f2- | sed 's/^[ \t]*//;s/[ \t]*$//;s/\r//'
}

Jc=$(get_val "Jc")
Jmin=$(get_val "Jmin")
Jmax=$(get_val "Jmax")
S1=$(get_val "S1")
S2=$(get_val "S2")
S3=$(get_val "S3")
S4=$(get_val "S4")
H1=$(get_val "H1")
H2=$(get_val "H2")
H3=$(get_val "H3")
H4=$(get_val "H4")

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
Jc = ${Jc:-0}
Jmin = ${Jmin:-0}
Jmax = ${Jmax:-0}
S1 = ${S1:-0}
S2 = ${S2:-0}
S3 = ${S3:-0}
S4 = ${S4:-0}
H1 = ${H1:-0}
H2 = ${H2:-0}
H3 = ${H3:-0}
H4 = ${H4:-0}

[Peer]
PublicKey = $SERVER_PUBKEY
PresharedKey = $PSK
Endpoint = $SERVER_IP:$SERVER_PORT
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 20
EOF