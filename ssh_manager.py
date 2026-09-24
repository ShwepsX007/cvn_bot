import subprocess
import asyncio

async def run_ssh_command(ip, port, cmd):
    # Указываем полный путь к ssh (/usr/bin/ssh)
    ssh_cmd = [
        "/usr/bin/ssh", "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        f"root@{ip}",
        cmd
    ]
    process = await asyncio.create_subprocess_exec(
        *ssh_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        return f"Ошибка: {stderr.decode()}"
    return stdout.decode().strip()

async def setup_new_server(ip, port):
    scripts = ["scripts/add_user.sh", "scripts/remove_user.sh"]
    for script in scripts:
        # Указываем полный путь к scp (/usr/bin/scp)
        scp_cmd = [
            "/usr/bin/scp", "-P", str(port),
            "-o", "StrictHostKeyChecking=no",
            script,
            f"root@{ip}:/root/"
        ]
        process = await asyncio.create_subprocess_exec(*scp_cmd)
        await process.communicate()
        
    chmod_cmd = "chmod +x /root/add_user.sh /root/remove_user.sh"
    await run_ssh_command(ip, port, chmod_cmd)
    return True

async def get_server_traffic(ip, port):
    """Получает данные по потреблению трафика на сервере"""
    # Запрашиваем данные у AmneziaWG (dump выводит понятную структуру пиров)
    cmd = "docker exec amnezia-awg2 awg show wg0 dump"
    raw_data = await run_ssh_command(ip, port, cmd)
    
    if "Ошибка" in raw_data or not raw_data:
        return "Не удалось получить данные трафика."
        
    lines = raw_data.split('\n')
    stats = "📊 Потребление трафика (активные сессии):\n\n"
    
    # Первые строки обычно содержат инфо о самом сервере, пропускаем их и парсим пиров
    for line in lines:
        parts = line.split('\t')
        if len(parts) >= 6: # Строка пира содержит ключи, байты и т.д.
            pub_key = parts[0][:10] + "..." # Маскируем ключ
            rx = int(parts[3]) # Скачано в байтах
            tx = int(parts[4]) # Загружено в байтах
            
            # Переводим в Мегабайты
            rx_mb = round(rx / (1024 * 1024), 2)
            tx_mb = round(tx / (1024 * 1024), 2)
            total_mb = round(rx_mb + tx_mb, 2)
            
            if total_mb > 0.05: # Показываем только тех, кто потребил хоть что-то
                stats += f"🔑 Ключ: {pub_key}\n   📥 Вход: {rx_mb} MB | 📤 Исход: {tx_mb} MB\n   🌍 Всего: {total_mb} MB\n\n"
                
    return stats