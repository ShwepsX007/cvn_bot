"""ВАЖНО: этот файл — ранняя демонстрационная версия бота и НЕ используется в проде.

Реальный бот + сайт запускаются одним процессом через web_app.py (бот — модуль bot.py).
Оставлен как справочный пример/заготовка; секреты в коде больше не хранит.
"""
import os
import asyncio
import sqlite3
import paramiko
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message

# === КОНФИГУРАЦИЯ ===
# Токен бота читаем из переменной окружения или config_tokens.py, как основной бот.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    try:
        from config_tokens import BOT_TOKEN as _T  # type: ignore
        BOT_TOKEN = (_T or "").strip()
    except ImportError:
        BOT_TOKEN = ""
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан (задайте config_tokens.py или переменную окружения BOT_TOKEN)")

# Единый ADMIN_ID из config.py — не рассинхронизируем с остальным кодом.
from config import ADMIN_ID  # noqa: E402

VPN_SERVER_IP = '78.17.66.215'
VPN_SSH_USER = 'root'
VPN_SSH_KEY_PATH = '/root/.ssh/id_rsa'  # Путь к приватному ключу на основном сервере

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect('vpn_users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            username TEXT,
            server_ip TEXT,
            public_key TEXT,
            is_active INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

# === SSH КЛИЕНТ ===
def run_ssh_command(ip, command):
    """Синхронная функция для выполнения SSH команд (будем запускать в потоке)"""
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Подключаемся по SSH ключу без пароля
        key = paramiko.RSAKey.from_private_key_file(VPN_SSH_KEY_PATH)
        ssh.connect(hostname=ip, port=2222, username=VPN_SSH_USER, pkey=key, timeout=10)

        stdin, stdout, stderr = ssh.exec_command(command)
        result = stdout.read().decode().strip()
        error = stderr.read().decode().strip()
        ssh.close()
        return result if result else error
    except Exception as e:
        return f"SSH Ошибка: {e}"

# === ХЭНДЛЕРЫ ===
@dp.message(Command("start"))
async def start_cmd(message: Message):
    # Регистрируем в БД
    conn = sqlite3.connect('vpn_users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (tg_id, username) VALUES (?, ?)',
                   (message.from_user.id, message.from_user.username))
    conn.commit()
    conn.close()

    await message.answer("Привет! Я бот для управления VPN. \nДля покупки доступа нажми /buy")

@dp.message(Command("buy"))
async def buy_cmd(message: Message):
    await message.answer("Оплата получена. Генерирую личный конфиг AmneziaWG...")

    # Передаем Telegram ID или юзернейм в скрипт, чтобы подписать пира в конфиге сервера
    username = f"user_{message.from_user.id}"
    cmd = f"bash /root/add_user.sh {username}"

    # Выполняем скрипт по SSH
    result = await asyncio.to_thread(run_ssh_command, VPN_SERVER_IP, cmd)

    # Результат (result) - это готовый текст конфига, который выдал bash-скрипт!
    # Отправляем его пользователю как текстовый файл

    from aiogram.types import BufferedInputFile

    config_file = BufferedInputFile(result.encode('utf-8'), filename="AmneziaWG_config.conf")
    await message.answer_document(config_file, caption="Вот ваш файл подключения! Импортируйте его в приложение AmneziaWG.")

@dp.message(Command("disable"))
async def disable_user(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    # Пример: отключаем публичный ключ юзера в WireGuard внутри Docker
    pub_key = "КЛЮЧ_ЮЗЕРА_ИЗ_БАЗЫ"

    # Команда заходит в контейнер amnezia-awg и удаляет пир
    cmd = f"docker exec amnezia-awg wg set wg0 peer {pub_key} remove"
    result = await asyncio.to_thread(run_ssh_command, VPN_SERVER_IP, cmd)

    await message.answer(f"Пользователь отключен.\nЛог: {result}")

async def main():
    init_db()
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
