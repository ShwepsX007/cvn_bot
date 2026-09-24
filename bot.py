import asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import database as db
import ssh_manager as ssh

from admin_panel import admin_router, ADMIN_ID
from user_handlers import user_router

BOT_TOKEN = "8714713208:AAFndBtdC4ELxE_xMluyxANfGvq9b9tTH1M"

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()
dp.include_router(admin_router)
dp.include_router(user_router)

# --- ПРЕДУПРЕЖДЕНИЯ ОБ ОКОНЧАНИИ ---
async def check_expiring_soon():
    users = db.get_expiring_soon_users()
    for tg_id, server_id, username in users:
        server = db.get_server_by_id(server_id)
        srv_name = server[2] if server else f"ID {server_id}"
        try:
            await bot.send_message(
                tg_id, 
                f"⚠️ <b>Внимание!</b>\nВаша подписка на VPN (Сервер: <b>{srv_name}</b>) истекает менее чем через <b>3 часа</b>.", 
                parse_mode="HTML"
            )
            db.mark_notified_3h(tg_id, server_id)
        except Exception: pass

async def check_expiring_1d():
    users = db.get_expiring_soon_users_1d()
    for tg_id, server_id, username in users:
        server = db.get_server_by_id(server_id)
        srv_name = server[2] if server else f"ID {server_id}"
        try:
            await bot.send_message(
                tg_id, 
                f"🔔 <b>Напоминание!</b>\nВаша подписка на VPN (Сервер: <b>{srv_name}</b>) истекает менее чем через <b>1 день</b>.", 
                parse_mode="HTML"
            )
            db.mark_notified_1d(tg_id, server_id)
        except Exception: pass

async def check_expiring_3d():
    users = db.get_expiring_soon_users_3d()
    for tg_id, server_id, username in users:
        server = db.get_server_by_id(server_id)
        srv_name = server[2] if server else f"ID {server_id}"
        try:
            await bot.send_message(
                tg_id, 
                f"🔔 <b>Напоминание!</b>\nВаша подписка на VPN (Сервер: <b>{srv_name}</b>) истекает через <b>3 дня</b>.", 
                parse_mode="HTML"
            )
            db.mark_notified_3d(tg_id, server_id)
        except Exception: pass

# --- ОТКЛЮЧЕНИЕ ПРОСРОЧЕННЫХ ---
async def check_expired_users():
    expired_users = db.get_expired_users()
    for tg_id, server_id, username in expired_users:
        server = db.get_server_by_id(server_id)
        if server:
            ip, port, srv_name = server[0], server[1], server[2]
            cmd = f"bash /root/remove_user.sh {username}"
            result = await ssh.run_ssh_command(ip, port, cmd)
            
            if "Ошибка" not in result:
                db.deactivate_user(tg_id, server_id)
                try:
                    await bot.send_message(tg_id, f"⚠️ Срок действия вашей VPN-подписки (Сервер: <b>{srv_name}</b>) завершен. Конфигурация отключена.", parse_mode="HTML")
                except Exception: pass
                try:
                    await bot.send_message(ADMIN_ID, f"🔴 <b>Подписка истекла!</b>\nПользователь <code>{tg_id}</code> был отключен от сервера {srv_name}.", parse_mode="HTML")
                except Exception: pass
            else:
                print(f"❌ Ошибка отключения пользователя {username}: {result}")

# --- ОЧИСТКА МЕРТВЫХ ДУШ (>30 ДНЕЙ) ---
async def clean_inactive_users():
    inactive = db.get_inactive_users_30d()
    if inactive:
        for tg_id, server_id, username in inactive:
            server = db.get_server_by_id(server_id)
            if server:
                ip, port = server[0], server[1]
                cmd = f"bash /root/remove_user.sh {username}"
                await ssh.run_ssh_command(ip, port, cmd)
            db.delete_user_completely(tg_id, server_id)
            print(f"🗑 Пользователь {username} удален за неактивность > 30 дней.")