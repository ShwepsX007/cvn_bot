from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
import database as db
import ssh_manager as ssh
from aiogram import html
from user_handlers import issue_vpn_access
from config import ADMIN_ID

admin_router = Router()

class AdminStates(StatesGroup):
    waiting_for_ip = State()
    waiting_for_srv_name = State()
    waiting_for_rename = State()
    waiting_for_limit = State()
    waiting_for_setting_value = State()
    waiting_for_user_id_to_delete = State()
    waiting_for_reply_text = State()
    waiting_for_broadcast = State()
    waiting_for_manual_srv = State()
    waiting_for_manual_user = State()
    waiting_for_manual_hours = State()
    waiting_for_srv_price = State()
    waiting_for_manual_details = State()
    waiting_for_aipay_key = State()
    waiting_for_platega_merchant = State()
    waiting_for_platega_secret = State()
    waiting_for_site_url = State()

def get_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats"))
    builder.add(InlineKeyboardButton(text="👥 Клиенты", callback_data="adm_users"))
    builder.add(InlineKeyboardButton(text="🖥 Сервера", callback_data="adm_servers_menu"))
    builder.add(InlineKeyboardButton(text="🎁 Выдать конфиг", callback_data="adm_issue_config"))
    builder.add(InlineKeyboardButton(text="🗑 Удалить юзера", callback_data="adm_ask_del_user"))
    builder.add(InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast"))
    builder.add(InlineKeyboardButton(text="⚙️ Настройки бота", callback_data="adm_settings"))
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()

@admin_router.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    await message.answer("🛠 Панель управления VPN бизнесом:", reply_markup=get_admin_keyboard())

@admin_router.callback_query(F.data == "adm_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🛠 Панель управления VPN бизнесом:", reply_markup=get_admin_keyboard())

# --- УПРАВЛЕНИЕ СЕРВЕРАМИ ---
@admin_router.callback_query(F.data == "adm_servers_menu")
async def show_servers_menu(callback: CallbackQuery):
    servers = db.get_active_servers()
    builder = InlineKeyboardBuilder()
    
    text = "🖥 <b>УПРАВЛЕНИЕ СЕРВЕРАМИ</b>\n\nВыберите сервер для детальной настройки или добавьте новый:"
    for s_id, ip, port, name, limit in servers:
        paid_count = db.count_paid_users_on_server(s_id)
        builder.add(InlineKeyboardButton(text=f"⚙️ {name} ({paid_count}/{limit})", callback_data=f"srv_manage_{s_id}"))
    
    builder.add(InlineKeyboardButton(text="➕ Добавить сервер", callback_data="adm_add_srv"))
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_menu"))
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("srv_manage_"))
async def manage_single_server(callback: CallbackQuery):
    s_id = int(callback.data.split("_")[2])
    server = db.get_server_by_id(s_id)
    if not server:
        await callback.answer("Сервер не найден!")
        return
    
    ip, port, name, limit = server[0], server[1], server[2], server[3]
    paid_count = db.count_paid_users_on_server(s_id)
    
    text = f"⚙️ <b>Сервер:</b> {name}\n"
    text += f"🌐 <b>IP:</b> {ip}\n"
    text += f"👥 <b>Платные клиенты:</b> {paid_count} из {limit}\n\nВыберите действие:"
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📝 Переименовать", callback_data=f"srv_ren_{s_id}"))
    builder.add(InlineKeyboardButton(text="📈 Изменить лимит", callback_data=f"srv_lim_{s_id}"))
    builder.add(InlineKeyboardButton(text="❌ Удалить сервер", callback_data=f"srv_del_{s_id}"))
    builder.add(InlineKeyboardButton(text="⬅️ К списку серверов", callback_data="adm_servers_menu"))
    builder.adjust(2, 1, 1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("srv_ren_"))
async def srv_rename_start(callback: CallbackQuery, state: FSMContext):
    s_id = int(callback.data.split("_")[2])
    await state.update_data(current_srv_id=s_id)
    await callback.message.answer("Введите новое понятное имя для этого сервера (например: 🇩🇪 Германия 1):")
    await state.set_state(AdminStates.waiting_for_rename)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_rename)
async def srv_rename_proc(message: Message, state: FSMContext):
    new_name = message.text.strip()
    data = await state.get_data()
    s_id = data.get("current_srv_id")
    db.rename_server(s_id, new_name)
    await message.answer(f"✅ Сервер переименован в «{new_name}».", reply_markup=get_admin_keyboard())
    await state.clear()

@admin_router.callback_query(F.data.startswith("srv_lim_"))
async def srv_limit_start(callback: CallbackQuery, state: FSMContext):
    s_id = int(callback.data.split("_")[2])
    await state.update_data(current_srv_id=s_id)
    await callback.message.answer("Введите максимальное количество платных мест для этого сервера (цифрами):")
    await state.set_state(AdminStates.waiting_for_limit)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_limit)
async def srv_limit_proc(message: Message, state: FSMContext):
    limit = message.text.strip()
    if not limit.isdigit():
        return await message.answer("Пожалуйста, введите число.")
    data = await state.get_data()
    s_id = data.get("current_srv_id")
    db.set_server_limit(s_id, int(limit))
    await message.answer(f"✅ Лимит сервера установлен: {limit} мест.", reply_markup=get_admin_keyboard())
    await state.clear()

@admin_router.callback_query(F.data.startswith("srv_del_"))
async def srv_del_proc(callback: CallbackQuery):
    s_id = int(callback.data.split("_")[2])
    db.delete_server(s_id)
    await callback.answer("Сервер удален из базы", show_alert=True)
    await show_servers_menu(callback)

# --- ДОБАВЛЕНИЕ СЕРВЕРА ---
@admin_router.callback_query(F.data == "adm_add_srv")
async def add_srv_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Шаг 1: Пришлите IP-адрес нового сервера.\n(Ключи SSH должны быть уже скопированы)")
    await state.set_state(AdminStates.waiting_for_ip)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_ip)
async def add_srv_ip(message: Message, state: FSMContext):
    ip = message.text.strip()
    await state.update_data(new_ip=ip)
    await message.answer(f"IP {ip} принят.\nШаг 2: Введите красивое название для этого сервера (его увидят клиенты):")
    await state.set_state(AdminStates.waiting_for_srv_name)

@admin_router.message(AdminStates.waiting_for_srv_name)
async def add_srv_proc(message: Message, state: FSMContext):
    name = message.text.strip()
    data = await state.get_data()
    ip = data.get("new_ip")
    
    msg = await message.answer(f"⏳ Подключаюсь к {ip} и заливаю скрипты...")
    await ssh.setup_new_server(ip, 2222)
    
    if db.add_server(ip, name, 2222):
        await msg.edit_text(f"✅ Сервер «{name}» ({ip}) успешно настроен и добавлен в базу!", reply_markup=get_admin_keyboard())
    else:
        await msg.edit_text("⚠️ Ошибка: Этот IP уже есть в базе данных.", reply_markup=get_admin_keyboard())
    await state.clear()

# --- СТАТИСТИКА И ЮЗЕРЫ ---
@admin_router.callback_query(F.data == "adm_stats")
async def show_stats(callback: CallbackQuery):
    servers = db.get_active_servers()
    all_subs = db.get_all_users()
    unique_users = db.get_unique_user_ids()
    
    text = f"📈 ОБЩАЯ СТАТИСТИКА:\n\n"
    text += f"🖥 Всего серверов: {len(servers)}\n"
    text += f"👤 Уникальных клиентов: {len(unique_users)}\n"
    text += f"🟢 Активных конфигураций: {len([u for u in all_subs if u[4] == 1])}\n\n"
    
    text += "ℹ️ Трафик по серверам:\n"
    for s_id, ip, port, name, limit in servers:
        traffic_info = await ssh.get_server_traffic(ip, port)
        text += f"\n⚙️ Сервер {name} ({ip}):\n{traffic_info}\n"
        
    builder = InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_menu"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup())

@admin_router.callback_query(F.data == "adm_users")
async def show_users(callback: CallbackQuery):
    users = db.get_all_users()
    if not users:
        return await callback.message.edit_text("Пользователей пока нет.", reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_menu")).as_markup())
        
    active_list, inactive_list = [], []
    for tg_id, s_id, username, expire, active in users:
        server = db.get_server_by_id(s_id)
        srv_name = server[2] if server else f"ID {s_id}"
        
        profile = db.get_user_profile(tg_id)
        if profile:
            full_name = str(profile[0]).replace("<", "").replace(">", "") 
            tg_alias = f" (@{profile[1]})" if profile[1] else ""
            client_name = f"<b>{full_name}</b>{tg_alias}"
        else:
            # Заглушка, если имени нет (чтобы не дублировать ID)
            client_name = f"<b>Аноним</b>"
        
        # Теперь ID выводится всегда и копируется по клику
        info = f"👤 {client_name} | ID: <code>{tg_id}</code> | 🖥 {srv_name} | До: {expire}"
        
        if active == 1: active_list.append(info)
        else: inactive_list.append(info)
            
    text = "👥 <b>СПИСОК КЛИЕНТОВ (ПОДПИСКИ):</b>\n\n🟢 <b>АКТИВНЫЕ:</b>\n"
    text += "\n".join(active_list) if active_list else "Нет активных"
    text += "\n\n🔴 <b>НЕАКТИВНЫЕ (ИСТЕКШИЕ):</b>\n"
    text += "\n".join(inactive_list) if inactive_list else "Нет неактивных"
    
    if len(text) > 4000: text = text[:4000] + "\n... (список обрезан)"
        
    builder = InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_menu"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@admin_router.callback_query(F.data == "adm_ask_del_user")
async def ask_delete_user(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите Telegram ID пользователя, конфигурации которого нужно удалить (только цифры):")
    await state.set_state(AdminStates.waiting_for_user_id_to_delete)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_user_id_to_delete)
async def process_delete_user(message: Message, state: FSMContext):
    user_id_str = message.text.strip()
    if not user_id_str.isdigit():
        return await message.answer("ID должен состоять только из цифр. Попробуйте еще раз.")
    tg_id = int(user_id_str)
    subs = db.get_user_subs(tg_id)

    if not subs:
        await message.answer(f"Пользователь с ID {tg_id} не найден в базе данных или у него нет подписок.")
        return await state.clear()

    builder = InlineKeyboardBuilder()
    for sub in subs:
        s_id = sub[1]
        active = sub[5]
        server = db.get_server_by_id(s_id)
        srv_name = server[2] if server else f"Сервер {s_id}"
        status = "🟢" if active == 1 else "🔴"
        builder.add(InlineKeyboardButton(text=f"🗑 {status} Удалить с: {srv_name}", callback_data=f"adm_del_{tg_id}_{s_id}"))
        
    builder.add(InlineKeyboardButton(text="💣 Удалить со всех серверов", callback_data=f"adm_del_{tg_id}_all"))
    builder.adjust(1)
    await message.answer(f"Найдено несколько подписок для пользователя <code>{tg_id}</code>.\nВыберите, откуда удалить конфигурацию:", reply_markup=builder.as_markup(), parse_mode="HTML")
    await state.clear()

@admin_router.callback_query(F.data.startswith("adm_del_"))
async def process_delete_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    tg_id = int(parts[2])
    target = parts[3]
    
    if target == "all":
        subs = db.get_user_subs(tg_id)
        for sub in subs:
            s_id, uname = sub[1], sub[2]
            server = db.get_server_by_id(s_id)
            if server:
                ip, port = server[0], server[1]
                await ssh.run_ssh_command(ip, port, f"bash /root/remove_user.sh {uname}")
        db.delete_user_completely(tg_id)
        await callback.message.edit_text(f"✅ Пользователь {tg_id} полностью удален со всех серверов.")
    else:
        s_id = int(target)
        sub = db.get_user_sub(tg_id, s_id)
        if sub:
            uname = sub[2]
            server = db.get_server_by_id(s_id)
            if server:
                ip, port, srv_name = server[0], server[1], server[2]
                await callback.message.answer(f"⏳ Удаляю профиль с сервера {srv_name}...")
                await ssh.run_ssh_command(ip, port, f"bash /root/remove_user.sh {uname}")
            db.delete_user_completely(tg_id, s_id)
            await callback.message.edit_text(f"✅ Конфиг пользователя {tg_id} успешно удален с выбранного сервера.")

# --- РУЧНАЯ ВЫДАЧА КОНФИГА ---
@admin_router.callback_query(F.data == "adm_issue_config")
async def manual_issue_start(callback: CallbackQuery, state: FSMContext):
    servers = db.get_active_servers()
    if not servers: return await callback.answer("Нет серверов!", show_alert=True)
    
    text = "<b>Доступные сервера:</b>\n"
    for s_id, ip, port, name, limit in servers: text += f"ID: <code>{s_id}</code> | {name} ({ip})\n"
    text += "\nОтправьте <b>ID сервера</b> (только цифру), с которого нужно выдать конфиг:"
    
    await callback.message.answer(text, parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_manual_srv)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_manual_srv)
async def manual_srv_proc(message: Message, state: FSMContext):
    if not message.text.isdigit(): return
    await state.update_data(man_srv_id=int(message.text))
    await message.answer("Отправьте <b>Telegram ID</b> пользователя (только цифры):", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_manual_user)

@admin_router.message(AdminStates.waiting_for_manual_user)
async def manual_user_proc(message: Message, state: FSMContext):
    if not message.text.isdigit(): return
    await state.update_data(man_tg_id=int(message.text))
    await message.answer("На какое количество <b>ЧАСОВ</b> выдать доступ? (например, 720 для 30 дней):", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_for_manual_hours)

@admin_router.message(AdminStates.waiting_for_manual_hours)
async def manual_hours_proc(message: Message, state: FSMContext):
    if not message.text.isdigit(): return
    hours = int(message.text)
    data = await state.get_data()
    s_id, tg_id = data['man_srv_id'], data['man_tg_id']
    
    server = db.get_server_by_id(s_id)
    if not server:
        await message.answer("Сервер не найден в базе.")
        return await state.clear()
        
    await message.answer("⏳ Подключаюсь к серверу и генерирую конфиг...")
    ip, port, srv_name = server[0], server[1], server[2]
    username = f"user_{tg_id}"
    cmd = f"bash /root/add_user.sh {username}"
    config_text = await ssh.run_ssh_command(ip, port, cmd)
    
    if not config_text or "Ошибка" in config_text:
        await message.answer(f"❌ Ошибка генерации:\n<code>{html.quote(config_text)}</code>", parse_mode="HTML")
        return await state.clear()
        
    db.add_or_update_user(tg_id, s_id, username, hours=hours, is_trial=False)
    config_file = BufferedInputFile(config_text.encode('utf-8'), filename=f"ID{s_id}AWG.conf")
    
    try:
        await message.bot.send_document(tg_id, config_file, caption=f"🎁 Администратор выдал вам доступ к VPN на {hours} ч.\nЛокация: {srv_name}")
        await message.answer(f"✅ Конфиг успешно отправлен пользователю {tg_id}.")
    except Exception:
        config_file_admin = BufferedInputFile(config_text.encode('utf-8'), filename=f"ID{s_id}AWG.conf")
        await message.answer("⚠️ Пользователь не запускал бота. Пересылаю конфиг вам:")
        await message.answer_document(config_file_admin, caption=f"Конфиг для ID {tg_id}")
    await state.clear()

# --- ЧАТ И РАССЫЛКА ---
@admin_router.callback_query(F.data.startswith("reply_to_"))
async def start_reply(callback: CallbackQuery, state: FSMContext):
    user_id = callback.data.split("_")[2]
    await state.update_data(reply_user_id=user_id)
    await callback.message.answer(f"Введите текст ответа для пользователя {user_id}:")
    await state.set_state(AdminStates.waiting_for_reply_text)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_reply_text)
async def send_reply(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        await message.bot.send_message(data.get("reply_user_id"), f"👨‍💻 <b>Ответ поддержки:</b>\n\n{message.text}", parse_mode="HTML")
        await message.answer("✅ Ответ доставлен!")
    except Exception:
        await message.answer("❌ Ошибка доставки.")
    await state.clear()

@admin_router.callback_query(F.data == "adm_broadcast")
async def ask_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите текст для рассылки всем пользователям:")
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_broadcast)
async def send_broadcast(message: Message, state: FSMContext):
    users = db.get_unique_user_ids()
    count = 0
    await message.answer("⏳ Начинаю рассылку...")
    for uid in users:
        try:
            await message.bot.send_message(uid, f"📢 <b>Объявление:</b>\n\n{message.text}", parse_mode="HTML")
            count += 1
        except Exception: pass
    await message.answer(f"✅ Рассылка завершена. Успешно доставлено: {count} чел.")
    await state.clear()

# --- НАСТРОЙКИ БОТА ---
@admin_router.callback_query(F.data == "adm_settings")
async def view_settings(callback: CallbackQuery):
    trial = db.get_setting("trial_hours")
    p1 = db.get_setting("price_1d")
    p7 = db.get_setting("price_7d")
    p30 = db.get_setting("price_30d")
    aipay_key = db.get_setting("aipay_api_key")
    aipay_status = f"✅ Задан (...{aipay_key[-4:]})" if aipay_key else "❌ Не задан"
    provider = (db.get_setting("payment_provider") or "off").strip().lower()
    prov_names = {"off": "❌ Выключена", "aipay": "AiPay (старый)", "platega": "Platega"}
    prov_status = prov_names.get(provider, provider)
    platega_mid = db.get_setting("platega_merchant_id")
    platega_sec = db.get_setting("platega_secret")
    platega_mid_status = f"✅ Задан (...{platega_mid[-4:]})" if platega_mid else "❌ Не задан"
    platega_sec_status = f"✅ Задан (...{platega_sec[-4:]})" if platega_sec else "❌ Не задан"
    platega_method = db.get_setting("platega_payment_method") or "2"
    site_url = db.get_setting("site_url") or "не задан"
    
    text = "⚙️ ГЛОБАЛЬНЫЕ ТАРИФЫ:\n\n"
    text += f"🎁 Пробный период: {trial} час(ов)\n"
    text += f"💵 Цена за 1 день: {p1} руб.\n"
    text += f"💵 Цена за 7 дней: {p7} руб.\n"
    text += f"💵 Цена за 30 дней: {p30} руб.\n\n"
    text += f"💳 Автооплата: <b>{prov_status}</b>\n"
    text += f"🔑 AiPay API-ключ: {aipay_status}\n"
    text += f"🆔 Platega Merchant ID: {platega_mid_status}\n"
    text += f"🔑 Platega Secret: {platega_sec_status}\n"
    text += f"🧾 Platega ID метода оплаты: {platega_method}\n"
    text += f"🌐 Адрес сайта: {site_url}\n"
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🖥 Цены серверов", callback_data="adm_srv_prices_menu"))
    builder.add(InlineKeyboardButton(text="💳 Реквизиты оплаты", callback_data="set_manual_payment_details"))
    builder.add(InlineKeyboardButton(text="🔁 Автооплата: вкл/выкл/провайдер", callback_data="pay_provider_switch"))
    builder.add(InlineKeyboardButton(text="🔑 API-ключ AiPay", callback_data="set_aipay_api_key"))
    builder.add(InlineKeyboardButton(text="🆔 Platega Merchant ID", callback_data="set_platega_merchant"))
    builder.add(InlineKeyboardButton(text="🔑 Platega Secret", callback_data="set_platega_secret"))
    builder.add(InlineKeyboardButton(text="🧾 Platega ID метода", callback_data="set_platega_payment_method"))
    builder.add(InlineKeyboardButton(text="🌐 Адрес сайта", callback_data="set_site_url"))
    builder.add(InlineKeyboardButton(text="📝 Тест (часы)", callback_data="set_trial_hours"))
    builder.add(InlineKeyboardButton(text="📝 1 день", callback_data="set_price_1d"))
    builder.add(InlineKeyboardButton(text="📝 7 дней", callback_data="set_price_7d"))
    builder.add(InlineKeyboardButton(text="📝 30 дней", callback_data="set_price_30d"))
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_menu"))
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@admin_router.callback_query(F.data == "set_aipay_api_key")
async def set_aipay_key_start(callback: CallbackQuery, state: FSMContext):
    current = db.get_setting("aipay_api_key")
    masked = f"...{current[-4:]}" if current else "не задан"
    await callback.message.answer(
        f"Текущий API-ключ AiPay: <code>{masked}</code>\n\n"
        f"Введите новый API-ключ AiPay (создается в личном кабинете lks.aipay.onl):",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_aipay_key)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_aipay_key)
async def set_aipay_key_proc(message: Message, state: FSMContext):
    db.update_setting("aipay_api_key", message.text.strip())
    await message.answer("✅ API-ключ AiPay успешно сохранен!", reply_markup=get_admin_keyboard())
    await state.clear()

# --- ПЕРЕКЛЮЧЕНИЕ ПЛАТЕЖНОГО ПРОВАЙДЕРА (off -> platega -> aipay -> off) ---
@admin_router.callback_query(F.data == "pay_provider_switch")
async def pay_provider_switch(callback: CallbackQuery):
    cur = (db.get_setting("payment_provider") or "off").strip().lower()
    nxt = {"off": "platega", "platega": "aipay", "aipay": "off"}.get(cur, "off")
    db.update_setting("payment_provider", nxt)
    names = {"off": "❌ Выключена", "aipay": "AiPay", "platega": "Platega"}
    await callback.answer(f"Автооплата: {names[nxt]}")
    await view_settings(callback)

@admin_router.callback_query(F.data == "set_platega_merchant")
async def set_platega_merchant_start(callback: CallbackQuery, state: FSMContext):
    current = db.get_setting("platega_merchant_id")
    masked = f"...{current[-4:]}" if current else "не задан"
    await callback.message.answer(
        f"Текущий Platega Merchant ID: <code>{masked}</code>\n\n"
        f"Введите Merchant ID (UUID). Его выдает менеджер Platega, "
        f"он также есть в личном кабинете my.platega.io на странице «Настройки»:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_platega_merchant)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_platega_merchant)
async def set_platega_merchant_proc(message: Message, state: FSMContext):
    db.update_setting("platega_merchant_id", message.text.strip())
    await message.answer("✅ Platega Merchant ID сохранен!", reply_markup=get_admin_keyboard())
    await state.clear()

@admin_router.callback_query(F.data == "set_platega_secret")
async def set_platega_secret_start(callback: CallbackQuery, state: FSMContext):
    current = db.get_setting("platega_secret")
    masked = f"...{current[-4:]}" if current else "не задан"
    await callback.message.answer(
        f"Текущий Platega Secret: <code>{masked}</code>\n\n"
        f"Введите Secret (API-ключ). Его выдает менеджер Platega, "
        f"он также есть в личном кабинете my.platega.io на странице «Настройки»:",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_platega_secret)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_platega_secret)
async def set_platega_secret_proc(message: Message, state: FSMContext):
    db.update_setting("platega_secret", message.text.strip())
    await message.answer("✅ Platega Secret сохранен!", reply_markup=get_admin_keyboard())
    await state.clear()

@admin_router.callback_query(F.data == "set_site_url")
async def set_site_url_start(callback: CallbackQuery, state: FSMContext):
    current = db.get_setting("site_url") or "не задан"
    await callback.message.answer(
        f"Текущий адрес сайта: <code>{current}</code>\n\n"
        f"Введите новый адрес сайта БЕЗ слеша в конце (например: https://amneziawg.fun).\n"
        f"Он используется для кнопки входа через Telegram на сайте.",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.waiting_for_site_url)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_site_url)
async def set_site_url_proc(message: Message, state: FSMContext):
    db.update_setting("site_url", message.text.strip().rstrip("/"))
    await message.answer("✅ Адрес сайта сохранен! Не забудьте также указать этот домен в @BotFather (/setdomain).", reply_markup=get_admin_keyboard())
    await state.clear()

@admin_router.callback_query(F.data == "set_manual_payment_details")
async def set_man_det(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите новые реквизиты и инструкции для ручной оплаты:")
    await state.set_state(AdminStates.waiting_for_manual_details)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_manual_details)
async def set_man_det_proc(message: Message, state: FSMContext):
    db.update_setting("manual_payment_details", message.text)
    await message.answer("✅ Реквизиты успешно сохранены!", reply_markup=get_admin_keyboard())
    await state.clear()

@admin_router.callback_query(
    F.data.startswith("set_")
    & ~F.data.startswith("set_srv_")
    & (F.data != "set_manual_payment_details")
    & (F.data != "set_aipay_api_key")
    & (F.data != "set_platega_merchant")
    & (F.data != "set_platega_secret")
    & (F.data != "set_site_url")
)
async def change_setting_start(callback: CallbackQuery, state: FSMContext):
    key = callback.data.replace("set_", "")
    await state.update_data(current_key=key)
    await callback.message.answer(f"Введите новое числовое значение для параметра `{key}`:")
    await state.set_state(AdminStates.waiting_for_setting_value)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_setting_value)
async def change_setting_proc(message: Message, state: FSMContext):
    val = message.text.strip()
    if not val.isdigit(): return await message.answer("Пожалуйста, введите целое число.")
    data = await state.get_data()
    db.update_setting(data.get("current_key"), val)
    await message.answer("✅ Параметр успешно обновлен!", reply_markup=get_admin_keyboard())
    await state.clear()

# --- ЦЕНЫ ДЛЯ КОНКРЕТНЫХ СЕРВЕРОВ ---
@admin_router.callback_query(F.data == "adm_srv_prices_menu")
async def srv_prices_menu(callback: CallbackQuery):
    servers = db.get_active_servers()
    builder = InlineKeyboardBuilder()
    text = "🖥 <b>ВЫБОР СЕРВЕРА ДЛЯ НАСТРОЙКИ ЦЕН</b>\n\nВыберите сервер:"
    for s_id, ip, port, name, limit in servers:
        builder.add(InlineKeyboardButton(text=f"⚙️ {name}", callback_data=f"adm_srv_price_{s_id}"))
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_settings"))
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("adm_srv_price_"))
async def srv_price_edit(callback: CallbackQuery):
    s_id = int(callback.data.split("_")[3])
    server = db.get_server_by_id(s_id)
    if not server: return
    
    if len(server) >= 7:
        name, p1, p7, p30 = server[2], server[4], server[5], server[6]
    else:
        name = server[2]
        p1, p7, p30 = None, None, None
    
    c_p1 = p1 if p1 is not None else db.get_setting("price_1d")
    c_p7 = p7 if p7 is not None else db.get_setting("price_7d")
    c_p30 = p30 if p30 is not None else db.get_setting("price_30d")
    
    text = (
        f"⚙️ <b>Цены для сервера: {name}</b>\n\n"
        f"💵 1 день: {c_p1} руб.\n"
        f"💵 7 дней: {c_p7} руб.\n"
        f"💵 30 дней: {c_p30} руб.\n\n"
        f"<i>Если цена не установлена, используется глобальная.</i>"
    )
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📝 1 день", callback_data=f"set_srv_{s_id}_price_1d"))
    builder.add(InlineKeyboardButton(text="📝 7 дней", callback_data=f"set_srv_{s_id}_price_7d"))
    builder.add(InlineKeyboardButton(text="📝 30 дней", callback_data=f"set_srv_{s_id}_price_30d"))
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_srv_prices_menu"))
    builder.adjust(3, 1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("set_srv_"))
async def set_srv_price_start(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    s_id = int(parts[2])
    period = parts[3] + "_" + parts[4] 
    await state.update_data(srv_price_id=s_id, srv_price_period=period)
    await callback.message.answer(f"Введите новую цену для тарифа `{period}` (только цифры):")
    await state.set_state(AdminStates.waiting_for_srv_price)
    await callback.answer()

@admin_router.message(AdminStates.waiting_for_srv_price)
async def set_srv_price_proc(message: Message, state: FSMContext):
    val = message.text.strip()
    if not val.isdigit(): return await message.answer("Пожалуйста, введите целое число.")
    data = await state.get_data()
    db.update_server_price(data.get("srv_price_id"), data.get("srv_price_period"), int(val))
    await message.answer("✅ Цена сервера успешно обновлена!", reply_markup=get_admin_keyboard())
    await state.clear()

# --- ОБРАБОТКА РУЧНЫХ ПЛАТЕЖЕЙ И ЗАПРОСОВ РЕКВИЗИТОВ ---

@admin_router.callback_query(F.data.startswith("adm_app_det_"))
async def approve_details_request(callback: CallbackQuery):
    parts = callback.data.split("_")
    tg_id = int(parts[3])
    s_id = parts[4]
    period = parts[5]
    
    details = db.get_setting("manual_payment_details") or "Реквизиты не заданы администратором."
    
    user_text = (
        f"✅ <b>Ваш запрос одобрен!</b>\n\n"
        f"Пожалуйста, оплатите по следующим реквизитам:\n"
        f"<code>{details}</code>\n\n"
        f"После успешного перевода нажмите кнопку ниже, чтобы прислать скриншот чека."
    )
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📎 Отправить чек", callback_data=f"send_receipt_{s_id}_{period}"))
    
    try:
        await callback.bot.send_message(tg_id, user_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Реквизиты успешно отправлены.</b>", parse_mode="HTML")
    except Exception:
        await callback.answer("⚠️ Не удалось отправить сообщение пользователю (возможно, он заблокировал бота).", show_alert=True)

@admin_router.callback_query(F.data.startswith("adm_rej_det_"))
async def reject_details_request(callback: CallbackQuery):
    parts = callback.data.split("_")
    tg_id = int(parts[3])
    
    user_text = "❌ <b>Отказ.</b> Администратор отклонил ваш запрос на получение реквизитов для оплаты."
    
    try:
        await callback.bot.send_message(tg_id, user_text, parse_mode="HTML")
        await callback.message.edit_text(callback.message.text + "\n\n❌ <b>Запрос успешно отклонен.</b>", parse_mode="HTML")
    except Exception:
        await callback.answer("⚠️ Не удалось отправить сообщение пользователю.", show_alert=True)

# --- ТО ЖЕ САМОЕ, НО ДЛЯ ЗАПРОСОВ РЕКВИЗИТОВ С САЙТА ---
# Сделано отдельным гейтом (своя таблица detail_requests), а не через adm_app_det_/adm_rej_det_,
# чтобы сайт не зависел от того, открыт ли у пользователя чат с ботом - статус просто
# опрашивается со страницы через /web/check_details/{id}.

@admin_router.callback_query(F.data.startswith("web_app_det_"))
async def approve_web_details_request(callback: CallbackQuery):
    request_id = int(callback.data.split("_")[3])
    req = db.get_detail_request(request_id)
    if not req or req[4] != 'pending':
        return await callback.answer("Запрос уже обработан или не найден.", show_alert=True)

    if db.set_detail_request_status(request_id, "approved"):
        await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Одобрено (сайт).</b>", parse_mode="HTML")
    await callback.answer("Одобрено!")

@admin_router.callback_query(F.data.startswith("web_rej_det_"))
async def reject_web_details_request(callback: CallbackQuery):
    request_id = int(callback.data.split("_")[3])
    req = db.get_detail_request(request_id)
    if not req or req[4] != 'pending':
        return await callback.answer("Запрос уже обработан или не найден.", show_alert=True)

    if db.set_detail_request_status(request_id, "rejected"):
        await callback.message.edit_text(callback.message.text + "\n\n❌ <b>Отклонено (сайт).</b>", parse_mode="HTML")
    await callback.answer("Отклонено.")

@admin_router.callback_query(F.data.startswith("adm_conf_man_"))
async def confirm_manual_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    order = db.get_manual_order(order_id)
    if not order or order[5] != 'pending':
        return await callback.answer("Заказ уже обработан или не найден", show_alert=True)
        
    tg_id, s_id, period = order[1], order[2], order[3]
    db.update_manual_order_status(order_id, 'paid')
    
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ <b>ОДОБРЕНО И ВЫДАНО</b>", parse_mode="HTML")
    
    success, msg = await issue_vpn_access(callback.bot, tg_id, s_id, period)
    if not success:
        await callback.message.answer(f"⚠️ Ошибка при выдаче конфига пользователю {tg_id}:\n{msg}")

@admin_router.callback_query(F.data.startswith("adm_decl_man_"))
async def decline_manual_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    order = db.get_manual_order(order_id)
    if not order or order[5] != 'pending':
        return await callback.answer("Заказ уже обработан или не найден", show_alert=True)
        
    tg_id = order[1]
    db.update_manual_order_status(order_id, 'declined')
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ <b>ОТКЛОНЕНО</b>", parse_mode="HTML")
    
    try:
        await callback.bot.send_message(tg_id, "❌ <b>Ваш платеж отклонен администратором.</b>\nЕсли вы считаете это ошибкой, обратитесь в поддержку.", parse_mode="HTML")
    except:
        pass