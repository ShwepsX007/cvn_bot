"""
Учет трафика пользователей и контроль лимитов.

Каждый пользователь на ноде - отдельный пир AmneziaWG с именем user_{tg_id}
(см. scripts/add_user.sh). Накопление:

  1. Скрипт traffic_dump.sh на ноде отдает конфиг awg0.conf (там комментарии
     '# Peer: user_xxx' над каждым [Peer] с его PublicKey) и вывод
     'awg show awg0 dump' со счётчиками transfer-rx/transfer-tx по каждому пиру.
  2. parse_peer_traffic() связывает username <-> публичный ключ и возвращает
     {username: (rx, tx)} в байтах.
  3. sync_all() добавляет дельты к users.traffic_used_bytes (дельта переживает
     перезапуск интерфейса/пересоздание пира - см. apply_user_traffic_counters).

Контроль лимитов: у подписки (tg_id + server_id) есть лимит в ГБ:
  - NULL  = общий по умолчанию (settings.traffic_limit_gb);
  - 0     = без ограничений;
  - >0    = лимит в гигабайтах.
Проверка лимитов и уведомления выполняются в bot.check_traffic_limits().
"""

import re

import database as db

GB = 1024 ** 3

# Строка пира в awg show dump: public-key, preshared-key, endpoint, allowed-ips,
# latest-handshake, transfer-rx, transfer-tx, persistent-keepalive
_CONF_PEER_RE = re.compile(r"^\s*#\s*Peer:\s*(\S+)\s*$")
_CONF_PUBKEY_RE = re.compile(r"^\s*PublicKey\s*=\s*(\S+)\s*$")


def resolve_limit_bytes(user_limit_gb, global_limit_gb=None):
    """Лимит подписки в байтах. 0 = без ограничений (тоже возвращаем 0).

    user_limit_gb None = взять общий по умолчанию (global_limit_gb или из настроек).
    """
    limit_gb = user_limit_gb
    if limit_gb is None:
        if global_limit_gb is None:
            try:
                global_limit_gb = float(db.get_setting("traffic_limit_gb") or 0)
            except (TypeError, ValueError):
                global_limit_gb = 0
        limit_gb = global_limit_gb
    try:
        limit_gb = float(limit_gb or 0)
    except (TypeError, ValueError):
        limit_gb = 0
    return int(limit_gb * GB) if limit_gb > 0 else 0


def format_bytes(n):
    """1234567890 -> '1.15 ГБ' (для уведомлений и админки)."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0
    for unit, size in (("ТБ", 1024 ** 4), ("ГБ", 1024 ** 3), ("МБ", 1024 ** 2), ("КБ", 1024)):
        if n >= size:
            return f"{n / size:.2f} {unit}"
    return f"{int(n)} Б"


def parse_peer_traffic(conf_text, dump_text):
    """Собирает {username: (rx_bytes, tx_bytes)} из awg0.conf и 'awg show awg0 dump'.

    Связка username <-> публичный ключ: над каждым [Peer] в конфиге есть
    комментарий '# Peer: user_xxx', затем строка 'PublicKey = ...'.
    В dump счётчики идут в строке пира: поле 6 = transfer-rx, поле 7 = transfer-tx.
    """
    key_to_user = {}
    pending_user = None
    for line in (conf_text or "").splitlines():
        m = _CONF_PEER_RE.match(line)
        if m:
            pending_user = m.group(1)
            continue
        m = _CONF_PUBKEY_RE.match(line)
        if m and pending_user:
            key_to_user[m.group(1)] = pending_user
            pending_user = None

    result = {}
    for line in (dump_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        # первая строка dump - интерфейс (4 поля), у пира >= 8 полей
        if len(parts) < 7:
            continue
        pubkey, rx_s, tx_s = parts[0], parts[5], parts[6]
        username = key_to_user.get(pubkey)
        if not username:
            continue
        try:
            rx, tx = int(rx_s), int(tx_s)
        except (TypeError, ValueError):
            continue
        result[username] = (rx, tx)
    return result


# Команда для ноды: конфиг (с именами пиров) + dump со счётчиками в одном вызове.
TRAFFIC_DUMP_CMD = (
    'docker exec -i amnezia-awg2 sh -c '
    '\'CONF=$(find / -name awg0.conf -o -name wg0.conf -type f 2>/dev/null | head -n 1); '
    'echo "---CONF---"; cat "$CONF"; echo "---DUMP---"; awg show awg0 dump\''
)


def split_dump_output(out_text):
    """Разбирает ответ TRAFFIC_DUMP_CMD на (conf_text, dump_text)."""
    if "---CONF---" in (out_text or "") and "---DUMP---" in out_text:
        _, rest = out_text.split("---CONF---", 1)
        conf_text, dump_text = rest.split("---DUMP---", 1)
        return conf_text, dump_text
    return out_text or "", ""


async def fetch_server_peers(ip, port, ssh_fn=None):
    """{username: (rx, tx)} с ноды или None, если нода недоступна."""
    if ssh_fn is None:
        import ssh_manager as ssh
        ssh_fn = ssh.run_ssh_command
    out = await ssh_fn(ip, port, TRAFFIC_DUMP_CMD)
    if not out or "Ошибка" in out:
        return None
    conf_text, dump_text = split_dump_output(out)
    return parse_peer_traffic(conf_text, dump_text)


def username_to_tg_id(username):
    """user_{tg_id} -> tg_id; для чужих имен (free-пиры и т.п.) -> None."""
    if not username or not username.startswith("user_"):
        return None
    try:
        return int(username[len("user_"):])
    except (TypeError, ValueError):
        return None


async def sync_all(ssh_fn=None):
    """Синхронизирует счётчики по всем активным нодам. Возвращает число обновленных
    подписок (ошибки отдельных нод не прерывают обход остальных)."""
    servers = db.get_active_servers(include_free=True) or []
    updated = 0
    for s_id, ip, port, _name, _limit in servers:
        try:
            peers = await fetch_server_peers(ip, port, ssh_fn)
        except Exception as e:
            print(f"⚠️ traffic: нода {ip} недоступна: {e}")
            continue
        if not peers:
            continue
        for username, (rx, tx) in peers.items():
            tg_id = username_to_tg_id(username)
            if tg_id is None:
                continue
            used, delta = db.apply_user_traffic_counters(tg_id, s_id, rx, tx)
            if used is not None:
                updated += 1
    return updated
