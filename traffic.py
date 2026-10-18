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
import shlex
from datetime import datetime

from aiogram import html as tg_html

import database as db
from config import ADMIN_ID

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


# --- ВОССТАНОВЛЕНИЕ ДОСТУПА ПОСЛЕ СНЯТИЯ ПО ЛИМИТУ ---
# Если лимит трафика ошибочно занизили (или счётчик обнулили), пользователей,
# отключенных по такому лимиту, нужно возвращать в строй БЕЗ оплаты и без
# перевыдачи конфига: пир восстанавливается с ТЕМИ ЖЕ ключами из сохраненного
# config_text - клиентский файл продолжает работать как ни в чем не бывало.

def parse_client_config(config_text):
    """(privkey, psk, client_ip) из сохраненного клиентского .conf (или None-поля)."""
    def _search(pattern):
        m = re.search(pattern, config_text or "", re.MULTILINE)
        return m.group(1).strip() if m else None
    priv = _search(r"^\s*PrivateKey\s*=\s*(\S+)")
    psk = _search(r"^\s*PresharedKey\s*=\s*(\S+)")
    ip = _search(r"^\s*Address\s*=\s*(\d+\.\d+\.\d+\.\d+)")
    return priv, psk, ip


def build_restore_peer_cmd(username, privkey, psk, client_ip):
    """Inline-команда для ноды: вернуть пир с прежними ключами (awg set + запись
    в конфиг с комментарием '# Peer: username', чтобы remove/traffic его снова знали).
    Скрипты на ноде не требуются - всё в одном вызове docker exec."""
    psk_part = f"printf %s {shlex.quote(psk)} > /tmp/psk.rest; " if psk else ""
    psk_arg = " preshared-key /tmp/psk.rest" if psk else ""
    psk_cleanup = "rm -f /tmp/psk.rest; " if psk else ""
    psk_line = f"PresharedKey = {psk}\\n" if psk else ""
    script = (
        "IFACE=$(awg show interfaces 2>/dev/null | tr -d '\\r' | awk '{print $1}'); "
        '[ -z "$IFACE" ] && IFACE=awg0; '
        'CONF=$(find / -name "$IFACE.conf" -o -name awg0.conf -o -name wg0.conf -type f 2>/dev/null | head -n 1); '
        '[ -z "$CONF" ] && { echo "Ошибка: конфиг не найден"; exit 1; }; '
        f'PUB=$(printf %s {shlex.quote(privkey)} | awg pubkey | tr -d "\\r"); '
        '[ -z "$PUB" ] && { echo "Ошибка: не удалось вычислить ключ"; exit 1; }; '
        f"{psk_part}"
        f'awg set "$IFACE" peer "$PUB"{psk_arg} allowed-ips {shlex.quote(client_ip)}/32 || {{ echo "Ошибка: awg set"; exit 1; }}; '
        f"{psk_cleanup}"
        f'printf "\\n# Peer: {shlex.quote(username)}\\n[Peer]\\nPublicKey = %s\\n{psk_line}AllowedIPs = {shlex.quote(client_ip)}/32\\n" "$PUB" >> "$CONF"; '
        f'echo "OK restored {shlex.quote(username)} $PUB"'
    )
    return f"docker exec -i amnezia-awg2 sh -c {shlex.quote(script)}"


async def restore_user_access(tg_id, server_id, ssh_fn=None):
    """Восстанавливает доступ подписки, отключенной по лимиту трафика.

    1. Если сохранен config_text - пир возвращается с ТЕМИ ЖЕ ключами, старый
       конфиг клиента продолжает работать (ничего перевыдавать не нужно).
    2. Иначе - перевыдача через add_user.sh (новые ключи): конфиг сохраняется,
       его можно скачать в кабинете (и отправить в Telegram делает вызывающий).
    Срок подписки НЕ меняется. Возвращает (ok, detail).
    """
    if ssh_fn is None:
        import ssh_manager as ssh
        ssh_fn = ssh.run_ssh_command

    sub = db.get_user_sub(tg_id, server_id)
    if not sub:
        return False, "подписка не найдена"
    username = sub[2]
    # Уже активна (например, восстановление уже выполнялось) - только чистим флаги.
    if sub[5] == 1:
        db.reactivate_user(tg_id, server_id)
        return True, "already_active"
    server = db.get_server_by_id(server_id)
    if not server:
        return False, "сервер не найден"
    ip, port = server[0], server[1]

    # 1) Попытка вернуть пир с прежними ключами
    cfg = db.get_user_config(tg_id, server_id)
    priv, psk, client_ip = parse_client_config(cfg)
    if priv and client_ip:
        try:
            result = await ssh_fn(ip, port, build_restore_peer_cmd(username, priv, psk, client_ip))
            if result and "Ошибка" not in result:
                db.reactivate_user(tg_id, server_id)
                return True, "same_keys"
        except Exception as e:
            print(f"⚠️ traffic: восстановление прежними ключами не удалось ({tg_id}): {e}")

    # 2) Фолбэк: перевыдача конфига (новые ключи)
    try:
        cmd = f"bash /root/add_user.sh {shlex.quote(username)}"
        config_text = await ssh_fn(ip, port, cmd)
    except Exception as e:
        return False, f"нода недоступна: {e}"
    if not config_text or "Ошибка" in config_text:
        return False, f"перевыдача не удалась: {config_text or 'нет ответа'}"
    db.set_user_config(tg_id, server_id, config_text)
    db.reactivate_user(tg_id, server_id)
    return True, "reissued"


def _expire_in_future(expire_date_str):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(expire_date_str or "", fmt) > datetime.now()
        except (TypeError, ValueError):
            continue
    return False


async def restore_after_limit_change(bot=None, ssh_fn=None):
    """Возвращает в строй отключенных по лимиту трафика, КТО ТЕПЕРЬ УКЛАДЫВАЕТСЯ
    в лимит (лимит подняли, счётчик обнулили - в т.ч. после ошибки с настройками).
    Заодно снимает флаг «предупрежден» у тех, кто снова далеко от лимита.

    Возвращает число восстановленных. Уведомления (Telegram пользователю и админу)
    отправляются здесь, если передан bot."""
    restored = 0
    for tg_id, server_id, username, expire_date, active, limit_gb, used in db.get_subs_over_traffic():
        limit_bytes = resolve_limit_bytes(limit_gb)
        used = used or 0
        if limit_bytes and used >= limit_bytes:
            continue  # всё ещё выше лимита - не восстанавливаем
        if not _expire_in_future(expire_date):
            continue  # срок и так истек - это уже честное отключение
        ok, detail = await restore_user_access(tg_id, server_id, ssh_fn=ssh_fn)
        if not ok:
            print(f"⚠️ traffic: не удалось восстановить {tg_id} (сервер {server_id}): {detail}")
            continue
        restored += 1
        if bot is not None:
            server = db.get_server_by_id(server_id)
            srv_name = server[2] if server else f"ID {server_id}"
            if detail == "reissued":
                user_note = "Новый файл конфигурации уже в личном кабинете (раздел «Мои подписки») — скачайте его взамен старого."
            else:
                user_note = "Ваш прежний конфиг продолжает работать — ничего делать не нужно."
            if not db.is_virtual_tg_id(tg_id):
                try:
                    await bot.send_message(
                        tg_id,
                        "✅ <b>Доступ восстановлен!</b>\n\n"
                        f"Сервер: <b>{tg_html.quote(srv_name)}</b>\n"
                        "Лимит трафика обновлён, доступ снова активен. " + user_note,
                        parse_mode="HTML")
                    if detail == "reissued":
                        # новый файл (прежние ключи уже не действуют) - сразу отправляем
                        from aiogram.types import BufferedInputFile
                        cfg_text = db.get_user_config(tg_id, server_id)
                        if cfg_text:
                            await bot.send_document(
                                tg_id,
                                BufferedInputFile(cfg_text.encode("utf-8"), filename=f"ID{int(server_id)}AWG.conf"),
                                caption="📎 Новый файл конфигурации взамен старого — удалите старый туннель и импортируйте этот.")
                except Exception as _e:
                    print(f"⚠️ Не удалось отправить сообщение в чат: {_e}")
            try:
                who = f"<code>{int(tg_id)}</code>"
                if db.is_virtual_tg_id(tg_id):
                    _acc = db.get_email_account_by_virtual_tg(tg_id)
                    who = f"📧 {tg_html.quote(_acc[1])}" if _acc else who
                await bot.send_message(
                    ADMIN_ID,
                    f"🟢 <b>Доступ восстановлен</b> после обновления лимита трафика: "
                    f"пользователь {who}, сервер {tg_html.quote(srv_name)} "
                    f"({'конфиг перевыдан' if detail == 'reissued' else 'прежние ключи'}).",
                    parse_mode="HTML")
            except Exception as _e:
                print(f"⚠️ Не удалось отправить сообщение в чат: {_e}")

    # Флаг «предупрежден»: лимит подняли - предупреждать пользователя можно снова
    try:
        warn_percent = int(db.get_setting("traffic_warn_percent") or 0)
    except (TypeError, ValueError):
        warn_percent = 0
    for tg_id, server_id, username, limit_gb, used, warned in db.get_active_subs_with_traffic():
        if not warned:
            continue
        limit_bytes = resolve_limit_bytes(limit_gb)
        used = used or 0
        if (not limit_bytes) or (warn_percent > 0 and used < limit_bytes * warn_percent / 100):
            db.clear_traffic_warned(tg_id, server_id)
    return restored
