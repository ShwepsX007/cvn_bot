import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import database as db
import ssh_manager as ssh

from admin_panel import admin_router, ADMIN_ID
from user_handlers import user_router

# ВАЖНО: токен бота НЕ храним в git-репозитории - Telegram автоматически
# отзывает токены, найденные в публичных репозиториях (так однажды уже случалось
# и бот отвечал "Unauthorized"). Порядок чтения:
#   1) переменная окружения BOT_TOKEN (например, Environment= в systemd unit);
#   2) файл config_tokens.py рядом (есть config_tokens.example.py; сам файл в .gitignore).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    try:
        from config_tokens import BOT_TOKEN as _LOCAL_TOKEN
        BOT_TOKEN = (_LOCAL_TOKEN or "").strip()
    except ImportError:
        BOT_TOKEN = ""

if not BOT_TOKEN:
    print("⛔ BOT_TOKEN не задан: создайте config_tokens.py (см. config_tokens.example.py) "
          "или переменную окружения BOT_TOKEN, иначе бот не запустится.")

bot = Bot(token=BOT_TOKEN or "0:placeholder", default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
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

# --- АВТООБНОВЛЕНИЕ КОНТЕЙНЕРОВ AMNEZIAWG НА НОДАХ ---
# Включается в админке: Настройки -> checkbox «Автообновление AWG-контейнера» (container_autoupdate=on).
# Запускается раз в день в 05:05 (см. web_app.py). Обновление безопасно: ключи пиров и конфиг
# лежат в volume /opt/amnezia/awg, поэтому выданные клиентам конфиги продолжают работать.
async def autoupdate_node_containers():
    from datetime import datetime
    if (db.get_setting("container_autoupdate") or "").strip().lower() != "on":
        return
    try:
        import server_installer
    except ImportError:
        print("⚠️ server_installer недоступен - пропускаю автообновление нод")
        return

    servers = db.get_active_servers(include_free=True)
    if not servers:
        return

    report = [f"🔄 Автообновление AWG-нод от {datetime.now().strftime('%d.%m.%Y %H:%M')}:"]
    notify = []
    for srv in servers:
        sid, ip, port, name = srv[0], srv[1], srv[2], srv[3]  # get_active_servers: (id, ip, port, name, max_users)
        try:
            res = await asyncio.to_thread(server_installer.update_container, ip, port)
        except Exception as e:
            res = {"ok": False, "changed": False, "error": f"{type(e).__name__}: {e}"}

        if res.get("ok") and res.get("changed"):
            report.append(f"✅ {name} ({ip}): контейнер обновлен до свежей сборки ({res.get('new_image','')[:19]}…). Конфиги клиентов не изменились и продолжают работать.")
            notify.append(f"✅ <b>{name}</b>: контейнер AWG обновлен до последней версии.")
        elif res.get("ok"):
            report.append(f"⚪ {name} ({ip}): уже последняя сборка - не трогал.")
        else:
            err = (res.get("error") or "неизвестная ошибка").split("\n")[0]
            report.append(f"❌ {name} ({ip}): {err}")
            notify.append(f"❌ <b>{name}</b>: автообновление не удалось - {err}")

    db.update_setting("node_update_report", "\n".join(report))
    print("\n".join(report))

    # Сообщаем админам только когда есть что сказать (обновления или ошибки),
    # а не «все ok, ничего не делал» каждый день.
    if notify:
        msg = "🔄 <b>Автообновление нод AmneziaWG:</b>\n\n" + "\n".join(notify)
        targets = {ADMIN_ID} | {a[0] for a in db.list_admins()}
        for aid in targets:
            try:
                await bot.send_message(aid, msg, parse_mode="HTML")
            except Exception:
                pass

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