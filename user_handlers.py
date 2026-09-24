import hashlib
from typing import Callable, Dict, Any, Awaitable
from datetime import datetime

from aiogram import Router, F, BaseMiddleware
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram import html

import database as db
import ssh_manager as ssh
import payments
from config import ADMIN_ID

user_router = Router()
ADMIN_ID = 1617274846

# Партнерская ссылка на Amnezia Premium (официальный тариф от разработчиков AmneziaWG)
AMNEZIA_PREMIUM_URL = "https://amnezia.org/premium?arf=6VBU1RPKQZ2GYY9J"

# --- Middleware для принудительного принятия правил ---
class MandatoryTosMiddleware(BaseMiddleware):
    async def __call__(
        self, 
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]], 
        event: TelegramObject, 
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        # 1. Пропускаем команду /start
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        # 2. Пропускаем коллбеки кнопок, которые относятся к принятию правил
        if isinstance(event, CallbackQuery) and event.data in ["usr_tos", "usr_tos_accept"]:
            return await handler(event, data)

        # 3. Проверяем статус соглашения в базе
        profile = db.get_user_profile(user.id)
        accepted = False
        if profile and len(profile) > 3 and profile[3] == 1:
            accepted = True

        # 4. Если не принял — блокируем действие и присылаем правила
        if not accepted:
            text = "⚠️ <b>Доступ ограничен</b>\n\nДля использования бота вы должны ознакомиться и принять Пользовательское соглашение."
            builder = InlineKeyboardBuilder().add(InlineKeyboardButton(text="📜 Прочитать и принять", callback_data="usr_tos"))
            
            if isinstance(event, Message):
                await event.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                await event.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
                await event.answer() 
            
            return 

        return await handler(event, data)

# Регистрируем мидлварь
user_router.message.middleware(MandatoryTosMiddleware())
user_router.callback_query.middleware(MandatoryTosMiddleware())


# --- Вспомогательные функции и стейты ---
def parse_date(date_str):
    try:
        if "." in date_str:
            return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S.%f")
        return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now()

def get_price_for_period(server, period):
    """server - кортеж из db.get_server_by_id (ip, port, name, limit, price_1d, price_7d, price_30d)"""
    sp1, sp7, sp30 = server[4], server[5], server[6]
    if period == "1d":
        return int(sp1 if sp1 is not None else db.get_setting("price_1d"))
    elif period == "7d":
        return int(sp7 if sp7 is not None else db.get_setting("price_7d"))
    elif period == "30d":
        return int(sp30 if sp30 is not None else db.get_setting("price_30d"))
    return 0

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
    builder.add(InlineKeyboardButton(text="📜 Соглашение и Политика", callback_data="usr_tos"))
    builder.adjust(1, 1, 1, 2, 1)
    return builder.as_markup()

async def check_and_clean_expired(tg_id: int):
    subs = db.get_user_subs(tg_id)
    if not subs: return "not_found"

    has_expired = False
    for sub in subs:
        t_id, s_id, uname, exp_date_str, has_trial, active, is_trial, notif, last_exp = sub
        if exp_date_str and active == 1:
            try:
                expire_date = parse_date(exp_date_str)
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


# --- Хендлеры ---

@user_router.message(Command("start"))
async def start_cmd(message: Message, state: FSMContext):
    await state.clear() 
    
    try:
        db.update_user_profile(
            message.from_user.id, 
            message.from_user.full_name, 
            message.from_user.username
        )
    except Exception as e:
        print(f"Ошибка сохранения профиля: {e}")

    profile = db.get_user_profile(message.from_user.id)
    if not profile or len(profile) <= 3 or profile[3] == 0:
        text = (
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Перед началом использования нашего VPN-сервиса, пожалуйста, ознакомьтесь с Пользовательским соглашением и Политикой конфиденциальности."
        )
        builder = InlineKeyboardBuilder().add(InlineKeyboardButton(text="📜 Открыть соглашение", callback_data="usr_tos"))
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        return

    await message.answer("👋 Добро пожаловать! Я бот для заказа ультра-быстрого VPN с защитой от блокировок AmneziaWG.\n\nВыберите интересующий раздел меню:", reply_markup=main_reply_kb)
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
            expire_date = parse_date(exp_date_str)
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
        "ℹ️ <b>ОПИСАНИЕ УСЛУГ И УСЛОВИЯ ПОЛЬЗОВАНИЯ</b>\n\n"
        "🌟 <b>Эксклюзивное качество и скорость:</b>\n"
        "Мы следим за тем, чтобы на каждом нашем сервере располагалось <b>максимум 20 человек</b>. "
        "Это гарантирует отсутствие перегрузок, стабильно высокую скорость и минимальный пинг для каждого пользователя. "
        "Ваш интернет всегда будет «летать»!\n\n"
        "🛒 <b>Как это работает?</b>\n"
        "Вы выбираете локацию, оплачиваете тариф (или берете бесплатный тест), и бот моментально выдает вам личный конфигурационный файл. "
        "Достаточно добавить его в приложение, нажать одну кнопку — и вы в безопасном интернете без ограничений.\n\n"
        "📱 <b>Поддерживаемые платформы:</b>\n"
        "Мы используем современный протокол AmneziaWG (надежная защита от блокировок), который легко настраивается на:\n"
        "• <b>Android</b> (смартфоны и планшеты)\n"
        "• <b>iOS</b> (iPhone / iPad)\n"
        "• <b>Windows</b> (ПК и ноутбуки)\n\n"
        "👑 <b>Выделенный личный сервер:</b>\n"
        "Если вы хотите получить максимальную приватность и использовать 100% мощности сервера только для себя — "
        "у нас есть услуга <b>Личного выделенного сервера</b>. Для заказа обратитесь в Поддержку с просьбой о выделенном сервере, "
        "и мы настроим его специально для вас."
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

    builder.add(InlineKeyboardButton(text="🌟 Amnezia Premium", url=AMNEZIA_PREMIUM_URL))
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
        last_exp = parse_date(u[8])
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
    
    builder.add(InlineKeyboardButton(text=f"⏳ 1 День — {p1} руб.", callback_data=f"pay_req_{s_id}_1d"))
    builder.add(InlineKeyboardButton(text=f"⏳ 7 Дней — {p7} руб.", callback_data=f"pay_req_{s_id}_7d"))
    builder.add(InlineKeyboardButton(text=f"⏳ 30 Дней — {p30} руб.", callback_data=f"pay_req_{s_id}_30d"))
    builder.add(InlineKeyboardButton(text="⬅️ Выбрать другой сервер", callback_data="usr_buy_choose_srv"))
    builder.adjust(1)
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data.startswith("pay_req_"))
async def ask_for_details_prompt(callback: CallbackQuery):
    _, _, s_id, period = callback.data.split("_")
    auto_enabled = payments.is_auto_pay_enabled()
    if auto_enabled:
        text = (
            "💳 <b>ОПЛАТА ТАРИФА</b>\n\n"
            "⚡️ <b>Оплата картой (авто)</b> — доступ выдается автоматически сразу после оплаты.\n"
            "📥 <b>Запрос реквизитов</b> — ручной перевод с подтверждением администратора.\n\n"
            "<i>Выберите удобный способ оплаты.</i>"
        )
    else:
        text = (
            "💳 <b>ОПЛАТА ТАРИФА</b>\n\n"
            "📥 <b>Запрос реквизитов</b> — ручной перевод с подтверждением администратора.\n\n"
            "<i>⚡️ Автоматическая оплата картой временно недоступна.</i>"
        )
    builder = InlineKeyboardBuilder()
    if auto_enabled:
        builder.add(InlineKeyboardButton(text="⚡️ Оплатить картой (авто)", callback_data=f"autopay_{s_id}_{period}"))
    builder.add(InlineKeyboardButton(text="📥 Запрос реквизитов", callback_data=f"ask_det_{s_id}_{period}"))
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"buy_srv_{s_id}"))
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data.startswith("autopay_"))
async def autopay_create(callback: CallbackQuery):
    _, s_id_str, period = callback.data.split("_")
    s_id = int(s_id_str)
    tg_id = callback.from_user.id

    if not payments.is_auto_pay_enabled():
        return await callback.answer(
            "Автоматическая оплата временно отключена. Воспользуйтесь запросом реквизитов.",
            show_alert=True
        )

    server = db.get_server_by_id(s_id)
    if not server:
        return await callback.answer("Сервер не найден.", show_alert=True)

    amount = get_price_for_period(server, period)
    if amount <= 0:
        return await callback.answer("Не удалось определить цену тарифа.", show_alert=True)

    await callback.answer("⏳ Создаю платеж...")

    try:
        payment_url, uid = await payments.create_order(
            amount, "RUB", tg_id=tg_id, username=callback.from_user.username
        )
    except Exception as e:
        await callback.message.answer(
            f"❌ Не удалось создать автоматический платеж. Попробуйте позже или воспользуйтесь ручной оплатой.\n\n"
            f"<code>{html.quote(str(e))}</code>",
            parse_mode="HTML"
        )
        return

    db.create_aipay_order(tg_id, s_id, period, amount, uid)

    text = (
        f"💳 <b>Автоматическая оплата</b>\n\n"
        f"Сумма к оплате: <b>{amount} руб.</b>\n\n"
        f"1️⃣ Нажмите «Перейти к оплате» и завершите платеж.\n"
        f"2️⃣ Доступ будет выдан автоматически в течение минуты после оплаты.\n"
        f"<i>Если этого не произошло — нажмите «Проверить статус».</i>"
    )
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url))
    builder.add(InlineKeyboardButton(text="🔄 Проверить статус оплаты", callback_data=f"check_aipay_{uid}"))
    builder.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"buy_srv_{s_id}"))
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data.startswith("check_aipay_"))
async def autopay_check_status(callback: CallbackQuery):
    uid = callback.data.replace("check_aipay_", "")
    order = db.get_order_by_uid(uid)
    if not order:
        return await callback.answer("Заказ не найден.", show_alert=True)

    order_id, tg_id, s_id, period, amount, status = order

    if status == "paid":
        return await callback.answer("✅ Оплата уже подтверждена, доступ выдан.", show_alert=True)
    if status in ("failed", "cancelled"):
        return await callback.answer("❌ Этот платеж не был завершен (отменен или произошла ошибка).", show_alert=True)

    try:
        remote_status = await payments.get_order_status(uid)
    except Exception:
        return await callback.answer("Не удалось проверить статус, попробуйте чуть позже.", show_alert=True)

    status_id = remote_status.get("id")

    if status_id == payments.STATUS_SUCCESS:
        if db.complete_order_by_uid(uid):
            await callback.answer("✅ Оплата подтверждена! Выдаю доступ...", show_alert=True)
            success, msg = await issue_vpn_access(callback.bot, tg_id, s_id, period, notify_admin=True)
            if not success:
                await callback.message.answer(
                    f"⚠️ Оплата прошла успешно, но при выдаче доступа произошла ошибка. Обратитесь в поддержку.\n"
                    f"<code>{html.quote(msg)}</code>",
                    parse_mode="HTML"
                )
        else:
            await callback.answer("✅ Оплата уже была подтверждена ранее.", show_alert=True)
    elif status_id in (payments.STATUS_ERROR, payments.STATUS_CANCELLED):
        db.fail_order_by_uid(uid, "failed" if status_id == payments.STATUS_ERROR else "cancelled")
        await callback.answer("❌ Платеж не завершен (ошибка или отмена).", show_alert=True)
    else:
        await callback.answer("⏳ Оплата еще не подтверждена. Попробуйте через минуту.", show_alert=True)

@user_router.callback_query(F.data.startswith("ask_det_"))
async def send_details_request_to_admin(callback: CallbackQuery):
    _, _, s_id, period = callback.data.split("_")
    tg_id = callback.from_user.id
    
    full_name = html.quote(callback.from_user.full_name)
    username_str = f" (@{callback.from_user.username})" if callback.from_user.username else ""
    
    admin_text = (
        f"👤 <b>Новый запрос реквизитов на оплату!</b>\n\n"
        f"Клиент: <b>{full_name}</b>{username_str}\n"
        f"ID: <code>{tg_id}</code>\n"
        f"Сервер ID: <b>{s_id}</b>\n"
        f"Период: <b>{period}</b>\n\n"
        f"Выдать пользователю реквизиты?"
    )
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Отправить", callback_data=f"adm_app_det_{tg_id}_{s_id}_{period}"))
    builder.add(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_rej_det_{tg_id}"))
    builder.adjust(2)
    
    try:
        await callback.bot.send_message(ADMIN_ID, admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.message.edit_text("✅ <b>Ваш запрос успешно отправлен администратору.</b>\nОжидайте ответа (обычно это занимает немного времени).", parse_mode="HTML")
    except Exception:
        await callback.answer("Ошибка при отправке запроса администратору. Попробуйте позже.", show_alert=True)

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
    amount = get_price_for_period(server, period)
    
    order_id = db.create_manual_order(message.from_user.id, s_id, period, amount)
    
    full_name = html.quote(message.from_user.full_name)
    username_str = f" (@{message.from_user.username})" if message.from_user.username else ""
    
    admin_text = (
        f"🧾 <b>Новая ручная оплата!</b>\n\n"
        f"Клиент: <b>{full_name}</b>{username_str}\n"
        f"ID: <code>{message.from_user.id}</code>\n"
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
    
    await callback.message.edit_text("⏳ Магия запущена! Обрабатываем запрос...")
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
        "4️⃣ Включите переключатель. <b>Готово!</b> Теперь вы в безопасном и свободном интернете! 🌍"
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

@user_router.callback_query(F.data == "usr_tos")
async def show_tos(callback: CallbackQuery):
    text = (
        "📜 <b>ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ И ПОЛИТИКА КОНФИДЕНЦИАЛЬНОСТИ</b>\n\n"
        "<b>1. Общие положения</b>\n"
        "Используя данного бота, вы принимаете условия предоставления услуг VPN. "
        "Сервис предоставляется «как есть» без гарантий абсолютной бесперебойности.\n\n"
        "<b>2. Политика конфиденциальности</b>\n"
        "• Сервис собирает <b>только базовую информацию</b> из вашего профиля (Telegram ID, имя и username) для привязки и управления подпиской.\n"
        "• Мы <b>НЕ ВЕДЕМ</b> логи вашего трафика, не отслеживаем посещаемые ресурсы и не перехватываем скачиваемые файлы.\n"
        "• Ваш IP-адрес используется протоколом WireGuard исключительно временно для поддержания активного соединения с сервером.\n\n"
        "<b>3. Правила использования</b>\n"
        "• Строго запрещается использование сервиса для любой незаконной деятельности (спам, кардинг, DDoS-атаки, мошенничество и т.д.).\n"
        "• При поступлении официальных жалоб (Abuse) на вашу активность со стороны дата-центра, мы оставляем за собой право заблокировать вашу учетную запись без возврата средств.\n\n"
        "<i>Нажимая кнопку ниже, вы подтверждаете свое согласие с данными правилами и условиями.</i>"
    )
    
    builder = InlineKeyboardBuilder()
    
    profile = db.get_user_profile(callback.from_user.id)
    accepted = False
    if profile and len(profile) > 3 and profile[3] == 1:
        accepted = True
        
    if not accepted:
        builder.add(InlineKeyboardButton(text="✅ Принять соглашение", callback_data="usr_tos_accept"))
    else:
        builder.add(InlineKeyboardButton(text="⬅️ В меню", callback_data="usr_menu"))
        
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@user_router.callback_query(F.data == "usr_tos_accept")
async def accept_tos_cb(callback: CallbackQuery):
    # Принудительно создаем запись юзера в БД, если он не нажимал /start
    db.update_user_profile(
        callback.from_user.id, 
        callback.from_user.full_name, 
        callback.from_user.username
    )
    
    # Теперь запись точно есть, и обновление пройдет успешно
    db.accept_tos(callback.from_user.id)
    await callback.answer("✅ Вы успешно приняли Пользовательское соглашение!", show_alert=True)
    
    await callback.message.edit_text(
        "👋 Добро пожаловать! Я бот для заказа ультра-быстрого VPN с защитой от блокировок AmneziaWG.\n\nВыберите интересующий раздел меню:", 
        reply_markup=get_main_keyboard()
    )


async def reissue_config_for_active_sub(bot, tg_id, server_id):
    """
    Разовая принудительная перевыдача конфига (новые ключи) для УЖЕ АКТИВНОЙ подписки,
    оформленной до появления сохранения config_text в базе. Срок действия и статус подписки
    не трогает - только сам файл конфигурации. Используется кнопкой "Обновить конфиг" на сайте
    для пользователей, у которых сайт не может отдать конфиг на скачивание (get_user_config пуст).

    ВНИМАНИЕ: если у пользователя все еще установлен и используется старый файл, он перестанет
    работать сразу после вызова этой функции (add_user.sh сначала удаляет старого пира).
    """
    username = f"user_{tg_id}"
    server = db.get_server_by_id(server_id)
    if not server:
        return False, "Сервер не найден"

    sub = db.get_user_sub(tg_id, server_id)
    if not sub or sub[5] != 1:
        return False, "Подписка не найдена или неактивна"

    ip, port, srv_name = server[0], server[1], server[2]

    cmd = f"bash /root/add_user.sh {username}"
    config_text = await ssh.run_ssh_command(ip, port, cmd)
    if not config_text or "Ошибка" in config_text:
        return False, config_text or "Пустой ответ (Таймаут или ошибка подключения)"

    db.set_user_config(tg_id, server_id, config_text)

    config_file = BufferedInputFile(config_text.encode('utf-8'), filename=f"ID{server_id}AWG.conf")
    caption = (
        "🔄 <b>Конфигурация обновлена!</b>\n\n"
        "Вы запросили перевыпуск конфига через сайт. Старый файл (если он у вас еще был) больше "
        "не будет работать - используйте прикрепленный новый.\n\n"
        "<i>Импортируйте его в приложение AmneziaWG вместо старого.</i>"
    )
    try:
        await bot.send_document(tg_id, config_file, caption=caption, parse_mode="HTML")
    except Exception:
        pass

    return True, config_text

async def issue_vpn_access(bot, tg_id, server_id, period, notify_admin=False):
    username = f"user_{tg_id}"
    server = db.get_server_by_id(server_id)
    if not server: return False, "Сервер не найден"
        
    ip, port, srv_name = server[0], server[1], server[2]
    
    # === ПРОВЕРКА НА ПРОДЛЕНИЕ АКТИВНОГО КОНФИГА ===
    user = db.get_user_sub(tg_id, server_id)
    is_active = False
    if user:
        exp_date_str = user[3]
        active_flag = user[5]
        if active_flag == 1 and exp_date_str:
            try:
                expire_date = parse_date(exp_date_str)
                if expire_date > datetime.now():
                    is_active = True
            except Exception: pass

    if is_active:
        if period == "trial":
            t_hours = int(db.get_setting("trial_hours"))
            db.add_or_update_user(tg_id, server_id, username, hours=t_hours, is_trial=True)
        elif period == "1d": db.add_or_update_user(tg_id, server_id, username, days=1, is_trial=False)
        elif period == "7d": db.add_or_update_user(tg_id, server_id, username, days=7, is_trial=False)
        elif period == "30d": db.add_or_update_user(tg_id, server_id, username, days=30, is_trial=False)

        try:
            await bot.send_message(
                tg_id, 
                f"✅ <b>Услуга продлена!</b>\n\nВаша подписка на сервер <b>{srv_name}</b> успешно продлена. Ваш текущий конфиг остается прежним и продолжает работать. Скачивать новый не нужно!", 
                parse_mode="HTML"
            )
        except Exception: pass

        await _notify_admin_grant(bot, tg_id, srv_name, period, renewed=True, notify_admin=notify_admin)
        return True, "Продлено"

    cmd = f"bash /root/add_user.sh {username}"
    config_text = await ssh.run_ssh_command(ip, port, cmd)
    
    if not config_text or "Ошибка" in config_text:
        error_msg = config_text if config_text else "Пустой ответ (Таймаут или ошибка подключения)"
        return False, error_msg

    if period == "trial":
        t_hours = int(db.get_setting("trial_hours"))
        db.add_or_update_user(tg_id, server_id, username, hours=t_hours, is_trial=True, config_text=config_text)
    elif period == "1d": db.add_or_update_user(tg_id, server_id, username, days=1, is_trial=False, config_text=config_text)
    elif period == "7d": db.add_or_update_user(tg_id, server_id, username, days=7, is_trial=False, config_text=config_text)
    elif period == "30d": db.add_or_update_user(tg_id, server_id, username, days=30, is_trial=False, config_text=config_text)

    config_file = BufferedInputFile(config_text.encode('utf-8'), filename=f"ID{server_id}AWG.conf")
    caption = (
        "✅ <b>Доступ успешно предоставлен!</b>\n\n"
        "Скачайте прикрепленный файл и импортируйте его в официальное приложение <b>AmneziaWG</b> на вашем устройстве.\n\n"
        "<i>Приятного и безопасного серфинга!</i>"
    )
    try:
        await bot.send_document(tg_id, config_file, caption=caption, parse_mode="HTML")
    except Exception: pass

    await _notify_admin_grant(bot, tg_id, srv_name, period, renewed=False, notify_admin=notify_admin)
    return True, config_text

async def _notify_admin_grant(bot, tg_id, srv_name, period, renewed, notify_admin):
    """
    Уведомляет администратора о выдаче доступа - только в случаях, когда у админа иначе
    НЕТ видимости происходящего:
      - пробный период (self-service, никакого одобрения не требуется) - уведомляем ВСЕГДА;
      - автооплата картой (провайдер из payments.py) - уведомляем, если вызывающий код передал notify_admin=True
        (вебхук / поллинг статуса оплаты).
    Ручную оплату здесь не дублируем - админ и так видит это сразу же после нажатия
    "Подтвердить" в confirm_manual_order().
    """
    if period == "trial":
        text = (
            f"🎁 <b>Взят бесплатный пробный период!</b>\n\n"
            f"Пользователь: <code>{tg_id}</code>\n"
            f"Сервер: <b>{srv_name}</b>"
        )
    elif notify_admin:
        period_label = {"1d": "1 день", "7d": "7 дней", "30d": "30 дней"}.get(period, period)
        action_label = "Продление подписки" if renewed else "Новая оплата"
        text = (
            f"💳 <b>{action_label} картой (авто)!</b>\n\n"
            f"Пользователь: <code>{tg_id}</code>\n"
            f"Сервер: <b>{srv_name}</b>\n"
            f"Период: {period_label}"
        )
    else:
        return

    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        pass