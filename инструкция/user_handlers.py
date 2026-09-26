import hashlib
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from datetime import datetime
import database as db
import ssh_manager as ssh
from aiogram import html

user_router = Router()
ADMIN_ID = 1617274846 

FK_MERCHANT_ID = "74582"
FK_SECRET_WORD_1 = "pizdollet[12" 

def generate_payment_link(amount: int, order_id: int, currency: str = "RUB"):
    sign_string = f"{FK_MERCHANT_ID}:{amount}:{FK_SECRET_WORD_1}:{currency}:{order_id}"
    signature = hashlib.md5(sign_string.encode('utf-8')).hexdigest()
    link = f"https://pay.freekassa.ru/?m={FK_MERCHANT_ID}&oa={amount}&o={order_id}&s={signature}&currency={currency}"
    return link

class SupportStates(StatesGroup):
    waiting_for_question = State()

class BuyStates(StatesGroup):
    waiting_for_receipt = State()

main_reply_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🚀 Главное меню")]], 
    resize_keyboard=True,
    is_persistent=True
)

def get_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🛍 Купить / Продлить VPN", callback_data="usr_buy_choose_srv"))
    builder.add(InlineKeyboardButton(text="👤 Мой профиль", callback_data="usr_profile"))
    builder.add(InlineKeyboardButton(text="ℹ️ Описание и условия", callback_data="usr_description"))
    builder.add(InlineKeyboardButton(text="📚 Инструкция по настройке", callback_data="usr_help"))
    builder.add(InlineKeyboardButton(text="🤝 Поддержка", callback_data="usr_support"))
    builder.adjust(1, 1, 1, 2)
    return builder.as_markup()

async def check_and_clean_expired(tg_id: int):
    subs = db.get_user_subs(tg_id)
    if not subs: return "not_found"

    has_expired = False
    for sub in subs:
        t_id, s_id, uname, exp_date_str, has_trial, active, is_trial, notif, last_exp = sub
        if exp_date_str and active == 1:
            try:
                expire_date = datetime.strptime(exp_date_str, "%Y-%m-%d %H:%M:%S.%f")
                if datetime.now() > expire_date:
                    server = db.get_server_by_id(s_id)
                    if server:
                        ip, port = server[0], server[1]
                        cmd = f"bash /root/remove_user.sh {uname}"
                        await ssh.run_ssh_command(ip, port, cmd)
                    db.deactivate_user(t_id, s_id)
                    has_expired = True
            except Exception:
                pass
    return "expired" if has_expired else "ok"

@user_router.message(Command("start"))
async def start_cmd(message: Message, state: FSMContext):
    await state.clear() 
    await message.answer("👋 Добро пожаловать! Здесь можно управлять подпиской и конфигурациями сервиса AmneziaWG.\n\nВыберите интересующий раздел меню:", reply_markup=main_reply_kb)
    await message.answer("Навигация:", reply_markup=get_main_keyboard())

@user_router.message(F.text == "🚀 Главное меню")
async def main_menu_btn(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=get_main_keyboard())

@user_router.callback_query(F.data == "usr_menu")
async def user_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Выберите интересующий раздел меню:", reply_markup=get_main_keyboard())

@user_router.callback_query(F.data == "usr_profile")
async def show_profile(callback: CallbackQuery):
    await check_and_clean_expired(callback.from_user.id)
    subs = db.get_user_subs(callback.from_user.id)
    active_subs = [s for s in subs if s[5] == 1]
    
    if not active_subs:
        text = "👤 МОЙ ПРОФИЛЬ:\n\nУ вас нет активных подписок. Приобретите их в меню покупки!"
    else:
        text = f"👤 МОЙ ПРОФИЛЬ (ID: `{callback.from_user.id}`):\n\n"
        for sub in active_subs:
            t_id, s_id, uname, exp_date_str, has_trial, active, is_trial, notif, last_exp = sub
            expire_date = datetime.strptime(exp_date_str, "%Y-%m-%d %H:%M:%S.%f")
            days_left = (expire_date - datetime.now()).days
            hours_left = round((expire_date - datetime.now()).seconds / 3600, 1)
            srv = db.get_server_by_id(s_id)
            srv_name = srv[2] if srv else f"ID {s_id}"
            
            text += f"🖥 <b>Сервер: {srv_name}</b>\n"
            text += f"📅 Активно до: {expire_date.strftime('%d.%m.%Y %H:%M')}\n"
            text += f"⏳ Осталось: {days_left} дн. ({hours_left} ч.)\n"
            if is_trial == 1:
                text += "🎁 <i>Пробный период</i>\n"
            text += "\n"

    builder = InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ В меню", callback_data="usr_menu"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data == "usr_description")
async def show_description(callback: CallbackQuery):
    text = (
        "ℹ️ <b>ОПИСАНИЕ СЕРВИСА</b>\n\n"
        "Сервис позволяет оформить доступ, получить конфигурационный файл и управлять подпиской через Telegram-бот или личный кабинет.\n\n"
        "📱 <b>Поддерживаемые платформы:</b>\n"
        "Приложение AmneziaWG доступно для Android, iOS и компьютеров.\n\n"
        "🧾 <b>Тарифы и доступ:</b>\n"
        "Доступные локации, сроки и стоимость показываются перед оформлением заказа.\n\n"
        "С документами сервиса можно ознакомиться по отдельным кнопкам в главном меню."
    )
    builder = InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ В меню", callback_data="usr_menu"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data == "usr_buy_choose_srv")
async def select_server(callback: CallbackQuery):
    await check_and_clean_expired(callback.from_user.id)
    servers = db.get_active_servers()
    
    if not servers:
        await callback.message.edit_text("Извините, в данный момент нет доступных серверов.", reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ Назад", callback_data="usr_menu")).as_markup())
        return

    text = "🌍 <b>ВЫБОР СЕРВЕРА</b>\n\nПожалуйста, выберите локацию, к которой хотите подключиться:"
    builder = InlineKeyboardBuilder()
    
    for s_id, ip, port, name, limit in servers:
        paid_count = db.count_paid_users_on_server(s_id)
        if paid_count >= limit:
            builder.add(InlineKeyboardButton(text=f"🔴 {name} [МЕСТ НЕТ]", callback_data=f"srv_full"))
        else:
            builder.add(InlineKeyboardButton(text=f"🟢 {name}", callback_data=f"buy_srv_{s_id}"))

    builder.add(InlineKeyboardButton(text="⬅️ В меню", callback_data="usr_menu"))
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data == "srv_full")
async def srv_full_alert(callback: CallbackQuery):
    await callback.answer("Увы, на этом сервере закончились свободные места. Пожалуйста, выберите другой или подождите!", show_alert=True)

@user_router.callback_query(F.data.startswith("buy_srv_"))
async def buy_menu(callback: CallbackQuery):
    s_id = int(callback.data.replace("buy_srv_", ""))
    u = db.get_user_sub(callback.from_user.id, s_id)
    
    server = db.get_server_by_id(s_id)
    ip, port, srv_name, limit, sp1, sp7, sp30 = server

    can_take_trial = True
    if u and u[5] == 1: 
        can_take_trial = False 
    elif u and u[8]: 
        last_exp = datetime.strptime(u[8], "%Y-%m-%d %H:%M:%S.%f")
        days_passed = (datetime.now() - last_exp).days
        if days_passed < 30:
            can_take_trial = False

    t_hours = db.get_setting("trial_hours")
    
    p1 = sp1 if sp1 is not None else db.get_setting("price_1d")
    p7 = sp7 if sp7 is not None else db.get_setting("price_7d")
    p30 = sp30 if sp30 is not None else db.get_setting("price_30d")
    
    text = f"🛍 <b>ОФОРМЛЕНИЕ ПОДПИСКИ</b>\n📍 Локация: {srv_name}\n\nВыберите желаемый период использования:"
    
    builder = InlineKeyboardBuilder()
    if can_take_trial:
        builder.add(InlineKeyboardButton(text=f"🎁 Бесплатный тест ({t_hours}ч)", callback_data=f"pay_success_{s_id}_trial"))
    else:
        text += "\n\n<i>⚠️ Бесплатный тест недоступен. Он выдается 1 раз в 30 дней при отсутствии активной подписки.</i>"
    
    builder.add(InlineKeyboardButton(text=f"⏳ 1 День — {p1} руб.", callback_data=f"pay_fk_{s_id}_1d"))
    builder.add(InlineKeyboardButton(text=f"⏳ 7 Дней — {p7} руб.", callback_data=f"pay_fk_{s_id}_7d"))
    builder.add(InlineKeyboardButton(text=f"⏳ 30 Дней — {p30} руб.", callback_data=f"pay_fk_{s_id}_30d"))
    builder.add(InlineKeyboardButton(text="⬅️ Выбрать другой сервер", callback_data="usr_buy_choose_srv"))
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data.startswith("pay_fk_"))
async def process_checkout(callback: CallbackQuery):
    _, _, s_id_str, period = callback.data.split("_")
    server_id = int(s_id_str)
    tg_id = callback.from_user.id
    
    server = db.get_server_by_id(server_id)
    sp1, sp7, sp30 = server[4], server[5], server[6]
    
    if period == "1d": amount = int(sp1 if sp1 is not None else db.get_setting("price_1d"))
    elif period == "7d": amount = int(sp7 if sp7 is not None else db.get_setting("price_7d"))
    elif period == "30d": amount = int(sp30 if sp30 is not None else db.get_setting("price_30d"))
    else: amount = 0
        
    if amount <= 0:
        return await callback.answer("Ошибка: цена тарифа не настроена.", show_alert=True)
    
    order_id = db.create_order(tg_id, server_id, period, amount)
    payment_url = generate_payment_link(amount, order_id)
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="💳 ОПЛАТИТЬ (Freekassa)", url=payment_url))
    builder.add(InlineKeyboardButton(text="🔄 Альтернативные способы оплаты (СБП/Карта)", callback_data=f"alt_pay_{server_id}_{period}"))
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"buy_srv_{server_id}"))
    builder.adjust(1)
    
    text = (
        f"💳 <b>ОПЛАТА ТАРИФА</b>\n\n"
        f"Период: <b>{period.replace('d', ' дней')}</b>\n"
        f"Сумма к оплате: <b>{amount} руб.</b>\n"
        f"Номер заказа: <b>#{order_id}</b>\n\n"
        f"Выберите удобный способ оплаты:"
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data.startswith("alt_pay_"))
async def alt_payment_method(callback: CallbackQuery, state: FSMContext):
    _, _, s_id, period = callback.data.split("_")
    details = db.get_setting("manual_payment_details") or "Реквизиты не заданы администратором."
    
    text = (
        f"🔄 <b>РУЧНАЯ ОПЛАТА (Прямой перевод)</b>\n\n"
        f"Это вариант оплаты через прямой перевод по реквизитам. Оплатите по данным реквизитам и пришлите чек. "
        f"Администратор проверит и выдаст вам доступ.\n\n"
        f"<b>Реквизиты:</b>\n<code>{details}</code>\n\n"
        f"После совершения перевода, нажмите кнопку ниже для отправки чека."
    )
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📎 Отправить чек", callback_data=f"send_receipt_{s_id}_{period}"))
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"buy_srv_{s_id}"))
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data.startswith("send_receipt_"))
async def wait_for_receipt(callback: CallbackQuery, state: FSMContext):
    _, _, s_id, period = callback.data.split("_")
    await state.update_data(rec_s_id=s_id, rec_period=period)
    await callback.message.answer("Пожалуйста, отправьте фотографию чека или скриншот перевода (одним файлом/картинкой):")
    await state.set_state(BuyStates.waiting_for_receipt)
    await callback.answer()

@user_router.message(BuyStates.waiting_for_receipt, F.photo | F.document)
async def receive_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    s_id = data.get("rec_s_id")
    period = data.get("rec_period")
    
    server = db.get_server_by_id(s_id)
    sp1, sp7, sp30 = server[4], server[5], server[6]
    
    if period == "1d": amount = int(sp1 if sp1 is not None else db.get_setting("price_1d"))
    elif period == "7d": amount = int(sp7 if sp7 is not None else db.get_setting("price_7d"))
    elif period == "30d": amount = int(sp30 if sp30 is not None else db.get_setting("price_30d"))
    else: amount = 0
    
    order_id = db.create_manual_order(message.from_user.id, s_id, period, amount)
    
    admin_text = (
        f"🧾 <b>Новая ручная оплата!</b>\n\n"
        f"Пользователь: <code>{message.from_user.id}</code>\n"
        f"Сервер ID: {s_id}\n"
        f"Период: {period}\n"
        f"Сумма: {amount} руб.\n\n"
        f"Проверьте чек и подтвердите выдачу."
    )
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_conf_man_{order_id}"))
    builder.add(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_decl_man_{order_id}"))
    
    if message.photo:
        await message.bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    elif message.document:
        await message.bot.send_document(ADMIN_ID, message.document.file_id, caption=admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        
    await message.answer("✅ Ваш чек успешно отправлен администратору на проверку. Ожидайте выдачи конфигурации (обычно занимает от нескольких минут до пары часов).")
    await state.clear()

@user_router.callback_query(F.data.startswith("pay_success_"))
async def grant_trial_access(callback: CallbackQuery):
    _, _, s_id_str, period = callback.data.split("_")
    server_id = int(s_id_str)
    tg_id = callback.from_user.id
    
    await callback.message.edit_text("⏳ Магия запущена! Подключаемся к серверу, генерируем конфиг...")
    success, msg = await issue_vpn_access(callback.bot, tg_id, server_id, period)
    
    if not success:
        await callback.message.answer(f"❌ Ошибка на сервере.\n\nДебаг-инфо:\n<code>{html.quote(msg)}</code>", parse_mode="HTML")

@user_router.callback_query(F.data == "usr_help")
async def show_help(callback: CallbackQuery):
    text = (
        "📚 <b>ИНСТРУКЦИЯ ПО НАСТРОЙКЕ:</b>\n\n"
        "1️⃣ <b>Скачайте приложение AmneziaWG</b> на ваше устройство по ссылкам ниже:\n"
        "📱 <a href='https://play.google.com/store/apps/details?id=org.amnezia.awg'>Скачать для Android (Google Play)</a>\n"
        "🍏 <a href='https://apps.apple.com/us/app/amneziawg/id6478942365'>Скачать для iOS (App Store)</a>\n"
        "💻 <a href='https://github.com/amnezia-vpn/amneziawg-windows-client/releases/tag/2.0.1'>Скачать для Windows (GitHub)</a>\n\n"
        "2️⃣ <b>Сохраните файл конфигурации</b> <code>.conf</code>, который бот прислал вам после оплаты или оформления пробного периода.\n\n"
        "3️⃣ Откройте приложение <b>AmneziaWG</b>, нажмите кнопку <b>«Добавить туннель»</b> (или знак ➕) и выберите скачанный файл.\n\n"
        "4️⃣ Включите переключатель. <b>Готово!</b> Подключение настроено в приложении. 🌍"
    )
    builder = InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ В меню", callback_data="usr_menu"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML", disable_web_page_preview=True)

@user_router.callback_query(F.data == "usr_support")
async def show_support(callback: CallbackQuery, state: FSMContext):
    text = "🤝 СЛУЖБА ПОДДЕРЖКИ:\n\nОпишите вашу проблему одним сообщением ниже, и я передам её администратору."
    builder = InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ Отмена", callback_data="usr_menu"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await state.set_state(SupportStates.waiting_for_question)

@user_router.message(SupportStates.waiting_for_question)
async def process_support_message(message: Message, state: FSMContext):
    admin_text = (
        f"🚨 <b>Новое обращение в поддержку!</b>\n\n"
        f"ID: <code>{message.from_user.id}</code>\n"
        f"Имя: {message.from_user.full_name}\n\n"
        f"<b>Текст:</b>\n{message.text}"
    )
    try:
        builder = InlineKeyboardBuilder()
        builder.add(InlineKeyboardButton(text="✍️ Ответить", callback_data=f"reply_to_{message.from_user.id}"))
        await message.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await message.answer("✅ Сообщение отправлено! Администратор ответит вам.", reply_markup=main_reply_kb)
    except Exception:
        await message.answer("❌ Ошибка отправки.")
    await state.clear()

async def issue_vpn_access(bot, tg_id, server_id, period):
    username = f"user_{tg_id}"
    server = db.get_server_by_id(server_id)
    if not server:
        return False, "Сервер не найден"
        
    ip, port, srv_name = server[0], server[1], server[2]
    
    cmd = f"bash /root/add_user.sh {username}"
    config_text = await ssh.run_ssh_command(ip, port, cmd)
    
    if not config_text or "Ошибка" in config_text:
        error_msg = config_text if config_text else "Пустой ответ (Таймаут или ошибка подключения)"
        return False, error_msg

    if period == "trial":
        t_hours = int(db.get_setting("trial_hours"))
        db.add_or_update_user(tg_id, server_id, username, hours=t_hours, is_trial=True)
    elif period == "1d": db.add_or_update_user(tg_id, server_id, username, days=1, is_trial=False)
    elif period == "7d": db.add_or_update_user(tg_id, server_id, username, days=7, is_trial=False)
    elif period == "30d": db.add_or_update_user(tg_id, server_id, username, days=30, is_trial=False)

    config_file = BufferedInputFile(config_text.encode('utf-8'), filename=f"ID{server_id}AWG.conf")
    caption = (
        "✅ <b>Доступ успешно предоставлен!</b>\n\n"
        "Скачайте прикрепленный файл и импортируйте его в официальное приложение <b>AmneziaWG</b> на вашем устройстве.\n\n"
        "<i>Приятного и безопасного серфинга!</i>"
    )
    try:
        await bot.send_document(tg_id, config_file, caption=caption, parse_mode="HTML")
    except Exception:
        pass
        
    return True, "Успех"