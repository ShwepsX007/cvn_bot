import os
import uuid
import random
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote
from fastapi import FastAPI, Request, HTTPException, Form, UploadFile, File
from fastapi.responses import Response, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import asyncio
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.types import BufferedInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ssh_manager as ssh
import database as bot_db
import aipay
import platega
import payments
import qrgen
import emailauth
import mailer
import webauth

from bot import bot, dp, BOT_TOKEN, check_expiring_soon, check_expiring_1d, check_expiring_3d, check_expired_users, clean_inactive_users
from user_handlers import issue_vpn_access, get_price_for_period, check_and_clean_expired, parse_date, reissue_config_for_active_sub
from admin_panel import ADMIN_ID

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
if not os.path.exists(TEMPLATES_DIR):
    os.makedirs(TEMPLATES_DIR)

STATIC_DIR = os.path.join(BASE_DIR, "static")
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Веб-админка (страницы /admin/web/*) - доступ только для админов
try:
    import web_admin
    app.include_router(web_admin.router)
except Exception as e:
    print(f"⚠️ Веб-админка не подключена: {e}")

# Секрет для подписи сессионных cookie (вход через Telegram). Генерируется один раз
# и сохраняется в файл рядом со скриптом, чтобы не разлогинивать всех при каждом рестарте.
SESSION_SECRET_FILE = os.path.join(BASE_DIR, ".session_secret")
if os.path.exists(SESSION_SECRET_FILE):
    with open(SESSION_SECRET_FILE, "r") as f:
        SESSION_SECRET = f.read().strip()
else:
    SESSION_SECRET = secrets.token_hex(32)
    with open(SESSION_SECRET_FILE, "w") as f:
        f.write(SESSION_SECRET)

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="vpn_session", max_age=30 * 24 * 3600)

templates = Jinja2Templates(directory=TEMPLATES_DIR)
CAPTCHA_STORE = {}
DOWNLOAD_STORE = {}

scheduler = AsyncIOScheduler()
_polling_task = None
BOT_USERNAME = None

async def create_amnezia_peer(ip: str, port: int) -> tuple[str, str]:
    peer_id = f"web_{uuid.uuid4().hex[:8]}"
    cmd = f"bash /root/add_user.sh {peer_id}"
    config_text = await ssh.run_ssh_command(ip, port, cmd)
    if not config_text or "Ошибка" in config_text:
        raise Exception(f"SSH Error: {config_text}")
    return config_text, peer_id

async def delete_amnezia_peer(ip: str, port: int, peer_id: str):
    cmd = f"bash /root/remove_user.sh {peer_id}"
    result = await ssh.run_ssh_command(ip, port, cmd)
    if "Ошибка" in result:
        raise Exception(f"Ошибка SSH при удалении: {result}")
    print(f"[CLEANUP] Успешно удален {peer_id} на сервере {ip}")

async def check_expired_web_trials():
    try:
        expired_trials = bot_db.get_expired_trials()
        if expired_trials:
            for trial_id, server_id, peer_id in expired_trials:
                try:
                    srv = bot_db.get_server_by_id(server_id)
                    if srv:
                        await delete_amnezia_peer(srv[0], srv[1], peer_id)
                    bot_db.delete_free_trial(trial_id)
                except Exception as e:
                    print(f"Ошибка при удалении {trial_id}: {e}")
    except Exception as e:
        print(f"Ошибка планировщика очистки: {e}")

@app.on_event("startup")
async def startup_event():
    global _polling_task, BOT_USERNAME
    bot_db.init_db()

    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
    except Exception as e:
        err = str(e)
        if "Unauthorized" in err:
            print("⛔⛔⛔ BOT_TOKEN ОТКЛОНЕН TELEGRAM (Unauthorized). Бот не будет отвечать, вход на сайте не заработает.")
            print("⛔ Скорее всего токен утек в публичный репозиторий и был отозван. Получите НОВЫЙ у @BotFather (/revoke),")
            print("⛔ вставьте его в config_tokens.py на сервере и перезапустите: systemctl restart vpn_bot")
        else:
            print(f"⚠️ Не удалось получить username бота (нужен для входа через Telegram): {e}")

    scheduler.add_job(check_expiring_soon, 'interval', minutes=15)
    scheduler.add_job(check_expiring_1d, 'interval', minutes=30)
    scheduler.add_job(check_expiring_3d, 'interval', minutes=60)
    scheduler.add_job(check_expired_users, 'interval', minutes=15)
    scheduler.add_job(check_expired_web_trials, 'interval', minutes=1)
    
    scheduler.add_job(clean_inactive_users, 'interval', hours=24)
    
    scheduler.start()
    
    _polling_task = asyncio.create_task(dp.start_polling(bot))
    print("🚀 WebApp, Telegram-бот и Планировщик успешно запущены вместе!")

@app.on_event("shutdown")
async def shutdown_event():
    """
    Без этого обработчика при выключении (systemctl restart/stop) процесс не завершается сам:
    aiogram-поллинг и планировщик продолжают работать в фоне, uvicorn зависает в ожидании,
    и systemd по таймауту убивает процесс через SIGKILL. Явно останавливаем всё, чтобы выход был чистым и быстрым.
    """
    print("🛑 Останавливаю планировщик и Telegram-бота...")
    try:
        scheduler.shutdown(wait=False)
    except Exception as e:
        print(f"Ошибка остановки планировщика: {e}")

    if _polling_task:
        _polling_task.cancel()
        try:
            await _polling_task
        except (asyncio.CancelledError, Exception):
            pass

    try:
        await bot.session.close()
    except Exception as e:
        print(f"Ошибка закрытия сессии бота: {e}")

    print("✅ Остановка завершена.")

@app.get("/")
async def read_root(request: Request):
    active_servers = bot_db.get_active_servers() or []
    servers = []
    
    for s_id, ip, port, name, limit in active_servers:
        paid_count = bot_db.count_paid_users_on_server(s_id) or 0
        free_slots = limit - paid_count
        servers.append({
            "id": s_id, "name": name, "free_slots": free_slots if free_slots > 0 else 0
        })

    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"request": request, "servers": servers}
    )

def _current_tg_id(request: Request):
    return request.session.get("tg_id")

def _current_email(request: Request):
    return request.session.get("email")

def is_web_admin(tg_id) -> bool:
    """Признак прав на веб-админку: главный админ из config или назначенный в базе."""
    try:
        tg = int(tg_id)
    except (TypeError, ValueError):
        return False
    return tg == int(ADMIN_ID) or bot_db.is_admin_user(tg)

def _client_host(request: Request):
    return request.headers.get("X-Real-IP") or (request.client.host if request.client else None)

_PW_MESSAGES = {
    "short": "Пароль должен быть не короче 8 символов.",
    "long": "Пароль слишком длинный (максимум 200 символов).",
    "weak": "Слишком простой пароль - придумайте посложнее.",
}

def _pw_message(code: str) -> str:
    return _PW_MESSAGES.get(code, "Пароль не подходит.")

def _check_captcha(captcha_id: str, captcha_answer: str) -> bool:
    """Разовая математическая капча (тот же механизм, что у пробного доступа)."""
    saved = CAPTCHA_STORE.pop(captcha_id, None)
    if not saved or saved["expires"] < datetime.utcnow():
        return False
    try:
        return int(captcha_answer) == saved["answer"]
    except (ValueError, TypeError):
        return False

@app.get("/login")
async def login_page(request: Request):
    if _current_tg_id(request):
        return RedirectResponse(url="/dashboard")
    site_url = bot_db.get_setting("site_url") or "https://amneziawg.fun"
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "bot_username": BOT_USERNAME,
            "site_url": site_url,
            "error": request.query_params.get("error"),
            "msg": request.query_params.get("msg")
        }
    )

@app.get("/auth/telegram")
async def auth_telegram(request: Request):
    """Redirect-цель виджета 'Log in with Telegram' (data-auth-url)."""
    params = dict(request.query_params)

    if not webauth.verify_telegram_auth(params, BOT_TOKEN):
        return RedirectResponse(url="/login?error=1")

    tg_id = int(params["id"])
    first_name = params.get("first_name", "")
    last_name = params.get("last_name", "")
    username = params.get("username")
    full_name = (f"{first_name} {last_name}".strip()) or (f"@{username}" if username else str(tg_id))

    # Тот же вызов, что и в /start бота - пользователь автоматически появляется в базе бота
    try:
        bot_db.update_user_profile(tg_id, full_name, username)
    except Exception as e:
        print(f"Ошибка сохранения профиля через веб-вход: {e}")

    request.session["tg_id"] = tg_id
    request.session["display_name"] = full_name

    # Если до этого входили по почте - довязываем email к этому Telegram-аккаунту
    email = _current_email(request)
    if email:
        try:
            acc = bot_db.get_email_account(email)
            if acc and (not acc[3] or acc[3] == tg_id):
                bot_db.bind_email_to_tg(email, tg_id)
        except Exception as e:
            print(f"Ошибка привязки почты {email} к tg {tg_id}: {e}")

    return RedirectResponse(url="/dashboard")

@app.get("/auth/bot")
async def auth_bot(request: Request):
    """Вход в кабинет по одноразовой ссылке, которую выдает бот (/start web_login).
    Замена Telegram-виджету: работает даже там, где виджет заблокирован."""
    token = str(request.query_params.get("token") or "")
    tg_id = await asyncio.to_thread(bot_db.consume_tg_login_token, token) if token else None
    if not tg_id:
        return RedirectResponse(url="/login?error=" + quote(
            "Ссылка для входа недействительна или истекла - откройте бота и нажмите /start по кнопке со страницы входа еще раз."
        ), status_code=303)

    profile = bot_db.get_user_profile(tg_id)
    full_name = (profile[0] if profile and profile[0] else None) or (f"@{profile[1]}" if profile and profile[1] else str(tg_id))

    request.session["tg_id"] = tg_id
    request.session["display_name"] = full_name

    # Если до этого входили по почте - довязываем email к этому Telegram-аккаунту
    email = _current_email(request)
    if email:
        try:
            acc = bot_db.get_email_account(email)
            if acc and (not acc[3] or acc[3] == tg_id):
                bot_db.bind_email_to_tg(email, tg_id)
        except Exception as e:
            print(f"Ошибка привязки почты {email} к tg {tg_id}: {e}")

    return RedirectResponse(url="/dashboard", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


# ==================== РЕГИСТРАЦИЯ / ВХОД ПО ПОЧТЕ ====================
@app.post("/auth/email/register")
async def register_email(request: Request):
    form = await request.form()
    email = emailauth.normalize_email(str(form.get("email") or ""))
    password = str(form.get("password") or "")
    confirm = str(form.get("confirm") or "")
    cap_id = str(form.get("captcha_id") or "")
    cap_ans = str(form.get("captcha_answer") or "")

    host = _client_host(request) or "unknown"
    if not emailauth.REGISTER_LIMITER.allow(host):
        return RedirectResponse(url="/login?tab=email&error=" + quote("Слишком много попыток. Попробуйте позже."), status_code=303)
    if not email or not emailauth.valid_email(email):
        return RedirectResponse(url="/login?tab=email&error=" + quote("Некорректный адрес почты."), status_code=303)
    if not _check_captcha(cap_id, cap_ans):
        return RedirectResponse(url="/login?tab=email&error=" + quote("Неверный ответ на проверочный вопрос. Обновите страницу и попробуйте снова."), status_code=303)
    if password != confirm:
        return RedirectResponse(url="/login?tab=email&error=" + quote("Пароли не совпадают."), status_code=303)
    pw_err = emailauth.password_problem(password)
    if pw_err:
        return RedirectResponse(url="/login?tab=email&error=" + quote(_pw_message(pw_err)), status_code=303)

    acc = bot_db.get_email_account(email)
    if acc and acc[4]:
        return RedirectResponse(url="/login?tab=email&error=" + quote("Эта почта уже зарегистрирована. Просто войдите."), status_code=303)

    password_hash = emailauth.hash_password(password)
    if acc:
        # Перерегистрация неподтвержденного адреса - обновляем пароль и шлем письмо снова
        bot_db.set_email_password(email, password_hash)
    else:
        if bot_db.create_email_account(email, password_hash=password_hash) is None:
            return RedirectResponse(url="/login?tab=email&error=" + quote("Эта почта уже занята."), status_code=303)

    token = bot_db.create_email_token(email, "verify", None, 24 * 3600)
    ok, _err = await asyncio.to_thread(
        mailer.send_token_mail, "verify", email, token
    )
    print(f"MAIL register verify -> {email}: ok={ok} err={_err}")
    if not ok:
        return RedirectResponse(url="/login?tab=email&error=" + quote("Не удалось отправить письмо. Попробуйте позже или войдите через Telegram."), status_code=303)
    return RedirectResponse(url="/login?tab=email&msg=" + quote("Письмо с подтверждением отправлено на " + email + ". Перейдите по ссылке из письма."), status_code=303)


@app.post("/auth/email/login")
async def login_email(request: Request):
    form = await request.form()
    email = emailauth.normalize_email(str(form.get("email") or ""))
    password = str(form.get("password") or "")
    host = _client_host(request) or "unknown"
    if not emailauth.LOGIN_LIMITER.allow(host):
        return RedirectResponse(url="/login?tab=email&error=" + quote("Слишком много попыток входа. Попробуйте через 10 минут."), status_code=303)

    acc = bot_db.get_email_account(email) if email else None
    if acc and acc[4] and not acc[2]:
        # Почта подтверждена (привязана из бота), но пароль так и не задали
        return RedirectResponse(url="/login?tab=email&error=" + quote("Для этой почты еще не задан пароль - нажмите «Забыли пароль?» ниже."), status_code=303)
    if not acc or not acc[2] or not emailauth.verify_password(password, acc[2]):
        return RedirectResponse(url="/login?tab=email&error=" + quote("Неверная почта или пароль."), status_code=303)
    if not acc[4]:
        # Неподтвержденная почта - шлем письмо еще раз
        token = bot_db.create_email_token(email, "verify", acc[3], 24 * 3600)
        ok, err = await asyncio.to_thread(
            mailer.send_token_mail, "verify", email, token
        )
        print(f"MAIL login resend-verify -> {email}: ok={ok} err={err}")
        return RedirectResponse(url="/login?tab=email&error=" + quote("Почта не подтверждена. Мы отправили письмо повторно - перейдите по ссылке из него."), status_code=303)

    request.session["email"] = email
    if acc[3]:
        request.session["tg_id"] = acc[3]
    return RedirectResponse(url="/dashboard", status_code=303)


def _auth_page(request: Request, mode: str, status: str = "form", message: str = "", token: str = "", email: str = "", reset_token: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="auth_email.html",
        context={
            "request": request, "mode": mode, "status": status,
            "message": message, "token": token, "email": email,
            "reset_token": reset_token,
            "bot_username": BOT_USERNAME
        }
    )


@app.get("/auth/email/verify")
async def verify_email(request: Request):
    token = str(request.query_params.get("token") or "")
    row = await asyncio.to_thread(bot_db.consume_email_token, token, "verify") if token else None
    if not row:
        return _auth_page(request, "verify", "invalid", "Ссылка недействительна или истекла. Попробуйте войти - мы отправим письмо с подтверждением повторно.")
    email, _tg = row
    bot_db.mark_email_verified(email)
    request.session["email"] = email
    acc = bot_db.get_email_account(email)
    if acc and acc[3]:
        request.session["tg_id"] = acc[3]
    return _auth_page(request, "verify", "ok", "Почта подтверждена!", email=email)


@app.get("/auth/email/forgot")
async def forgot_get(request: Request):
    return _auth_page(request, "forgot", "form")


@app.post("/auth/email/forgot")
async def forgot_post(request: Request):
    form = await request.form()
    email = emailauth.normalize_email(str(form.get("email") or ""))
    host = _client_host(request) or "unknown"
    if emailauth.REGISTER_LIMITER.allow(host):
        acc = bot_db.get_email_account(email) if email else None
        if acc and acc[2]:
            token = bot_db.create_email_token(email, "reset", acc[3], 3600)
            ok, err = await asyncio.to_thread(
                mailer.send_token_mail, "reset", email, token
            )
            print(f"MAIL forgot reset -> {email}: ok={ok} err={err}")
    # Ответ всегда одинаковый - не раскрываем, существует ли такая почта
    return _auth_page(request, "forgot", "sent", "Если эта почта зарегистрирована, мы отправили на нее письмо со ссылкой для сброса пароля.")


@app.get("/auth/email/reset")
async def reset_get(request: Request):
    token = str(request.query_params.get("token") or "")
    row = await asyncio.to_thread(bot_db.peek_email_token, token, "reset") if token else None
    if not row:
        return _auth_page(request, "reset", "invalid", "Ссылка для сброса недействительна или истекла. Запросите новую.")
    return _auth_page(request, "reset", "form", token=token, email=row[0])


@app.post("/auth/email/reset")
async def reset_post(request: Request):
    form = await request.form()
    token = str(form.get("token") or "")
    password = str(form.get("password") or "")
    confirm = str(form.get("confirm") or "")
    if password != confirm:
        return _auth_page(request, "reset", "form", "Пароли не совпадают.", token=token)
    pw_err = emailauth.password_problem(password)
    if pw_err:
        return _auth_page(request, "reset", "form", _pw_message(pw_err), token=token)
    row = await asyncio.to_thread(bot_db.consume_email_token, token, "reset") if token else None
    if not row:
        return _auth_page(request, "reset", "invalid", "Ссылка для сброса недействительна или истекла. Запросите новую.")
    email, _tg = row
    bot_db.set_email_password(email, emailauth.hash_password(password))
    bot_db.mark_email_verified(email)
    request.session["email"] = email
    acc = bot_db.get_email_account(email)
    if acc and acc[3]:
        request.session["tg_id"] = acc[3]
    return _auth_page(request, "reset", "ok", "Пароль установлен!", email=email)


@app.post("/web/link_email")
async def web_link_email(request: Request):
    """Запрос на привязку почты из кабинета (пришлем письмо со ссылкой)."""
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = emailauth.normalize_email(str((body or {}).get("email") or ""))
    if not emailauth.valid_email(email):
        raise HTTPException(status_code=400, detail="Некорректный адрес почты.")

    host = _client_host(request) or "unknown"
    if not emailauth.MAIL_LINK_LIMITER.allow(host):
        raise HTTPException(status_code=429, detail="Слишком много писем подряд. Попробуйте через час.")

    acc = bot_db.get_email_account(email)
    if acc and acc[3] and acc[3] != tg_id:
        raise HTTPException(status_code=409, detail="Эта почта уже привязана к другому Telegram-аккаунту.")
    if acc and acc[4] and acc[3] == tg_id:
        return {"ok": True, "already": True}
    if not acc:
        if bot_db.create_email_account(email, tg_id=tg_id, verified=0) is None:
            raise HTTPException(status_code=409, detail="Эта почта уже занята другим аккаунтом.")

    token = emailauth.issue_token(email, "link", tg_id)
    ok, err = await asyncio.to_thread(mailer.send_token_mail, "link", email, token)
    print(f"MAIL cabinet link -> {email}: ok={ok} err={err}")
    if not ok:
        raise HTTPException(status_code=502, detail=f"Не удалось отправить письмо: {err}")
    return {"ok": True}


@app.get("/auth/email/link")
async def link_email_confirm(request: Request):
    """Подтверждение привязки почты из телеграм-бота по ссылке из письма."""
    token = str(request.query_params.get("token") or "")
    row = await asyncio.to_thread(bot_db.consume_email_token, token, "link") if token else None
    if not row:
        return _auth_page(request, "link", "invalid", "Ссылка привязки недействительна или истекла. Запросите новую в боте.")
    email, tg_id = row
    bot_db.bind_email_to_tg(email, tg_id)
    bot_db.mark_email_verified(email)
    # Если у аккаунта еще нет пароля - сразу предлагаем задать его по свежей ссылке
    acc = bot_db.get_email_account(email)
    reset_token = ""
    if acc and not acc[2]:
        reset_token = bot_db.create_email_token(email, "reset", tg_id, 3600)
    return _auth_page(request, "link", "ok", "Почта успешно привязана к вашему аккаунту!", email=email, reset_token=reset_token)

@app.get("/dashboard")
async def dashboard(request: Request):
    tg_id = _current_tg_id(request)

    # Сессия по почте: если почта уже привязана к Telegram - работаем как tg-аккаунт;
    # если нет - сначала просим привязать Telegram (виджетом), иначе уведомления и подписки некуда вязать.
    if not tg_id and _current_email(request):
        acc = bot_db.get_email_account(_current_email(request))
        if acc and acc[3]:
            request.session["tg_id"] = acc[3]
            tg_id = acc[3]
        else:
            return templates.TemplateResponse(
                request=request,
                name="link_telegram.html",
                context={
                    "request": request,
                    "bot_username": BOT_USERNAME,
                    "site_url": bot_db.get_setting("site_url") or "https://amneziawg.fun",
                    "email": _current_email(request),
                }
            )

    if not tg_id:
        return RedirectResponse(url="/login")

    profile = bot_db.get_user_profile(tg_id)
    accepted_tos = bool(profile and len(profile) > 3 and profile[3] == 1)

    active_subs = []
    servers_info = []

    if accepted_tos:
        await check_and_clean_expired(tg_id)

        subs_raw = bot_db.get_user_subs(tg_id) or []
        for sub in subs_raw:
            t_id, s_id, uname, exp_date_str, has_trial, active, is_trial, notif, last_exp = sub
            if active == 1:
                srv = bot_db.get_server_by_id(s_id)
                srv_name = srv[2] if srv else f"Сервер {s_id}"
                expire_date = parse_date(exp_date_str)
                active_subs.append({
                    "server_id": s_id,
                    "server_name": srv_name,
                    "expire": expire_date.strftime("%d.%m.%Y %H:%M"),
                    "is_trial": is_trial == 1,
                    "has_config": bool(bot_db.get_user_config(tg_id, s_id))
                })

        active_servers = bot_db.get_active_servers() or []
        for s_id, ip, port, name, limit in active_servers:
            paid_count = bot_db.count_paid_users_on_server(s_id) or 0
            srv_full = bot_db.get_server_by_id(s_id)
            p1 = srv_full[4] if srv_full[4] is not None else bot_db.get_setting("price_1d")
            p7 = srv_full[5] if srv_full[5] is not None else bot_db.get_setting("price_7d")
            p30 = srv_full[6] if srv_full[6] is not None else bot_db.get_setting("price_30d")

            # Логика доступности пробного периода зеркалит buy_menu() в user_handlers.py -
            # держите их синхронизированными при изменении условий.
            u = bot_db.get_user_sub(tg_id, s_id)
            can_trial = True
            if u and u[5] == 1:
                can_trial = False
            elif u and u[8]:
                last_exp = parse_date(u[8])
                if (datetime.now() - last_exp).days < 30:
                    can_trial = False

            servers_info.append({
                "id": s_id, "name": name,
                "free_slots": max(limit - paid_count, 0),
                "price_1d": p1, "price_7d": p7, "price_30d": p30,
                "can_trial": can_trial
            })

    email_acc = bot_db.get_email_account_by_tg(tg_id) if tg_id else None

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "display_name": request.session.get("display_name", "Пользователь"),
            "tg_id": tg_id,
            "accepted_tos": accepted_tos,
            "active_subs": active_subs,
            "servers": servers_info,
            "trial_hours": bot_db.get_setting("trial_hours"),
            # кнопка автооплаты рисуется, только если провайдер включен (payments.py)
            "auto_pay_enabled": payments.is_auto_pay_enabled(),
            # привязанная почта (если есть) - для карточки "Почта" в кабинете
            "bound_email": (email_acc or (None, None))[1],
            "email_has_password": bool(email_acc and email_acc[2]),
            # кнопка перехода в админку - только для администраторов
            "is_admin": is_web_admin(tg_id),
        }
    )

@app.post("/web/accept_tos")
async def web_accept_tos(request: Request):
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не авторизован")
    bot_db.accept_tos(tg_id)
    return RedirectResponse(url="/dashboard", status_code=303)

@app.post("/web/order")
async def web_create_order(request: Request):
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    profile = bot_db.get_user_profile(tg_id)
    if not profile or len(profile) <= 3 or profile[3] != 1:
        raise HTTPException(status_code=403, detail="Сначала примите пользовательское соглашение")

    data = await request.json()
    try:
        server_id = int(data.get("server_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректный сервер")
    period = data.get("period")
    method = data.get("method")

    server = bot_db.get_server_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    if period == "trial":
        u = bot_db.get_user_sub(tg_id, server_id)
        can_trial = True
        if u and u[5] == 1:
            can_trial = False
        elif u and u[8]:
            last_exp = parse_date(u[8])
            if (datetime.now() - last_exp).days < 30:
                can_trial = False
        if not can_trial:
            raise HTTPException(status_code=400, detail="Пробный период сейчас недоступен")

        success, msg = await issue_vpn_access(bot, tg_id, server_id, "trial")
        if not success:
            raise HTTPException(status_code=500, detail=msg)
        return {"success": True, "granted": True}

    if period not in ("1d", "7d", "30d"):
        raise HTTPException(status_code=400, detail="Некорректный период")

    amount = get_price_for_period(server, period)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Не удалось определить цену тарифа")

    if method == "auto":
        if not payments.is_auto_pay_enabled():
            raise HTTPException(status_code=503, detail="Автоматическая оплата временно недоступна. Воспользуйтесь ручной оплатой.")
        try:
            payment_url, uid = await payments.create_order(
                amount, "RUB", tg_id=tg_id, client_ip=request.headers.get("X-Real-IP")
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        bot_db.create_aipay_order(tg_id, server_id, period, amount, uid)
        return {"success": True, "payment_url": payment_url, "uid": uid, "amount": amount}

    elif method == "manual":
        # Не показываем реквизиты сразу - как и в боте, нужен запрос и одобрение админом.
        request_id = bot_db.create_detail_request(tg_id, server_id, period)

        full_name = profile[0] if profile and profile[0] else str(tg_id)
        admin_text = (
            f"👤 <b>Новый запрос реквизитов на оплату (с сайта)!</b>\n\n"
            f"Клиент: <b>{full_name}</b>\n"
            f"ID: <code>{tg_id}</code>\n"
            f"Сервер ID: <b>{server_id}</b>\n"
            f"Период: <b>{period}</b>\n\n"
            f"Выдать пользователю реквизиты?"
        )
        builder = InlineKeyboardBuilder()
        builder.add(InlineKeyboardButton(text="✅ Отправить", callback_data=f"web_app_det_{request_id}"))
        builder.add(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"web_rej_det_{request_id}"))
        builder.adjust(2)

        try:
            await bot.send_message(ADMIN_ID, admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Не удалось отправить запрос администратору: {e}")

        return {"success": True, "amount": amount, "request_id": request_id}

    raise HTTPException(status_code=400, detail="Некорректный способ оплаты")

@app.get("/web/check_details/{request_id}")
async def web_check_details(request: Request, request_id: int):
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    req = bot_db.get_detail_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Запрос не найден")

    r_id, req_tg_id, s_id, period, status = req
    if req_tg_id != tg_id:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    if status == "approved":
        details = bot_db.get_setting("manual_payment_details") or "Реквизиты не заданы администратором."
        return {"status": "approved", "manual_details": details, "server_id": s_id, "period": period}

    return {"status": status}

@app.post("/web/receipt")
async def web_upload_receipt(request: Request, server_id: int = Form(...), period: str = Form(...), file: UploadFile = File(...)):
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    server = bot_db.get_server_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    amount = get_price_for_period(server, period)
    order_id = bot_db.create_manual_order(tg_id, server_id, period, amount)

    profile = bot_db.get_user_profile(tg_id)
    full_name = profile[0] if profile and profile[0] else str(tg_id)

    admin_text = (
        f"🧾 <b>Новая ручная оплата (с сайта)!</b>\n\n"
        f"Клиент: <b>{full_name}</b>\n"
        f"ID: <code>{tg_id}</code>\n"
        f"Сервер ID: {server_id}\n"
        f"Период: {period}\n"
        f"Сумма: {amount} руб.\n\n"
        f"Проверьте чек и подтвердите выдачу."
    )

    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_conf_man_{order_id}"))
    builder.add(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_decl_man_{order_id}"))

    file_bytes = await file.read()
    receipt_file = BufferedInputFile(file_bytes, filename=file.filename or "receipt.jpg")

    try:
        if (file.content_type or "").startswith("image/"):
            await bot.send_photo(ADMIN_ID, receipt_file, caption=admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        else:
            await bot.send_document(ADMIN_ID, receipt_file, caption=admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось отправить чек администратору: {e}")

    return {"success": True, "order_id": order_id}

@app.get("/web/check_aipay/{uid}")
async def web_check_aipay(request: Request, uid: str):
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    order = bot_db.get_order_by_uid(uid)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    order_id, order_tg_id, s_id, period, amount, status = order
    if order_tg_id != tg_id:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    if status == "paid":
        return {"status": "paid"}
    if status in ("failed", "cancelled"):
        return {"status": status}

    try:
        remote_status = await payments.get_order_status(uid)
    except Exception:
        return {"status": "pending"}

    status_id = remote_status.get("id")
    if status_id == payments.STATUS_SUCCESS:
        if bot_db.complete_order_by_uid(uid):
            success, msg = await issue_vpn_access(bot, order_tg_id, s_id, period, notify_admin=True)
            if not success:
                return {"status": "error", "detail": msg}
        return {"status": "paid"}
    elif status_id in (payments.STATUS_ERROR, payments.STATUS_CANCELLED):
        bot_db.fail_order_by_uid(uid, "failed" if status_id == payments.STATUS_ERROR else "cancelled")
        return {"status": "failed"}
    return {"status": "pending"}

@app.get("/web/check_manual/{order_id}")
async def web_check_manual(request: Request, order_id: int):
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    order = bot_db.get_manual_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    o_id, order_tg_id, s_id, period, amount, status = order
    if order_tg_id != tg_id:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    return {"status": status, "server_id": s_id}

@app.post("/web/support")
async def web_support(request: Request):
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    data = await request.json()
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустое сообщение")

    profile = bot_db.get_user_profile(tg_id)
    full_name = profile[0] if profile and profile[0] else str(tg_id)

    admin_text = (
        f"🚨 <b>Новое обращение в поддержку (с сайта)!</b>\n\n"
        f"ID: <code>{tg_id}</code>\n"
        f"Имя: {full_name}\n\n"
        f"<b>Текст:</b>\n{text}"
    )
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✍️ Ответить", callback_data=f"reply_to_{tg_id}"))

    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось отправить сообщение: {e}")

    return {"success": True}

@app.post("/web/reissue")
async def web_reissue_config(request: Request):
    """Разовая перевыдача конфига для активных подписок 'из старых времен' (без сохраненного config_text)."""
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    data = await request.json()
    try:
        server_id = int(data.get("server_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректный сервер")

    # Разрешаем перевыпуск только если конфига еще нет на сайте - чтобы случайно
    # не сбросить ключи у тех, кому и так уже все нормально работает.
    if bot_db.get_user_config(tg_id, server_id):
        raise HTTPException(status_code=400, detail="Конфигурация уже сохранена, перевыпуск не требуется")

    success, msg = await reissue_config_for_active_sub(bot, tg_id, server_id)
    if not success:
        raise HTTPException(status_code=500, detail=msg)

    return {"success": True}

@app.get("/download/{server_id}")
async def download_config(request: Request, server_id: int):
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Необходимо войти через Telegram, чтобы скачать конфигурацию")

    sub = bot_db.get_user_sub(tg_id, server_id)
    if not sub or sub[5] == 0:
        raise HTTPException(status_code=403, detail="Подписка не найдена или неактивна")

    config_text = bot_db.get_user_config(tg_id, server_id)
    if not config_text:
        raise HTTPException(status_code=404, detail="Конфигурация еще не сохранена на сервере. Попробуйте продлить/переоформить доступ.")

    # Имя файла для скачивания отдельно от служебного идентификатора пира (uname/sub[2]) -
    # тот трогать нельзя, он используется на VPN-сервере через SSH-скрипты.
    # Требование клиентских приложений: без пробелов/скобок/подчеркиваний, короткое имя.
    return Response(
        content=config_text,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={tg_id}AWG.conf"}
    )

@app.get("/qr/{server_id}")
async def qr_config(request: Request, server_id: int):
    """PNG с QR-кодом конфига - для быстрого импорта на смартфон прямо с экрана (скан в приложении AmneziaWG)."""
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Необходимо войти через Telegram")

    sub = bot_db.get_user_sub(tg_id, server_id)
    if not sub or sub[5] == 0:
        raise HTTPException(status_code=403, detail="Подписка не найдена или неактивна")

    config_text = bot_db.get_user_config(tg_id, server_id)
    if not config_text:
        raise HTTPException(status_code=404, detail="Конфигурация еще не сохранена. Нажмите «Обновить конфиг».")

    png = qrgen.make_qr_png(config_text)
    if not png:
        raise HTTPException(status_code=503, detail="QR-модуль (segno) не установлен на сервере")
    return Response(content=png, media_type="image/png")

@app.get("/web/config/{server_id}")
async def web_get_config(request: Request, server_id: int):
    """Текст конфига для показа в личном кабинете по кнопке (не встраиваем в HTML страницы изначально)."""
    tg_id = _current_tg_id(request)
    if not tg_id:
        raise HTTPException(status_code=401, detail="Необходимо войти через Telegram")

    sub = bot_db.get_user_sub(tg_id, server_id)
    if not sub or sub[5] == 0:
        raise HTTPException(status_code=403, detail="Подписка не найдена или неактивна")

    config_text = bot_db.get_user_config(tg_id, server_id)
    if not config_text:
        raise HTTPException(status_code=404, detail="Конфигурация еще не сохранена. Нажмите «Обновить конфиг».")
    return {"config": config_text}

@app.post("/webhook/aipay")
async def aipay_webhook(request: Request):
    """
    Вебхук от AiPay о смене статуса заказа.
    Этот URL нужно один раз указать в личном кабинете AiPay (lks.aipay.onl -> Сведения о магазине -> Store webhook URL),
    например: https://ваш-домен.ru/webhook/aipay
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    uid = data.get("uid")
    signature = data.get("signature")
    status = data.get("status") or {}
    status_id = status.get("id")

    if not uid or not signature:
        raise HTTPException(status_code=400, detail="Missing uid/signature")

    if not aipay.verify_webhook_signature(uid, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    order = bot_db.get_order_by_uid(uid)
    if not order:
        # Заказ не найден в нашей базе - отвечаем 200, чтобы AiPay не повторял вебхук впустую
        return {"ok": True}

    order_id, tg_id, s_id, period, amount, order_status = order

    if status_id == aipay.STATUS_SUCCESS:
        if bot_db.complete_order_by_uid(uid):
            success, msg = await issue_vpn_access(bot, tg_id, s_id, period, notify_admin=True)
            if not success:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ AiPay: оплата <code>{uid}</code> подтверждена, но выдача доступа пользователю "
                        f"<code>{tg_id}</code> завершилась ошибкой:\n{msg}",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    elif status_id in (aipay.STATUS_ERROR, aipay.STATUS_CANCELLED):
        bot_db.fail_order_by_uid(uid, "failed" if status_id == aipay.STATUS_ERROR else "cancelled")
        try:
            await bot.send_message(
                tg_id,
                "❌ Оплата не была завершена (отменена или произошла ошибка). "
                "Попробуйте снова или воспользуйтесь ручной оплатой."
            )
        except Exception:
            pass

    return {"ok": True}

@app.post("/webhook/platega")
async def platega_webhook(request: Request):
    """
    Callback от Platega о смене статуса транзакции.
    Этот URL нужно один раз указать в личном кабинете Platega (my.platega.io -> Настройки -> Callback URLs),
    например: https://amneziawg.fun/webhook/platega (только HTTPS с валидным сертификатом).
    Аутентификация - по заголовкам X-MerchantId и X-Secret (проверяются внутри).
    """
    if not platega.verify_webhook_headers(request.headers):
        raise HTTPException(status_code=403, detail="Invalid credentials")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    uid = data.get("id")
    raw_status = (data.get("status") or "").upper()
    if not uid:
        raise HTTPException(status_code=400, detail="Missing id")

    order = bot_db.get_order_by_uid(uid)
    if not order:
        # Не наш заказ - отвечаем 200, чтобы Platega не повторяла callback
        return {"ok": True}

    order_id, tg_id, s_id, period, amount, order_status = order

    if raw_status == "CONFIRMED":
        if bot_db.complete_order_by_uid(uid):
            success, msg = await issue_vpn_access(bot, tg_id, s_id, period, notify_admin=True)
            if not success:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ Platega: оплата <code>{uid}</code> подтверждена, но выдача доступа пользователю "
                        f"<code>{tg_id}</code> завершилась ошибкой:\n{msg}",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    elif raw_status in ("CANCELED", "CHARGEBACKED"):
        bot_db.fail_order_by_uid(uid, "cancelled" if raw_status == "CANCELED" else "failed")
        try:
            await bot.send_message(
                tg_id,
                "❌ Оплата не была завершена (отменена или произошла ошибка). "
                "Попробуйте снова или воспользуйтесь ручной оплатой."
            )
        except Exception:
            pass

    return {"ok": True}

@app.get("/api/captcha")
async def get_captcha():
    now = datetime.utcnow()
    expired = [k for k, v in CAPTCHA_STORE.items() if v["expires"] < now]
    for k in expired: CAPTCHA_STORE.pop(k, None)

    num1, num2 = random.randint(1, 10), random.randint(1, 10)
    session_id = str(uuid.uuid4())
    CAPTCHA_STORE[session_id] = {"answer": num1 + num2, "expires": now + timedelta(minutes=5)}
    return {"captcha_id": session_id, "question": f"Сколько будет {num1} + {num2}?"}

@app.post("/api/get-trial-token")
async def get_trial_token(request: Request):
    data = await request.json()
    captcha_id = data.get("captcha_id")
    captcha_answer = data.get("answer")
    
    if not captcha_id or captcha_id not in CAPTCHA_STORE:
        raise HTTPException(status_code=400, detail="Капча устарела, обновите страницу.")
    
    saved_captcha = CAPTCHA_STORE.pop(captcha_id)
    if saved_captcha["expires"] < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Время действия капчи истекло.")
    
    try:
        if int(captcha_answer) != saved_captcha["answer"]: raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Неверный ответ капчи.")

    client_ip = request.headers.get("X-Real-IP") or request.client.host
    if not client_ip: raise HTTPException(status_code=400, detail="IP не определен.")

    twelve_hours_ago = (datetime.utcnow() - timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')
    if bot_db.get_recent_trial_by_ip(client_ip, twelve_hours_ago):
        raise HTTPException(status_code=400, detail="Доступ уже запрашивался с вашего IP за последние 12 часов.")

    download_token = str(uuid.uuid4())
    DOWNLOAD_STORE[download_token] = {
        "client_ip": client_ip,
        "expires": datetime.utcnow() + timedelta(minutes=2)
    }
    return {"token": download_token}

@app.get("/api/download-trial")
async def download_trial(token: str):
    now = datetime.utcnow()
    expired_tokens = [k for k, v in DOWNLOAD_STORE.items() if v["expires"] < now]
    for k in expired_tokens: DOWNLOAD_STORE.pop(k, None)

    if not token or token not in DOWNLOAD_STORE:
        raise HTTPException(status_code=400, detail="Ссылка для скачивания устарела или недействительна.")

    token_data = DOWNLOAD_STORE.pop(token) 
    client_ip = token_data["client_ip"]

    active_servers = bot_db.get_active_servers() or []
    available_servers = [s for s in active_servers if (s[4] - (bot_db.count_paid_users_on_server(s[0]) or 0)) > 0]

    if not available_servers: 
        raise HTTPException(status_code=400, detail="Нет свободных серверов. Попробуйте позже.")
    
    server = random.choice(available_servers)
    server_id, srv_ip, srv_port = server[0], server[1], server[2]

    try:
        config_text, peer_id = await create_amnezia_peer(srv_ip, srv_port)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка генерации конфига: {str(e)}")

    expires_at = datetime.utcnow() + timedelta(hours=1)
    bot_db.add_free_trial(client_ip, server_id, peer_id, expires_at.strftime('%Y-%m-%d %H:%M:%S'))

    return Response(
        content=config_text,
        media_type="application/octet-stream", 
        headers={"Content-Disposition": "attachment; filename=TWG.conf"}
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)