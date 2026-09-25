"""
Автоустановка нового VPN-сервера (ноды AmneziaWG) прямо из веб-админки.

Все шаги, которые раньше делались руками по инструкции (см. инструкция.txt):
  1) подключение к свежему Ubuntu VPS по SSH (root + пароль);
  2) обмен SSH-ключами (сервер бота -> нода), чтобы бот ходил без пароля;
  3) проверка контейнера amnezia-awg2; если его нет - АВТОУСТАНОВКА по шаблону:
     установка docker, образ amneziavpn/amneziawg-go, конфиг awg0.conf с параметрами
     обфускации, ip_forward, NAT (MASQUERADE), запуск контейнера;
  4) заливка scripts/add_user.sh и remove_user.sh (chmod +x, чистка \r);
  5) финальная проверка (awg show).

Работает через paramiko (пароль нужен только один раз - в момент установки;
дальше бот ходит по SSH-ключу, как и раньше).
"""

import os
import secrets
import subprocess

CONTAINER_NAME = "amnezia-awg2"
AWG_IMAGE = "amneziavpn/amneziawg-go:latest"
AWG_CONF_DIR = "/opt/amnezia/awg"
AWG_IFACE = "awg0"
AWG_LISTEN_IN_CONTAINER = 51820
SUBNET = "10.8.1"

# Шаблон клиентских параметров обфускации AmneziaWG (как в приложении Amnezia по умолчанию).
# add_user.sh копирует эти параметры клиентам один в один, поэтому почти любые значения
# годятся - важно лишь, чтобы потом сервер и клиенты оказались одинаковыми.
OBFUSCATION_DEFAULTS = {
    "Jc": 4,
    "Jmin": 40,
    "Jmax": 70,
    "S1": 0,
    "S2": 0,
    "H1": 1,
    "H2": 2,
    "H3": 3,
    "H4": 4,
    "MTU": 1420,
}

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")


class InstallError(Exception):
    pass


def _log(log_lines, text):
    log_lines.append(text)


def _run(ssh, cmd, timeout=120):
    """Выполнить команду на ноде. Возвращает (exit_code, stdout, stderr)."""
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return code, out, err


def _must(ssh, cmd, what, log_lines, timeout=120):
    code, out, err = _run(ssh, cmd, timeout=timeout)
    if code != 0:
        raise InstallError(f"{what}: команда `{cmd}` завершилась с кодом {code}.\n{out}\n{err}")
    if out.strip():
        _log(log_lines, f"   {what}: {out.strip()[:300]}")
    return out


def _local(cmd, timeout=60):
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _ensure_bot_ssh_pubkey(log_lines):
    """Публичный ключ бота (генерируем, если еще нет)."""
    for path in ("/root/.ssh/id_ed25519.pub", os.path.expanduser("~/.ssh/id_ed25519.pub"),
                 "/root/.ssh/id_rsa.pub", os.path.expanduser("~/.ssh/id_rsa.pub")):
        if os.path.exists(path):
            with open(path) as f:
                key = f.read().strip()
            if key:
                _log(log_lines, f"🔑 Использую существующий ключ: {path}")
                return key
    code, out, err = _local('ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519')
    if code != 0:
        raise InstallError(f"Не удалось сгенерировать SSH-ключ на сервере бота: {err}")
    with open("/root/.ssh/id_ed25519.pub") as f:
        key = f.read().strip()
    _log(log_lines, "🔑 Сгенерирован новый SSH-ключ бота (ed25519)")
    return key


def _detect_udp_port(ssh, log_lines):
    """Узнать опубликованный UDP-порт существующего контейнера."""
    code, out, err = _run(ssh, "docker port amnezia-awg2 51820/udp | head -n 1")
    if code == 0 and out.strip():
        host_port = out.strip().split(":")[-1]
        if host_port.isdigit():
            return int(host_port)
    return None


def _install_amnezia_container(ssh, udp_port, log_lines):
    """Автоматическая установка AmneziaWG (docker + контейнер + NAT) по шаблону."""
    _log(log_lines, "🐳 Проверка docker...")
    code, out, err = _run(ssh, "command -v docker")
    if code != 0:
        _log(log_lines, "🐳 Docker не найден - устанавливаю (2-4 минуты)...")
        _must(ssh, "export DEBIAN_FRONTEND=noninteractive && apt-get update -y", "apt update", log_lines, timeout=600)
        _must(ssh, "curl -fsSL https://get.docker.com | sh", "установка docker", log_lines, timeout=600)
        _must(ssh, "systemctl enable --now docker", "запуск docker", log_lines)
    else:
        _log(log_lines, "🐳 Docker CLI уже установлен ✓ - проверяю daemon...")

    # Важный случай (встречается у хостеров): CLI docker установлен, но daemon ВЫКЛЮЧЕН -
    # pull/exec тогда падают с "no such file /var/run/docker.sock".
    code, out, err = _run(ssh, "docker info --format ok", timeout=60)
    if code != 0:
        _log(log_lines, "🐳 Daemon docker не отвечает - запускаю...")
        _run(ssh, "systemctl unmask docker 2>/dev/null; systemctl enable --now docker", timeout=120)
        code, out, err = _run(
            ssh,
            "for i in 1 2 3 4 5 6; do docker info --format ok 2>/dev/null && exit 0; sleep 5; done; exit 1",
            timeout=120)
        if code != 0:
            _, st, _ = _run(ssh, "{ systemctl status docker --no-pager | tail -5; journalctl -u docker -n 5 --no-pager | tail -5; } 2>&1", timeout=60)
            raise InstallError(
                "docker daemon не стартует:\n" + str(st).strip() +
                "\n\nИсправьте на ноде (обычно: systemctl start docker), пришлите это на проверку если непонятно.")
        _log(log_lines, "🐳 Daemon запущен ✓")
        # Если daemon был выключен, внешняя проверка контейнера могла идти «вслепую».
        # Вдруг контейнер УЖЕ есть (ставили приложением)? Тогда ничего нельзя пересоздавать!
        code2, _, _ = _run(ssh, f"docker ps -a --format '{{{{.Names}}}}' | grep -x {CONTAINER_NAME}")
        if code2 == 0:
            raise InstallError(
                f"Daemon был выключен - я запустил его и обнаружил: контейнер {CONTAINER_NAME} "
                "УЖЕ существует. Запустите мастер еще раз - он пойдет по короткой ветке "
                "(ключ+скрипты), НЕ трогая установленный AmneziaWG.")
    else:
        _log(log_lines, "🐳 Daemon работает ✓")

    _log(log_lines, "📥 Загрузка образа AmneziaWG (может занять пару минут)...")
    code, out, err = _run(ssh, f"docker pull {AWG_IMAGE}", timeout=900)
    if code != 0:
        # Частый RU-случай: Docker Hub зарезан у хостера - пробуем через зеркала реестра.
        _log(log_lines, "⚠️ Pull не получился - настраиваю зеркала реестра Docker и пробую еще раз...")
        mirror_cmd = (
            "mkdir -p /etc/docker && "
            "cat > /etc/docker/daemon.json <<'DOCKERCFG'\n"
            '{ "registry-mirrors": ["https://mirror.gcr.io", "https://dockerhub.timeweb.cloud", "https://docker.m.daocloud.io"] }\n'
            "DOCKERCFG\n"
            "systemctl restart docker"
        )
        _run(ssh, mirror_cmd, timeout=300)
        code, out, err = _run(ssh, f"docker pull {AWG_IMAGE}", timeout=900)
        if code != 0:
            raise InstallError(
                f"Не удалось скачать образ {AWG_IMAGE} (в т.ч. через зеркала):\n{(err or out).strip()}\n"
                "Похоже, нода не может достучаться до реестров Docker - проверьте интернет на ноде "
                "(curl -I https://mirror.gcr.io) либо тикет хостеру.")

    _must(ssh, f"mkdir -p {AWG_CONF_DIR}", "каталог конфигов", log_lines)

    _log(log_lines, "🔐 Генерация ключей сервера...")
    priv = _must(ssh, f"docker run --rm {AWG_IMAGE} awg genkey", "genkey", log_lines, timeout=120).strip()
    if not priv or len(priv) < 30:
        raise InstallError("Генерация приватного ключа AmneziaWG не удалась - неожиданный вывод awg genkey.")

    obf = "\n".join(f"{k} = {v}" for k, v in OBFUSCATION_DEFAULTS.items())
    conf = (
        "[Interface]\n"
        f"PrivateKey = {priv}\n"
        f"Address = {SUBNET}.1/24\n"
        f"ListenPort = {AWG_LISTEN_IN_CONTAINER}\n"
        f"{obf}\n"
    )
    # Пишем конфиг через quoted-heredoc: 'AWGCONF' в кавычках отключает подстановку переменных
    _must(ssh, f"cat > {AWG_CONF_DIR}/{AWG_IFACE}.conf <<'AWGCONF'\n{conf}AWGCONF", "запись awg0.conf", log_lines)

    _log(log_lines, "🌐 Включаю форвардинг пакетов...")
    _run(ssh, "sysctl -w net.ipv4.ip_forward=1 >/dev/null")
    _run(ssh, "grep -q 'net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf")
    _run(ssh, "sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1")

    _log(log_lines, "🔥 Настройка NAT...")
    code, out, err = _run(ssh, "ip route get 8.8.8.8 | awk '{print $5; exit}'")
    iface = out.strip() or "eth0"
    _log(log_lines, f"   Внешний интерфейс: {iface}")
    nat_cmds = [
        f"iptables -t nat -C POSTROUTING -s {SUBNET}.0/24 -o {iface} -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s {SUBNET}.0/24 -o {iface} -j MASQUERADE",
        f"iptables -C FORWARD -i {AWG_IFACE} -j ACCEPT 2>/dev/null || iptables -A FORWARD -i {AWG_IFACE} -j ACCEPT",
        f"iptables -C FORWARD -o {AWG_IFACE} -j ACCEPT 2>/dev/null || iptables -A FORWARD -o {AWG_IFACE} -j ACCEPT",
    ]
    for cmd in nat_cmds:
        _must(ssh, cmd, "iptables", log_lines)
    # Сохранить правила после перезагрузки (если пакета persistent нет - просто предупредим)
    _run(ssh, "export DEBIAN_FRONTEND=noninteractive && apt-get install -y iptables-persistent >/dev/null 2>&1; netfilter-persistent save >/dev/null 2>&1", timeout=600)

    _log(log_lines, f"🔥 Открываю UDP-порт {udp_port} в ufw (если включен)...")
    _run(ssh, f"command -v ufw >/dev/null && ufw status | grep -qw active && ufw allow {udp_port}/udp || true")

    _log(log_lines, "🚀 Запускаю контейнер AmneziaWG...")
    run_cmd = (
        f"docker rm -f {CONTAINER_NAME} >/dev/null 2>&1; "
        f"docker run -d --name {CONTAINER_NAME} --restart always "
        f"--cap-add NET_ADMIN --device /dev/net/tun "
        f"-p {udp_port}:{AWG_LISTEN_IN_CONTAINER}/udp "
        f"-v {AWG_CONF_DIR}:/opt/amnezia/awg "
        f"{AWG_IMAGE}"
    )
    _must(ssh, run_cmd, "запуск контейнера", log_lines, timeout=300)


BOT_KEY_CANDIDATES = (
    "/root/.ssh/id_ed25519",
    os.path.expanduser("~/.ssh/id_ed25519"),
    "/root/.ssh/id_rsa",
    os.path.expanduser("~/.ssh/id_rsa"),
)


def _connect_with_key(ip, port, log_lines):
    """Подключение к ноде по SSH-ключу бота (так же ходит бот в повседневной работе)."""
    import paramiko
    last_err = None
    for path in BOT_KEY_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ip, port=port, username="root", key_filename=path,
                        timeout=15, banner_timeout=20, auth_timeout=20,
                        allow_agent=False, look_for_keys=False)
            _log(log_lines, f"🔌 Подключено по ключу {path}")
            return ssh
        except Exception as e:
            last_err = e
            _log(log_lines, f"   ⚠️ ключ {path} не подошел: {e}")
    raise InstallError(
        f"Не удалось подключиться к root@{ip}:{port} SSH-ключом бота ({last_err}). "
        "Если нода старая и ключ на нее не ставился - прогоните ее через "
        "«🚀 Мастер установки» (он докинет ключ) или выполните ssh-copy-id вручную."
    )


def update_container(ip, port, progress=None):
    """Обновление контейнера AmneziaWG на существующей ноде до :latest.

    Безопасно для пользователей: конфиг сервера и публичные ключи пиров лежат в
    volume /opt/amnezia/awg (а не внутри образа!), поэтому после пересоздания
    контейнера ВСЕ ранее выданные клиентские конфиги продолжают работать -
    главное сохранить тот же внешний UDP-порт (его мы определяем и переиспользуем).

    Возвращает dict: {ok, changed, old_image, new_image, udp_port, steps, error}.
    ok=True, changed=False означает «уже самая свежая сборка, ничего не трогали».
    """
    log_lines = []

    def log(text):
        _log(log_lines, text)
        if progress:
            progress(text)

    res = {"ok": False, "changed": False, "old_image": None, "new_image": None,
           "udp_port": None, "steps": log_lines, "error": None}

    ssh = None
    try:
        log(f"🔌 Подключаюсь к root@{ip}:{port} по SSH-ключу бота...")
        ssh = _connect_with_key(ip, port, log_lines)

        code, out, err = _run(ssh, f"docker ps -a --format '{{{{.Names}}}}' 2>/dev/null | grep -x {CONTAINER_NAME}")
        if code != 0:
            raise InstallError(f"На ноде нет контейнера {CONTAINER_NAME} - нечего обновлять. "
                               "Прогоните ноду через «🚀 Мастер установки».")

        code, out, _ = _run(ssh, f"docker inspect -f '{{{{.Image}}}}' {CONTAINER_NAME}")
        old_img = out.strip()
        res["old_image"] = old_img
        log(f"📦 Текущий образ контейнера: {old_img[:19]}…")

        log(f"📥 Скачиваю свежий образ {AWG_IMAGE} (1-3 минуты)...")
        _must(ssh, f"docker pull {AWG_IMAGE}", "docker pull", log_lines, timeout=900)
        code, out, _ = _run(ssh, f"docker image inspect -f '{{{{.Id}}}}' {AWG_IMAGE}")
        new_img = out.strip()
        res["new_image"] = new_img

        if new_img and new_img == old_img:
            log("✅ Образ уже самый свежий - контейнер НЕ трогаю, пользователи ничего не заметят.")
            res["ok"] = True
            res["changed"] = False
            return res

        log(f"🆕 Обнаружена новая версия ({new_img[:19]}…) - пересоздаю контейнер "
            "с сохранением конфига и порта...")

        udp_port = _detect_udp_port(ssh, log_lines)
        if not udp_port:
            raise InstallError("Не удалось определить внешний UDP-порт контейнера - "
                               "автообновление ОТМЕНЕНО до удаления (ничего не сломано). "
                               "Проверьте вручную: docker port amnezia-awg2")
        res["udp_port"] = udp_port
        log(f"🔌 Внешний UDP-порт сохраняю прежним: {udp_port} - клиентские конфиги останутся валидными")

        code, out, _ = _run(ssh, f"docker exec {CONTAINER_NAME} awg show | grep -c '^peer:' || true")
        peers_before = out.strip() if code == 0 else "?"
        log(f"👥 Пиров (клиентов) в текущем интерфейсе: {peers_before}")

        log("🔄 Пересоздаю контейнер (~10 секунд даунтайм, туннель сам переподключится)...")
        run_cmd = (
            f"docker rm -f {CONTAINER_NAME} >/dev/null 2>&1; "
            f"docker run -d --name {CONTAINER_NAME} --restart always "
            f"--cap-add NET_ADMIN --device /dev/net/tun "
            f"-p {udp_port}:{AWG_LISTEN_IN_CONTAINER}/udp "
            f"-v {AWG_CONF_DIR}:/opt/amnezia/awg "
            f"{AWG_IMAGE}"
        )
        _must(ssh, run_cmd, "пересоздание контейнера", log_lines, timeout=300)

        log("🔍 Проверяю интерфейс AmneziaWG...")
        code, out, err = _run(ssh, f"sleep 3; docker exec -i {CONTAINER_NAME} awg show 2>&1 | head -5", timeout=60)
        if "interface" not in out.lower() and "public key" not in out.lower():
            raise InstallError(f"Контейнер пересоздан, но awg не отвечает:\n{out}\n{err}\n"
                               "Смотрите на ноде: docker logs amnezia-awg2")

        code, out2, _ = _run(ssh, f"docker exec {CONTAINER_NAME} awg show | grep -c '^peer:' || true")
        peers_after = out2.strip() if code == 0 else "?"
        log(f"👥 Пиров после обновления: {peers_after} (было {peers_before})")

        res["ok"] = True
        res["changed"] = True
        log("✅ Контейнер обновлен. Ранее выданные конфиги продолжают работать - "
            "перекачивать ничего не нужно.")
        return res

    except InstallError as e:
        res["error"] = str(e)
        log(f"❌ {e}")
        return res
    except Exception as e:
        res["error"] = f"Непредвиденная ошибка: {type(e).__name__}: {e}"
        log(f"❌ {res['error']}")
        return res
    finally:
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass


def install_new_server(ip, port, password, server_name, progress=None):
    """Полный цикл установки ноды. progress - функция(кусок лога). Возвращает dict:
    {ok, steps (лог), udp_port, error}."""
    try:
        import paramiko
    except ImportError:
        return {"ok": False, "steps": [], "udp_port": None,
                "error": "paramiko не установлен на сервере бота: pip install paramiko"}

    log_lines = []

    def log(text):
        _log(log_lines, text)
        if progress:
            progress(text)

    udp_port = secrets.randbelow(50000) + 10000  # случайный внешний UDP-порт 10000-59999

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        log(f"🔌 Подключаюсь к root@{ip}:{port} по паролю...")
        ssh.connect(ip, port=port, username="root", password=password,
                    timeout=15, banner_timeout=20, auth_timeout=20,
                    allow_agent=False, look_for_keys=False)
        log("✅ Подключение по паролю успешно")

        # --- Шаг 1: SSH-ключ бота на ноду ---
        log("🔑 Передаю SSH-ключ бота на новый сервер...")
        pubkey = _ensure_bot_ssh_pubkey(log_lines)
        escaped = pubkey.replace("'", "'\\''")
        _must(ssh,
              "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
              f"grep -qF '{escaped}' /root/.ssh/authorized_keys 2>/dev/null || echo '{escaped}' >> /root/.ssh/authorized_keys && "
              "chmod 600 /root/.ssh/authorized_keys",
              "установка ключа", log_lines)
        log("✅ Ключ установлен - дальше бот будет ходить без пароля")

        # --- Шаг 2: контейнер AmneziaWG ---
        code, out, err = _run(ssh, f"docker ps -a --format '{{{{.Names}}}}' 2>/dev/null | grep -x {CONTAINER_NAME}")
        if code == 0:
            log(f"📦 Контейнер {CONTAINER_NAME} уже установлен - пропускаю установку AmneziaWG")
            existing_port = _detect_udp_port(ssh, log_lines)
            if existing_port:
                udp_port = existing_port
                log(f"🔌 Внешний UDP-порт AmneziaWG: {udp_port}")
            else:
                log("⚠️ Не удалось определить UDP-порт контейнера - проверьте вручную (docker port amnezia-awg2)")
        else:
            log("📦 Контейнер AmneziaWG не найден - запускаю АВТОУСТАНОВКУ по шаблону...")
            _install_amnezia_container(ssh, udp_port, log_lines)

        # --- Шаг 3: проверка awg внутри контейнера ---
        log("🔍 Проверяю интерфейс AmneziaWG внутри контейнера...")
        code, out, err = _run(ssh, f"sleep 3; docker exec -i {CONTAINER_NAME} awg show 2>&1 | head -5", timeout=60)
        if "interface" not in out.lower() and "public key" not in out.lower():
            raise InstallError(f"Контейнер {CONTAINER_NAME} есть, но awg не отвечает:\n{out}\n{err}\n"
                               "Возможно, контейнер не стартовал - проверьте: docker logs amnezia-awg2")
        log("✅ Интерфейс AmneziaWG работает")

        # --- Шаг 4: скрипты add/remove ---
        log("📤 Заливаю скрипты add_user.sh / remove_user.sh...")
        sftp = ssh.open_sftp()
        uploaded = []
        try:
            for name in ("add_user.sh", "remove_user.sh"):
                local_path = os.path.join(SCRIPTS_DIR, name)
                if not os.path.exists(local_path):
                    raise InstallError(f"Не найден локальный файл {local_path} на сервере бота")
                sftp.put(local_path, f"/root/{name}")
                uploaded.append(name)
        finally:
            sftp.close()
        _must(ssh, "chmod +x /root/add_user.sh /root/remove_user.sh && "
                   "sed -i 's/\\r$//' /root/add_user.sh /root/remove_user.sh",
              "chmod + чистка переносов", log_lines)
        log("✅ Скрипты на месте и исполняемы")

        # --- Шаг 5: финальная проверка add_user.sh (тестовый пир не создаем - только синтаксис) ---
        _must(ssh, "bash -n /root/add_user.sh && bash -n /root/remove_user.sh", "проверка синтаксиса скриптов", log_lines)
        log("✅ Скрипты синтаксически корректны")

        log("")
        log(f"🏁 ГОТОВО! Сервер {server_name} ({ip}) полностью настроен.")
        log(f"📡 Внешний UDP-порт AmneziaWG: {udp_port}")
        log("⚠️ Если у хостера есть внешний firewall/Security Group - откройте в его панели UDP-порт "
            f"{udp_port}, иначе клиенты не подключатся.")
        log("🧪 Рекомендация: выдайте себе тестовый конфиг (Клиенты → Выдать доступ) и проверьте соединение.")
        return {"ok": True, "steps": log_lines, "udp_port": udp_port, "error": None}

    except InstallError as e:
        log_lines.append(f"\n❌ ОШИБКА: {e}")
        return {"ok": False, "steps": log_lines, "udp_port": udp_port, "error": str(e)}
    except Exception as e:
        log_lines.append(f"\n❌ НЕПРЕДВИДЕННАЯ ОШИБКА: {e}")
        return {"ok": False, "steps": log_lines, "udp_port": udp_port, "error": str(e)}
    finally:
        try:
            ssh.close()
        except Exception:
            pass
