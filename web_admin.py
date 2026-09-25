"""
Веб-админка AmneziaWG VPN (управление через сайт).

Доступ: только пользователи, вошедшие в кабинет (Telegram-виджет, кнопка через бота
или почта) и имеющие права администратора:
  - главный админ жестко задан в config.ADMIN_ID (его нельзя удалить);
  - дополнительные админы - таблица admins (управляются со страницы «Админы»).

Все страницы под /admin/web/*. Повторяет основные функции телеграм-админки
(/admin в боте): статистика, клиенты, серверы, заявки, настройки, рассылка.
"""

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import database as db
import mailer
import payments
from config import ADMIN_ID

router = APIRouter(prefix="/admin/web", tags=["web-admin"])
templates = Jinja2Templates(directory="templates")

ROOT_ADMIN_ID = int(ADMIN_ID)

# Настройки, чьи значения не показываем целиком в списке и не требуем вводить заново
SECRET_KEYS = ("mail_smtp_password", "mail_resend_key", "aipay_api_key", "platega_secret", "platega_merchant_id")

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
}

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
    return templates.TemplateResponse(request=request, name="admin_servers.html", context=_ctx(request, "servers", servers=servers))


@router.post("/servers/add")
async def admin_server_add(request: Request, ip: str = Form(...), name: str = Form(...), port: int = Form(2222)):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    ip = ip.strip()
    name = name.strip() or "VPN Server"
    if not ip:
        return _back("/admin/web/servers", error="Введите IP сервера.")
    try:
        db.add_server(ip, name, int(port))
        return _back("/admin/web/servers", msg=f"Сервер «{name}» добавлен.")
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
):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    try:
        db.rename_server(server_id, name.strip() or "VPN Server")
        db.set_server_limit(server_id, int(max_users))
        for period, price in (("price_1d", price_1d), ("price_7d", price_7d), ("price_30d", price_30d)):
            if int(price) > 0:
                db.update_server_price(server_id, period, int(price))
        db.set_server_active(server_id, active == "1")
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
                            limit: int = Form(20)):
    """Автоустановка ноды: обмен ключами, AmneziaWG (если нет), скрипты, занесение в базу."""
    if not _admin_tg(request):
        return RedirectResponse(url="/login")

    ip = ip.strip()
    name = name.strip() or "VPN Server"
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
        db.set_server_limit(next((s[0] for s in db.get_all_servers_full() if s[1] == ip), 0) or 0, int(limit))
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


# ==================== НАСТРОЙКИ ====================
@router.get("/settings")
async def admin_settings(request: Request):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    rows = []
    for key, value in db.get_all_settings():
        is_secret = key in SECRET_KEYS
        rows.append({
            "key": key,
            "label": SETTING_LABELS.get(key, key),
            "value": "" if is_secret else (value or ""),
            "masked": f"•••{str(value)[-4:]}" if is_secret and value else "",
            "is_secret": is_secret,
            "is_long": len(value or "") > 40 and not is_secret,
        })
    return templates.TemplateResponse(request=request, name="admin_settings.html", context=_ctx(
        request, "settings", rows=rows, mail_status=mailer.describe()
    ))


@router.post("/settings")
async def admin_settings_save(request: Request, key: str = Form(...), value: str = Form("")):
    if not _admin_tg(request):
        return RedirectResponse(url="/login")
    key = key.strip()
    value = value.strip()
    # Пустое значение секретного поля = «не менять»
    if key in SECRET_KEYS and value == "":
        return _back("/admin/web/settings", msg=f"{key}: без изменений.")
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
        "Это тестовое письмо из веб-админки AmneziaWG VPN. Если оно пришло во <b>входящие</b> (а не в спам) - настройка почты удалась, регистрация по почте будет работать.",
        site_url,
        "Открыть сайт",
        "Письмо отправлено администратором вручную для проверки доставки."
    )
    ok, err = await asyncio.to_thread(mailer.send_email, to, "Тестовое письмо — AmneziaWG VPN", html)
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
    return _back("/admin/web/broadcast", msg=f"Рассылка завершена: доставлено {sent}, не доставлено {failed} (заблокировали бота).")
