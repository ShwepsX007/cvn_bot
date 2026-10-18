"""
Веб-админка сервиса AmneziaWG (управление через сайт).

Доступ: только пользователи, вошедшие в кабинет (Telegram-виджет, кнопка через бота
или почта) и имеющие права администратора:
  - главный админ жестко задан в config.ADMIN_ID (его нельзя удалить);
  - дополнительные админы - таблица admins (управляются со страницы «Админы»).

Все страницы под /admin/web/*. Повторяет основные функции телеграм-админки
(/admin в боте): статистика, клиенты, серверы, заявки, настройки, рассылка.
"""

import asyncio
import hmac
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import database as db
import mailer
import payments
import platega
import support as support_service
import traffic as traffic_module
from config import ADMIN_ID

router = APIRouter(prefix="/admin/web", tags=["web-admin"])
templates = Jinja2Templates(directory="templates")

ROOT_ADMIN_ID = int(ADMIN_ID)

# Настройки, чьи значения не показываем целиком в списке и не требуем вводить заново
SECRET_KEYS = ("mail_smtp_password", "mail_resend_key", "aipay_api_key", "platega_secret", "platega_merchant_id")

# Ключи, чьи значения содержат HTML и должны редактироваться в <textarea> большого размера
HTML_KEYS = ("manual_payment_details", "site_banner_html", "analytics_html")

# Названия настроек для подписей в форме
SETTING_LABELS = {
    "trial_hours": "Пробный период (часов)",
    "price_1d": "Цена 1 день (руб)",
    "price_7d": "Цена 7 дней (руб)",
    "price_30d": "Цена 30 дней (руб)",
    "site_url": "Адрес сайта",
    "payment_provider": "Автооплата (off/aipay/platega)",
    "aipay_api_key": "AiPay API-ключ",
    "platega_merchant_id": "Platega Merchant ID",
    "platega_secret": "Platega Secret",
    "platega_payment_method": "Platega ID метода оплаты",
    "manual_payment_details": "Реквизиты ручной оплаты",
    "mail_mode": "Тип отправки почты (auto/smtp/resend/off)",
    "mail_smtp_host": "SMTP хост",
    "mail_smtp_port": "SMTP порт",
    "mail_smtp_user": "SMTP логин",
    "mail_smtp_password": "SMTP пароль",
    "mail_smtp_tls": "SMTP TLS (starttls/ssl/none)",
    "mail_from": "Отправитель писем (From)",
    "mail_resend_key": "Resend API-ключ",
    "container_autoupdate": "Автообновление AWG-контейнера (on/off)",
    "node_update_report": "Отчет автообновления (служебное)",
    "free_hours": "Бесплатный доступ: срок конфига (часов)",
    "free_remind_minutes": "Бесплатный доступ: напоминание в TG за (мин)",
    "traffic_limit_gb": "Трафик: общий лимит по умолчанию (ГБ, 0 = без ограничений)",
    "traffic_warn_percent": "Трафик: предупреждать при использовании (%)",
    "inactive_cleanup_days": "Хранение конфигов отключенных пользователей (дней, 0 = пока числится в базе)",
    "site_banner_html": "Пользовательский рекламный HTML (не отображается на публичных страницах до проверки)",
    "analytics_html": "HTML-код аналитики/счётчика (вставляется перед </body> всех страниц)",
}

# Эти ключи не показываем в таблице настроек: container_autoupdate рисуется отдельным
# чекбоксом, остальные - служебные (меняются кодом, а не руками).
PAYMENT_FORM_KEYS = {"payment_provider", *platega.SETTING_KEYS}
HIDDEN_SETTING_KEYS = {"container_autoupdate", "node_update_report",
                       "stats_baseline_count", "stats_baseline_sum", *PAYMENT_FORM_KEYS}

PERIOD_LABELS = {"trial": "Пробный", "1d": "1 день", "7d": "7 дней", "30d": "30 дней"}


def _admin_tg(request: Request):
    """tg_id админа из сессии или None. Неавторизованных отправляет на /login,
    авторизованных не-админов - 403."""
    tg_id = request.session.get("tg_id")
    if not tg_id:
        return None
    if int(tg_id) == ROOT_ADMIN_ID or db.is_admin_user(int(tg_id)):
        return int(tg_id)
    raise HTTPException(status_code=403, detail="Доступ только для администраторов")


def _ctx(request: Request, page: str, **extra):
    ctx = {
        "request": request,
        "page": page,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error"),
    }
    ctx.update(extra)
    return ctx


def _back(url: str, msg: str = "", error: str = ""):
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url=url, status_code=303)


# ==================== ДАШБОРД ====================
@router.get("")
async def admin_index(request: Request):
    tg_id = _admin_tg(request)
    if not tg_id:
        return RedirectResponse(url="/login")

    servers = db.get_all_servers_full()
    unique_users = len(db.get_unique_user_ids() or [])
    active_subs = db.count_active_subs()
    paid_count, revenue = db.get_paid_revenue_display()
    baseline_active = int(db.get_setting("stats_baseline_count") or 0) > 0 or int(db.get_setting("stats_baseline_sum") or 0) > 0
    pend_det = db.get_pending_detail_requests()
    pend_man = db.get_pending_manual_orders()

    return templates.TemplateResponse(request=request, name="admin_index.html", context=_ctx(
        request, "index",
        servers_total=len(servers),
        servers_active=len([s for s in servers if s[3]]),
        unique_users=unique_users,
        active_subs=active_subs,
        paid_count=paid_count,
        revenue=revenue,
        baseline_active=baseline_active,
        pend_det=len(pend_det),
        pend_man=len(pend_man),
        mail_status=mailer.describe(),
        auto_pay=payments.is_auto_pay_enabled(),
        provider=(db.get_setting("payment_provider") or "off"),
    ))


# ==================== КЛИЕНТЫ ====================
@router.get("/users")
async def admin_users(request: Request):
    tg_id = _admin_tg(request)
    if not tg_id:
        return RedirectResponse(url="/login")

    profiles = db.get_all_profiles()
    users = []
    for p_tg, full_name, username, last_active, accepted_tos, subs_total, subs_active in profiles:
        subs = []
        for sub in (db.get_user_subs(p_tg) or []):
            s_id = sub[1]
            srv = db.get_server_by_id(s_id)
            subs.append({
                "server_id": s_id,
                "server_name": srv[2] if srv else f"#{s_id}",
                "expire": sub[3],
                "active": sub[5] == 1,
                "is_trial": sub[6] == 1 if len(sub) > 6 else False,
            })
        users.append({
            "tg_id": p_tg,
            "name": full_name or str(p_tg),
            "username": username,
            "last_active": last_active,
            "subs": subs,
        })

    servers = db.get_active_servers() or []
    return templates.TemplateResponse(request=request, name="admin_users.html", context=_ctx(
        request, "users", users=users, servers=servers, root_admin=ROOT_ADMIN_ID
    ))


@router.post("/users/grant")
async def admin_user_grant(request: Request, tg_id: int = Form(...), server_id: int = Form(...), period: str = Form(...)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    if period not in PERIOD_LABELS:
        return _back("/admin/web/users", error="Неизвестный период.")

    from bot import bot  # локальный импорт - модуль бота инициализируется в web_app
    from user_handlers import issue_vpn_access

    try:
        db.update_user_profile(tg_id, str(tg_id), None)
        success, msg = await issue_vpn_access(bot, tg_id, server_id, period)
    except Exception as e:
        return _back("/admin/web/users", error=f"Ошибка выдачи: {e}")
    if success:
        return _back("/admin/web/users", msg=f"Доступ ({PERIOD_LABELS[period]}) выдан пользователю {tg_id}.")
    return _back("/admin/web/users", error=f"Не удалось выдать доступ: {msg}")


@router.post("/users/deactivate")
async def admin_user_deactivate(request: Request, tg_id: int = Form(...), server_id: int = Form(...)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    db.deactivate_user(tg_id, server_id)
    return _back("/admin/web/users", msg=f"Подписка пользователя {tg_id} на сервере {server_id} отключена.")


@router.post("/users/delete")
async def admin_user_delete(request: Request, tg_id: int = Form(...), server_id: int = Form(0)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    if int(tg_id) == ROOT_ADMIN_ID:
        return _back("/admin/web/users", error="Нельзя удалить главного администратора.")
    try:
        db.delete_user_completely(tg_id, server_id if server_id else None)
        return _back("/admin/web/users", msg=f"Пользователь {tg_id} удален.")
    except Exception as e:
        return _back("/admin/web/users", error=f"Ошибка удаления: {e}")


# ---------- ЛИЧНОЕ СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЮ ----------
# Любому пользователю можно написать напрямую из админки: сообщение сохраняется
# в его чате (кабинет -> «Поддержка») и продублируется в Telegram, если он есть.

@router.get("/users/message/{tg_id}")
async def admin_user_message_form(request: Request, tg_id: int):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request=request, name="admin_user_message.html", context=_ctx(
        request, "users",
        user=_chat_user_info(tg_id),
        messages=_chat_messages(tg_id)[-8:],  # последние сообщения для контекста
    ))


@router.post("/users/message")
async def admin_user_message_send(request: Request, tg_id: int = Form(...), text: str = Form(...)):
    admin_tg = _admin_tg(request)
    if not admin_tg:
        return RedirectResponse(url="/login")
    text = (text or "").strip()
    if not text:
        return _back(f"/admin/web/users/message/{tg_id}", error="Пустое сообщение.")

    sender_name = "Администратор"

    from bot import bot
    delivered, note = await support_service.reply_and_deliver(
        bot, tg_id, text, sender_name=sender_name, headline="Сообщение от администратора")
    if delivered:
        return _back("/admin/web/users",
                     msg=f"Сообщение пользователю {tg_id} отправлено в Telegram и сохранено в чате.")
    return _back("/admin/web/users",
                 msg=f"Сообщение сохранено в чате — пользователь {tg_id} увидит его в личном кабинете. "
                     f"В Telegram доставить не удалось: {note}")


# ==================== СЕРВЕРЫ ====================
@router.get("/servers")
async def admin_servers(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    servers = []
    for s_id, ip, port, active, name, max_users, p1, p7, p30 in db.get_all_servers_full():
        paid = db.count_paid_users_on_server(s_id) or 0
        subs_total = db.count_server_subs(s_id)
        subs_active = db.count_server_subs(s_id, only_active=True)
        servers.append({
            "id": s_id, "ip": ip, "port": port, "active": bool(active),
            "name": name, "limit": max_users, "paid": paid,
            "price_1d": p1, "price_7d": p7, "price_30d": p30,
            "subs_total": subs_total, "subs_active": subs_active,
        })
    autoupdate = (db.get_setting("container_autoupdate") or "").strip().lower() == "on"
    report = (db.get_setting("node_update_report") or "").strip()
    free_flags = db.get_server_free_flags()
    return templates.TemplateResponse(request=request, name="admin_servers.html", context=_ctx(
        request, "servers", servers=servers, autoupdate=autoupdate, update_report=report, free_flags=free_flags
    ))


@router.post("/servers/add")
async def admin_server_add(request: Request, ip: str = Form(...), name: str = Form(...),
                           port: int = Form(2222), is_free: str = Form("")):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    ip = ip.strip()
    name = name.strip() or "Сервер сервиса"
    if not ip:
        return _back("/admin/web/servers", error="Введите IP сервера.")
    try:
        db.add_server(ip, name, int(port))
        if is_free == "on":
            sid = next((s[0] for s in db.get_all_servers_full() if s[1] == ip), 0)
            if sid:
                db.set_server_free(sid, True)
        note = f"Сервер «{name}» добавлен."
        if is_free == "on":
            note += " Отмечен как 🎁 бесплатный - виден ТОЛЬКО на сайте, в Telegram не дублируется."
        return _back("/admin/web/servers", msg=note)
    except Exception as e:
        return _back("/admin/web/servers", error=f"Ошибка добавления: {e}")


@router.post("/servers/update")
async def admin_server_update(
    request: Request,
    server_id: int = Form(...),
    name: str = Form(...),
    max_users: int = Form(...),
    price_1d: int = Form(0),
    price_7d: int = Form(0),
    price_30d: int = Form(0),
    active: str = Form(""),
    is_free: str = Form(""),
):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    try:
        db.rename_server(server_id, name.strip() or "Сервер сервиса")
        db.set_server_limit(server_id, int(max_users))
        for period, price in (("price_1d", price_1d), ("price_7d", price_7d), ("price_30d", price_30d)):
            if int(price) > 0:
                db.update_server_price(server_id, period, int(price))
        db.set_server_active(server_id, active == "1")
        db.set_server_free(server_id, is_free == "on")
        return _back("/admin/web/servers", msg=f"Сервер #{server_id} обновлен.")
    except Exception as e:
        return _back("/admin/web/servers", error=f"Ошибка обновления: {e}")


@router.post("/servers/delete")
async def admin_server_delete(request: Request, server_id: int = Form(...), mode: str = Form("permanent")):
    """Удаление: permanent - насовсем из базы; disable - только убрать из продажи."""
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    try:
        if mode == "disable":
            db.set_server_active(server_id, False)
            return _back("/admin/web/servers", msg=f"Сервер #{server_id} выключен (убран из продажи).")
        subs_total = db.count_server_subs(server_id)
        subs_active = db.count_server_subs(server_id, only_active=True)
        db.delete_server_permanently(server_id)
        note = f"Сервер #{server_id} удален из базы насовсем."
        if subs_total:
            note += f" Внимание: на нем было подписок: {subs_total} (активных: {subs_active}) - их записи остались, а выданные конфиги продолжат работать до истечения срока."
        return _back("/admin/web/servers", msg=note)
    except Exception as e:
        return _back("/admin/web/servers", error=f"Ошибка удаления: {e}")


@router.post("/servers/awg-update")
async def admin_node_update(request: Request, server_id: int = Form(...)):
    """🔄 Ручное обновление контейнера AmneziaWG на существующей ноде до :latest.
    Конфиги пиров живут в volume и не слетают - клиентам перекачивать ничего не нужно.
    Отдельный путь /servers/awg-update, чтобы не пересекаться с /servers/update (сохранение карточки сервера)."""
    if not _admin_tg(request):
        return RedirectResponse(url="/login")

    srv = db.get_server_by_id(server_id)
    if not srv:
        return _back("/admin/web/servers", error="Сервер не найден в базе.")
    sid = server_id
    ip, port, name = srv[0], srv[1], srv[2]  # get_server_by_id: (ip, port, name, max_users, ...)

    log_lines = []
    try:
        import server_installer
        result = await asyncio.to_thread(server_installer.update_container, ip, port, log_lines.append)
    except Exception as e:
        result = {"ok": False, "changed": False, "udp_port": None,
                  "error": f"{type(e).__name__}: {e}"}

    if result.get("ok"):
        db.update_setting("node_update_report",
                          f"✅ Ручное обновление {name} ({ip}): " +
                          ("контейнер обновлен до свежей сборки." if result.get("changed") else "уже последняя версия - не трогал."))

    return templates.TemplateResponse(request=request, name="admin_node_update.html", context=_ctx(
        request, "servers", ok=result["ok"], changed=result.get("changed", False),
        sid=sid, name=name, ip=ip, udp_port=result.get("udp_port"),
        old_image=result.get("old_image"), new_image=result.get("new_image"),
        log="\n".join(log_lines), error=result.get("error")
    ))


# ==================== МАСТЕР УСТАНОВКИ НОВОГО СЕРВЕРА ====================
@router.get("/servers/wizard")
async def server_wizard_form(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request=request, name="admin_wizard.html", context=_ctx(request, "servers"))


@router.post("/servers/wizard")
async def server_wizard_run(request: Request,
                            ip: str = Form(...),
                            ssh_port: int = Form(2222),
                            ssh_password: str = Form(...),
                            name: str = Form(""),
                            limit: int = Form(20),
                            is_free: str = Form("")):
    """Автоустановка ноды: обмен ключами, AmneziaWG (если нет), скрипты, занесение в базу."""
    if not _admin_tg(request):
        return RedirectResponse(url="/login")

    ip = ip.strip()
    name = name.strip() or "Сервер сервиса"
    if not ip:
        return _back("/admin/web/servers/wizard", error="Укажите IP нового сервера.")

    import server_installer
    # установка идет 1-5 минут (pull образа) - выносим в поток, чтобы не блокировать сайт
    result = await asyncio.to_thread(server_installer.install_new_server, ip, int(ssh_port), ssh_password, name)

    if not result["ok"]:
        return templates.TemplateResponse(request=request, name="admin_wizard_result.html", context=_ctx(
            request, "servers",
            ok=False, ip=ip, name=name, udp_port=result["udp_port"],
            log="\n".join(result["steps"]), error=result["error"]
        ))

    try:
        db.add_server(ip, name, int(ssh_port))
        sid = next((s[0] for s in db.get_all_servers_full() if s[1] == ip), 0) or 0
        db.set_server_limit(sid, int(limit))
        if is_free == "on":
            db.set_server_free(sid, True)
    except Exception as e:
        return templates.TemplateResponse(request=request, name="admin_wizard_result.html", context=_ctx(
            request, "servers",
            ok=False, ip=ip, name=name, udp_port=result["udp_port"],
            log="\n".join(result["steps"]),
            error=f"Нода установлена, но не добавлена в базу: {e}"
        ))

    return templates.TemplateResponse(request=request, name="admin_wizard_result.html", context=_ctx(
        request, "servers",
        ok=True, ip=ip, name=name, udp_port=result["udp_port"],
        log="\n".join(result["steps"]), error=None
    ))


# ==================== СЧЕТЧИК ОПЛАТ (сброс) ====================
@router.post("/stats/reset")
async def stats_reset(request: Request, mode: str = Form("reset")):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    if mode == "clear":
        db.clear_revenue_baseline()
        return _back("/admin/web", msg="Счетчик оплат снова показывает сумму за все время.")
    cnt, total = db.reset_revenue_baseline()
    return _back("/admin/web", msg=f"Счетчик оплат сброшен. Зафиксирована точка отсчета: {cnt} оплат на {total} руб. (история заказов сохранена).")


# ==================== ЗАЯВКИ ====================
@router.get("/requests")
async def admin_requests(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    det = []
    for r_id, r_tg, r_srv, r_period, r_created in db.get_pending_detail_requests():
        srv = db.get_server_by_id(r_srv)
        det.append({"id": r_id, "tg_id": r_tg, "server": srv[2] if srv else f"#{r_srv}",
                    "period": PERIOD_LABELS.get(r_period, r_period), "created": r_created})
    man = []
    for o_id, o_tg, o_srv, o_period, o_amount, o_created in db.get_pending_manual_orders():
        srv = db.get_server_by_id(o_srv)
        man.append({"id": o_id, "tg_id": o_tg, "server": srv[2] if srv else f"#{o_srv}",
                    "period": PERIOD_LABELS.get(o_period, o_period), "amount": o_amount, "created": o_created})
    return templates.TemplateResponse(request=request, name="admin_requests.html", context=_ctx(
        request, "requests", details=det, manuals=man
    ))


@router.post("/requests/details")
async def admin_details_action(request: Request, request_id: int = Form(...), action: str = Form(...)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    req = db.get_detail_request(request_id)
    if not req or req[4] != 'pending':
        return _back("/admin/web/requests", error="Запрос уже обработан или не найден.")
    new_status = "approved" if action == "approve" else "rejected"
    db.set_detail_request_status(request_id, new_status)

    # Уведомим и в телеграм (best effort; на сайте пользователь увидит статус по опросу)
    from bot import bot
    try:
        if new_status == "approved":
            await bot.send_message(req[1], "✅ <b>Ваш запрос на реквизиты одобрен!</b>\nОткройте личный кабинет на сайте - реквизиты уже доступны.", parse_mode="HTML")
        else:
            await bot.send_message(req[1], "❌ Ваш запрос на реквизиты отклонен администратором.", parse_mode="HTML")
    except Exception:
        pass
    return _back("/admin/web/requests", msg=f"Заявка #{request_id} {'одобрена' if action == 'approve' else 'отклонена'}.")


@router.post("/requests/manual")
async def admin_manual_action(request: Request, order_id: int = Form(...), action: str = Form(...)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    order = db.get_manual_order(order_id)
    if not order or order[5] != 'pending':
        return _back("/admin/web/requests", error="Заказ уже обработан или не найден.")

    tg_id, s_id, period = order[1], order[2], order[3]
    from bot import bot

    if action == "confirm":
        db.update_manual_order_status(order_id, 'paid')
        from user_handlers import issue_vpn_access
        try:
            success, msg = await issue_vpn_access(bot, tg_id, s_id, period)
        except Exception as e:
            success, msg = False, str(e)
        if success:
            return _back("/admin/web/requests", msg=f"Платеж #{order_id} подтвержден, конфиг выдан пользователю {tg_id}.")
        return _back("/admin/web/requests", error=f"Платеж #{order_id} отмечен оплаченным, но выдача не удалась: {msg}")

    db.update_manual_order_status(order_id, 'declined')
    try:
        await bot.send_message(tg_id, "❌ <b>Ваш платеж отклонен администратором.</b>\nЕсли вы считаете это ошибкой, обратитесь в поддержку.", parse_mode="HTML")
    except Exception:
        pass
    return _back("/admin/web/requests", msg=f"Платеж #{order_id} отклонен.")


# ==================== ЧАТЫ ПОДДЕРЖКИ ====================
# У каждого пользователя свой чат (все сообщения с одним tg_id). Отвечать можно
# и отсюда, и из Telegram (кнопка «Ответить» под уведомлением) - история общая.
# Ответ сохраняется в чате всегда, а в Telegram дублируется по возможности.

def _chat_user_info(tg_id):
    """Карточка пользователя для страниц чатов: имя, почта, признак «без Telegram»."""
    acc = (db.get_email_account_by_virtual_tg(tg_id) if db.is_virtual_tg_id(tg_id)
           else db.get_email_account_by_tg(tg_id))
    profile = db.get_user_profile(tg_id)
    return {
        "tg_id": tg_id,
        "name": support_service.user_display_name(tg_id),
        "full_name": (profile[1] if profile and len(profile) > 1 and profile[1] else ""),
        "email": (acc[1] if acc else None),
        "is_virtual": db.is_virtual_tg_id(tg_id),
    }


def _fmt_ts(value):
    """'YYYY-MM-DD HH:MM:SS[.fff]' -> 'DD.MM.YYYY HH:MM' (неизвестное не трогаем)."""
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value or "", fmt).strftime("%d.%m.%Y %H:%M")
        except (TypeError, ValueError):
            continue
    return value or ""


def _chat_messages(tg_id):
    rows = []
    for m_id, _m_tg, sender, sender_name, text, created_at in db.get_support_messages(tg_id):
        rows.append({"id": m_id, "sender": sender, "sender_name": sender_name or "",
                     "text": text, "created_at": _fmt_ts(created_at)})
    return rows


@router.get("/chats")
async def admin_chats(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    chats = []
    for tg_id, sender, sender_name, text, last_at, cnt in db.get_support_chats():
        info = _chat_user_info(tg_id)
        chats.append({
            "tg_id": tg_id,
            "name": info["name"],
            "email": info["email"],
            "is_virtual": info["is_virtual"],
            "last_sender": "Клиент" if sender == "user" else "Админ",
            "last_text": (text or "")[:120],
            "last_at": _fmt_ts(last_at),
            "count": cnt,
        })
    return templates.TemplateResponse(request=request, name="admin_chats.html", context=_ctx(
        request, "chats", chats=chats
    ))


@router.get("/chats/{tg_id}")
async def admin_chat_view(request: Request, tg_id: int):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request=request, name="admin_chat.html", context=_ctx(
        request, "chats",
        user=_chat_user_info(tg_id),
        messages=_chat_messages(tg_id),
    ))


@router.post("/chats/{tg_id}/reply")
async def admin_chat_reply(request: Request, tg_id: int, text: str = Form(...)):
    admin_tg = _admin_tg(request)
    if not admin_tg:
        return RedirectResponse(url="/login")
    text = (text or "").strip()
    if not text:
        return _back(f"/admin/web/chats/{tg_id}", error="Пустое сообщение.")

    sender_name = "Администратор"

    # Сохраняем в чате всегда; в Telegram дублируем по возможности.
    from bot import bot
    delivered, note = await support_service.reply_and_deliver(bot, tg_id, text, sender_name=sender_name)
    if delivered:
        return _back(f"/admin/web/chats/{tg_id}", msg="Ответ отправлен пользователю в Telegram и сохранён в чате.")
    return _back(f"/admin/web/chats/{tg_id}",
                 msg=f"Ответ сохранён в чате — пользователь увидит его на сайте. "
                     f"В Telegram доставить не удалось: {note}")


@router.post("/chats/{tg_id}/delete")
async def admin_chat_delete(request: Request, tg_id: int):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    db.delete_support_chat(tg_id)
    return _back("/admin/web/chats", msg=f"Чат пользователя {tg_id} удален.")


# ==================== ТРАФИК ====================
# Управление лимитами трафика: общий по умолчанию (для всех), личные лимиты
# по каждой подписке, сброс счетчиков и принудительная синхронизация с нод.

def _parse_limit_gb(raw):
    """'10', '10,5', '0' -> число (0 = без лимита); пустая строка -> None (общий)."""
    raw = (raw or "").strip().replace(",", ".")
    if raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("Лимит должен быть числом (гигабайты), 0 = без ограничений")
    if value < 0:
        raise ValueError("Лимит не может быть отрицательным")
    return value


@router.get("/traffic")
async def admin_traffic(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    global_limit = db.get_setting("traffic_limit_gb") or "0"
    warn_percent = db.get_setting("traffic_warn_percent") or "80"

    rows = []
    for (tg_id, s_id, username, expire, active, limit_gb, used, warned, over) in db.get_all_subs_with_traffic():
        srv = db.get_server_by_id(s_id)
        limit_bytes = traffic_module.resolve_limit_bytes(limit_gb)
        used = used or 0
        percent = min(100, round(used * 100 / limit_bytes)) if limit_bytes else 0
        info = _chat_user_info(tg_id)
        rows.append({
            "tg_id": tg_id,
            "server_id": s_id,
            "server_name": srv[2] if srv else f"#{s_id}",
            "name": info["name"],
            "email": info["email"],
            "is_virtual": info["is_virtual"],
            "expire": _fmt_ts(expire)[:16] if expire else "—",
            "active": active == 1,
            # None = общий по умолчанию, 0 = без лимита, >0 = ГБ
            "limit_gb": limit_gb,
            "limit_display": ("без лимита" if not limit_bytes
                              else (f"общий ({global_limit} ГБ)" if limit_gb is None
                                    else f"{limit_gb:g} ГБ")),
            "used_display": traffic_module.format_bytes(used),
            "percent": percent,
            "warned": warned == 1,
            "over": over == 1,
        })
    return templates.TemplateResponse(request=request, name="admin_traffic.html", context=_ctx(
        request, "traffic",
        rows=rows, global_limit=global_limit, warn_percent=warn_percent,
    ))


@router.post("/traffic/settings")
async def admin_traffic_settings(request: Request,
                                 global_limit_gb: str = Form("0"),
                                 warn_percent: str = Form("0")):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    try:
        limit = _parse_limit_gb(global_limit_gb)
        warn = int((warn_percent or "0").strip().replace(",", "."))
        if warn < 0 or warn > 100:
            raise ValueError("Процент предупреждения — от 0 до 100")
    except ValueError as e:
        return _back("/admin/web/traffic", error=str(e))
    db.update_settings({
        "traffic_limit_gb": "0" if limit in (None, 0) else f"{limit:g}",
        "traffic_warn_percent": str(warn),
    })
    from bot import bot
    restored = await traffic_module.restore_after_limit_change(bot=bot)
    extra = f" Возвращено в строй: {restored}." if restored else ""
    return _back("/admin/web/traffic", msg=f"Общие настройки трафика сохранены.{extra}")


@router.post("/traffic/limit")
async def admin_traffic_set_limit(request: Request, tg_id: int = Form(...),
                                  server_id: int = Form(...), limit_gb: str = Form("")):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    try:
        limit = _parse_limit_gb(limit_gb)
    except ValueError as e:
        return _back("/admin/web/traffic", error=str(e))
    if not db.get_user_traffic(tg_id, server_id):
        return _back("/admin/web/traffic", error="Подписка не найдена.")
    db.set_user_traffic_limit(tg_id, server_id, limit)
    desc = "общий по умолчанию" if limit is None else ("без лимита" if not limit else f"{limit:g} ГБ")
    # Лимит подняли - если подписка была отключена по старому лимиту, вернуть её.
    from bot import bot
    restored = await traffic_module.restore_after_limit_change(bot=bot)
    extra = f" Доступ восстановлен автоматически." if restored else ""
    return _back("/admin/web/traffic", msg=f"Лимит трафика для {tg_id} (сервер {server_id}): {desc}.{extra}")


@router.post("/traffic/reset")
async def admin_traffic_reset(request: Request, tg_id: int = Form(...), server_id: int = Form(...)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    db.reset_user_traffic(tg_id, server_id, full=True)
    from bot import bot
    restored = await traffic_module.restore_after_limit_change(bot=bot)
    extra = f" Доступ восстановлен автоматически." if restored else ""
    return _back("/admin/web/traffic",
                 msg=f"Счётчик трафика пользователя {tg_id} (сервер {server_id}) обнулён.{extra}")


@router.post("/traffic/apply-all")
async def admin_traffic_apply_all(request: Request, limit_gb: str = Form("")):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    try:
        limit = _parse_limit_gb(limit_gb)
    except ValueError as e:
        return _back("/admin/web/traffic", error=str(e))
    if limit is None:
        return _back("/admin/web/traffic", error="Укажите лимит для применения ко всем (0 = без лимита).")
    db.set_all_traffic_limits(limit)
    desc = "без лимита" if not limit else f"{limit:g} ГБ"
    from bot import bot
    restored = await traffic_module.restore_after_limit_change(bot=bot)
    extra = f" Возвращено в строй: {restored}." if restored else ""
    return _back("/admin/web/traffic", msg=f"Лимит {desc} установлен для ВСЕХ подписок.{extra}")


@router.post("/traffic/clear-all")
async def admin_traffic_clear_all(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    db.set_all_traffic_limits(None)
    from bot import bot
    restored = await traffic_module.restore_after_limit_change(bot=bot)
    extra = f" Возвращено в строй: {restored}." if restored else ""
    return _back("/admin/web/traffic",
                 msg=f"Личные лимиты сброшены: все подписки используют общий по умолчанию.{extra}")


@router.post("/traffic/sync")
async def admin_traffic_sync(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    try:
        updated = await traffic_module.sync_all()
    except Exception as e:
        return _back("/admin/web/traffic", error=f"Ошибка синхронизации: {e}")
    return _back("/admin/web/traffic", msg=f"Синхронизация завершена: обновлено записей — {updated}.")


# ==================== НАСТРОЙКИ ====================
def _settings_csrf(request: Request) -> str:
    token = request.session.get("settings_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["settings_csrf"] = token
    return token


def _verify_settings_csrf(request: Request, token: str):
    expected = request.session.get("settings_csrf") or ""
    if not expected or not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="Обновите страницу настроек и повторите сохранение.")


@router.get("/settings")
async def admin_settings(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    rows = []
    for key, value in db.get_all_settings():
        if key in HIDDEN_SETTING_KEYS:
            continue
        is_secret = key in SECRET_KEYS
        is_html = key in HTML_KEYS
        rows.append({
            "key": key,
            "label": SETTING_LABELS.get(key, key),
            "value": "" if is_secret else (value or ""),
            "masked": f"•••{str(value)[-4:]}" if is_secret and value else "",
            "is_secret": is_secret,
            "is_html": is_html,
            "is_long": (len(value or "") > 40 or is_html) and not is_secret,
            "textarea_rows": 8 if is_html else 2,
        })
    try:
        webhook_url = platega.get_webhook_url()
    except ValueError:
        webhook_url = ""
    payment_settings = {
        "provider": payments.get_provider(),
        "merchant_set": bool(db.get_setting("platega_merchant_id")),
        "secret_set": bool(db.get_setting("platega_secret")),
        "method": db.get_setting("platega_payment_method") or platega.DEFAULT_PAYMENT_METHOD,
        "site_url": db.get_setting("site_url") or "",
        "webhook_url": webhook_url,
        "api_url": platega.BASE_URL,
        "errors": payments.configuration_errors("platega"),
        "enabled": payments.is_auto_pay_enabled(),
    }
    return templates.TemplateResponse(request=request, name="admin_settings.html", context=_ctx(
        request, "settings", rows=rows, mail_status=mailer.describe(),
        payment_settings=payment_settings, csrf_token=_settings_csrf(request),
        container_autoupdate=(db.get_setting("container_autoupdate") or "").strip().lower() == "on"
    ))


@router.post("/settings/payments")
async def admin_payment_settings_save(
    request: Request,
    payment_provider: str = Form(...),
    platega_merchant_id: str = Form(""),
    platega_secret: str = Form(""),
    platega_payment_method: str = Form(...),
    site_url: str = Form(...),
    csrf_token: str = Form(""),
):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    _verify_settings_csrf(request, csrf_token)
    values = {
        "payment_provider": payment_provider,
        "platega_payment_method": platega_payment_method,
        "site_url": site_url,
        # Пустые поля не удаляют ранее сохранённые реквизиты.
        "platega_merchant_id": platega_merchant_id.strip() or db.get_setting("platega_merchant_id") or "",
        "platega_secret": platega_secret.strip() or db.get_setting("platega_secret") or "",
    }
    try:
        values = {
            key: payments.normalize_setting(key, value) if value or key not in SECRET_KEYS else ""
            for key, value in values.items()
        }
        payments.validate_configuration(values["payment_provider"], values)
    except ValueError as exc:
        return _back("/admin/web/settings", error=str(exc))
    db.update_settings(values)
    return _back("/admin/web/settings", msg="Настройки оплаты сохранены. Не забудьте указать Callback URL в личном кабинете Platega.")


@router.post("/settings")
async def admin_settings_save(request: Request, key: str = Form(...), value: str = Form(""),
                              csrf_token: str = Form("")):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    _verify_settings_csrf(request, csrf_token)
    key = key.strip()
    value = value.strip()
    # Пустое значение секретного поля = «не менять»
    if key in SECRET_KEYS and value == "":
        return _back("/admin/web/settings", msg=f"{key}: без изменений.")
    if key in payments.SETTING_KEYS:
        try:
            value = payments.normalize_setting(key, value)
            if key == "payment_provider":
                payments.validate_configuration(value)
        except ValueError as exc:
            return _back("/admin/web/settings", error=str(exc))
    db.update_setting(key, value)
    return _back("/admin/web/settings", msg=f"Настройка «{SETTING_LABELS.get(key, key)}» сохранена.")


# ==================== АДМИНЫ ====================
@router.get("/admins")
async def admin_admins(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    rows = []
    seen = set()
    for a_tg, added_at, added_by in db.list_admins():
        prof = db.get_user_profile(a_tg)
        rows.append({"tg_id": a_tg, "name": (prof[0] if prof else ""), "username": (prof[1] if prof else ""),
                     "added_at": added_at, "is_root": False})
        seen.add(a_tg)
    if ROOT_ADMIN_ID not in seen:
        prof = db.get_user_profile(ROOT_ADMIN_ID)
        rows.insert(0, {"tg_id": ROOT_ADMIN_ID, "name": (prof[0] if prof else ""), "username": (prof[1] if prof else ""),
                        "added_at": "—", "is_root": True})
    else:
        for r in rows:
            if r["tg_id"] == ROOT_ADMIN_ID:
                r["is_root"] = True
    return templates.TemplateResponse(request=request, name="admin_admins.html", context=_ctx(request, "admins", admins=rows))


@router.post("/admins/add")
async def admin_add(request: Request, tg_id: int = Form(...)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    db.add_admin(int(tg_id), ROOT_ADMIN_ID)
    return _back("/admin/web/admins", msg=f"Пользователь {tg_id} назначен администратором.")


@router.post("/admins/remove")
async def admin_remove(request: Request, tg_id: int = Form(...)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    if int(tg_id) == ROOT_ADMIN_ID:
        return _back("/admin/web/admins", error="Главного администратора удалить нельзя (он задан в config.py).")
    db.remove_admin(int(tg_id))
    return _back("/admin/web/admins", msg=f"Пользователь {tg_id} снят с администраторов.")


# ==================== РАССЫЛКА ====================
@router.get("/broadcast")
async def admin_broadcast_form(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request=request, name="admin_broadcast.html", context=_ctx(
        request, "broadcast", users_count=len(db.get_unique_user_ids() or [])
    ))


@router.post("/mailtest")
async def admin_mailtest(request: Request, to: str = Form(...)):
    """Проверка доставки почты прямо из настроек: шлем тестовое письмо и показываем точный ответ."""
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    to = to.strip()
    if "@" not in to:
        return _back("/admin/web/settings", error="Введите адрес для тестового письма.")
    site_url = (db.get_setting("site_url") or "https://amneziawg.fun").rstrip("/")
    html = mailer._letter_html(
        "✉️ Почта работает!",
        "Это тестовое письмо из веб-админки сервиса AmneziaWG. Если оно пришло во <b>входящие</b> (а не в спам) - настройка почты удалась, регистрация по почте будет работать.",
        site_url,
        "Открыть сайт",
        "Письмо отправлено администратором вручную для проверки доставки."
    )
    ok, err = await asyncio.to_thread(mailer.send_email, to, "Тестовое письмо — сервис AmneziaWG", html)
    print(f"MAIL TEST to {to}: ok={ok} err={err}")
    if ok:
        return _back("/admin/web/settings", msg=f"Письмо принято почтовым сервисом ({mailer.describe()}). Проверьте {to}: входящие И ПАПКУ СПАМ. Также проверьте вкладку Emails->Logs в личном кабинете Resend.")
    return _back("/admin/web/settings", error=f"Почтовый сервис ОТКЛОНИЛ отправку: {err}")


@router.post("/broadcast")
async def admin_broadcast_send(request: Request, text: str = Form(...)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    text = text.strip()
    if not text:
        return _back("/admin/web/broadcast", error="Введите текст рассылки.")

    from bot import bot
    ids = db.get_unique_user_ids() or []
    sent, failed = 0, 0
    for uid in ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    return _back("/admin/web/broadcast", msg=f"Рассылка завершена: доставлено {sent}, не доставлено {failed} (чат был недоступен).")
