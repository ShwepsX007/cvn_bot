import subprocess
import asyncio
import shlex
import os

# Путь к known_hosts, где мы будем хранить отпечатки узлов сервиса (вместо того, чтобы
# принимать любой ключ - так защита от MITM хотя бы между основным сервером и нодами).
_KNOWN_HOSTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".known_hosts")
# Чтобы не ломать существующую установку, по умолчанию продолжаем работать с
# StrictHostKeyChecking=accept-new (не отключает проверку полностью, но и не
# требует ручного добавления ключей при первой установке ноды).
_HOST_KEY_STRATEGY = os.environ.get("SSH_HOST_KEY_POLICY", "accept-new")


def _ssh_base_args(ip: str, port: int) -> list:
    """Базовые аргументы SSH/SCP для подключения к узлов сервисае."""
    return [
        "/usr/bin/ssh", "-p", str(port),
        "-o", f"StrictHostKeyChecking={_HOST_KEY_STRATEGY}",
        "-o", f"UserKnownHostsFile={_KNOWN_HOSTS}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
        f"root@{ip}",
    ]


def _scp_base_args(ip: str, port: int) -> list:
    return [
        "/usr/bin/scp", "-P", str(port),
        "-o", f"StrictHostKeyChecking={_HOST_KEY_STRATEGY}",
        "-o", f"UserKnownHostsFile={_KNOWN_HOSTS}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
    ]


async def run_ssh_command(ip, port, cmd):
    """Выполняет cmd на ноде. Важно: cmd должен быть либо строкой с шелл-командой,
    либо списком аргументов — в последнем случае аргументы квотируются shlex."""
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(shlex.quote(str(x)) for x in cmd)
    ssh_cmd = _ssh_base_args(ip, port) + [cmd]
    process = await asyncio.create_subprocess_exec(
        *ssh_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    err_text = stderr.decode(errors="replace").strip()
    out_text = stdout.decode(errors="replace").strip()
    # docker exec на нодах AmneziaWG часто возвращает Windows-style \r\n
    # (TTY-режим). Сбрасываем \r у обоих потоков, чтобы не тащить каретку
    # в конфиги WireGuard, в парсер awg dump и в сравнения строк ("Ошибка" и т.п.).
    err_text = err_text.replace("\r\n", "\n").replace("\r", "\n")
    out_text = out_text.replace("\r\n", "\n").replace("\r", "\n")
    if process.returncode != 0:
        return f"Ошибка: {err_text or out_text}"
    return out_text


async def setup_new_server(ip, port):
    scripts = ["scripts/add_user.sh", "scripts/remove_user.sh"]
    for script in scripts:
        scp_cmd = _scp_base_args(ip, port) + [
            script,
            f"root@{ip}:/root/",
        ]
        process = await asyncio.create_subprocess_exec(*scp_cmd)
        await process.communicate()

    # Выполняем chmod безопасно (нет пользовательских аргументов, но для единообразия)
    await run_ssh_command(ip, port, "chmod +x /root/add_user.sh /root/remove_user.sh")
    return True


async def get_server_traffic(ip, port):
    """Получает данные по потреблению трафика на сервере."""
    cmd = "docker exec amnezia-awg2 awg show wg0 dump"
    raw_data = await run_ssh_command(ip, port, cmd)

    if "Ошибка" in raw_data or not raw_data:
        return "Не удалось получить данные трафика."

    lines = raw_data.split('\n')
    stats = "📊 Потребление трафика (активные сессии):\n\n"

    # Первые строки обычно содержат инфо о самом сервере, пропускаем их и парсим пиров
    for line in lines:
        parts = line.split('\t')
        if len(parts) >= 6:
            try:
                pub_key = parts[0][:10] + "..."
                rx = int(parts[3])
                tx = int(parts[4])
            except (ValueError, IndexError):
                continue

            rx_mb = round(rx / (1024 * 1024), 2)
            tx_mb = round(tx / (1024 * 1024), 2)
            total_mb = round(rx_mb + tx_mb, 2)

            if total_mb > 0.05:
                stats += (
                    f"🔑 Ключ: {pub_key}\n"
                    f"   📥 Вход: {rx_mb} MB | 📤 Исход: {tx_mb} MB\n"
                    f"   🌍 Всего: {total_mb} MB\n\n"
                )

    return stats
