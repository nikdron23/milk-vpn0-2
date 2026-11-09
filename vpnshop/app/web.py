"""Публичный сайт, личный кабинет, подписка и callback'и ЮKassa."""
import base64
import logging
import re
from datetime import timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import select

from app import db, payments, services
from app.common import bot_link, csrf_check, flash, message_page, redirect, render
from app.config import get_settings
from app.models import Key, Order, Plan, User
from app.security import read_login_token
from app.xui import XUIError

log = logging.getLogger(__name__)
router = APIRouter()


# ---------- публичные страницы ----------
@router.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    async with db.Session() as s:
        plans = await services.active_plans(s)
        servers = await services.public_servers(s)
    best = min(plans, key=lambda p: p.price_kop / max(p.days, 1)).id if len(plans) > 1 else None
    return render(request, "landing.html", {"plans": plans, "servers": servers, "best_plan_id": best})


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return render(request, "setup.html")


@router.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return render(request, "terms.html")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return render(request, "privacy.html")


@router.get("/r/{code}")
async def referral_redirect(code: str):
    """Короткая реферальная ссылка для сайта: /r/<код> -> бот с параметром start."""
    link = bot_link()
    if link and re.fullmatch(r"[0-9a-zA-Z]{4,16}", code):
        return redirect(f"{link}?start=r_{code}")
    return redirect("/")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return render(request, "login.html")


@router.get("/auth")
async def auth(request: Request, token: str = ""):
    tg_id = read_login_token(token)
    user = None
    if tg_id is not None:
        async with db.Session() as s:
            user = await s.scalar(select(User).where(User.tg_id == tg_id))
    if user is None or user.is_banned:
        return message_page(
            request, "Ссылка недействительна",
            "Ссылка устарела или уже недействительна. Запросите новую в боте — кнопка «Личный кабинет».",
            400, ("/login", "Как войти"),
        )
    request.session.clear()  # защита от фиксации сессии
    request.session["uid"] = user.id
    return redirect("/cabinet")


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect("/")


# ---------- подписка для VPN-приложений ----------
@router.get("/sub/{sub_id}")
async def subscription(sub_id: str):
    """Ссылка-подписка: приложение (Hiddify, v2rayNG, Streisand…) получает все локации ключа сразу."""
    if not re.fullmatch(r"[0-9a-f]{8,32}", sub_id):
        raise HTTPException(404)
    cfg = get_settings()
    async with db.Session() as s:
        key = await s.scalar(select(Key).where(Key.sub_id == sub_id))
        if key is None or not key.active or key.user.is_banned:
            raise HTTPException(404)
        links = await services.key_links(s, key)
        usage = await services.safe_traffic(s, key)
        expire = int(key.expires_at.replace(tzinfo=timezone.utc).timestamp())
    if not links:
        return PlainTextResponse("Серверы временно недоступны, попробуйте позже", status_code=503)
    used, total = usage if usage else (0, 0)
    headers = {
        "Profile-Title": "base64:" + base64.b64encode(cfg.brand_name.encode()).decode(),
        "Profile-Update-Interval": "6",
        "Subscription-Userinfo": f"upload=0; download={used}; total={total}; expire={expire}",
        "Cache-Control": "no-store",
    }
    if cfg.support_username:
        headers["Support-URL"] = f"https://t.me/{cfg.support_username.lstrip('@')}"
    body = base64.b64encode("\n".join(link for _, link in links).encode()).decode()
    return PlainTextResponse(body, headers=headers)


# ---------- ЮKassa ----------
@router.post("/payment/yookassa")
async def payment_webhook(request: Request):
    """HTTP-уведомление ЮKassa. Само уведомление не подписано, поэтому берём из него только id платежа,
    а состояние проверяем запросом в API ЮKassa. Ответ 200 — «принято»; 5xx — ЮKassa повторит позже."""
    try:
        body = await request.json()
    except ValueError:
        return PlainTextResponse("bad json", status_code=400)
    obj = body.get("object") if isinstance(body, dict) else None
    pid = obj.get("id") if isinstance(obj, dict) else None
    if not isinstance(pid, str) or not re.fullmatch(r"[0-9a-zA-Z\-]{8,64}", pid):
        return PlainTextResponse("bad payload", status_code=400)
    if body.get("event") not in ("payment.succeeded", "payment.canceled", "payment.waiting_for_capture"):
        return PlainTextResponse("ignored")
    try:
        outcome = await services.settle_payment(pid)
    except payments.PaymentNotFound:
        log.warning("уведомление о неизвестном платеже %s", pid)
        return PlainTextResponse("unknown payment")
    except (XUIError, services.ServiceError, payments.PaymentError):
        log.exception("не удалось обработать платёж %s", pid)
        return PlainTextResponse("temporary error", status_code=500)
    return PlainTextResponse(outcome)


@router.get("/payment/return")
async def payment_return(request: Request, order: int = 0):
    """Сюда возвращается пользователь после оплаты. Статус проверяем в ЮKassa — это работает,
    даже если уведомления на сервер ещё не настроены."""
    async with db.Session() as s:
        o = await s.get(Order, order) if order > 0 else None
        payment_id = o.payment_id if o else None
        status = o.status if o else None
    if o is None:
        return message_page(request, "Заказ не найден", "Проверьте ссылку или откройте бота.", 404,
                            ("/login", "Личный кабинет"))
    if status == "pending" and payment_id:
        try:
            await services.settle_payment(payment_id)
        except Exception:  # noqa: BLE001 — покажем нейтральное сообщение, дальше доработают уведомление/сверка
            log.warning("return: не удалось проверить платёж %s", payment_id, exc_info=True)
        async with db.Session() as s:
            status = (await s.get(Order, order)).status
    if status == "paid":
        return message_page(
            request, "Спасибо!", "Оплата получена. Ключ уже отправлен вам в Telegram и доступен в личном кабинете.",
            link=("/login", "Личный кабинет"),
        )
    if status == "canceled":
        return message_page(
            request, "Оплата не завершена", "Платёж не прошёл или был отменён. Деньги не списаны — попробуйте ещё раз.",
            link=("/login", "Личный кабинет"),
        )
    return message_page(
        request, "Платёж обрабатывается", "Как только банк подтвердит оплату, ключ придёт в Telegram. Обычно это "
        "занимает меньше минуты.", link=("/login", "Личный кабинет"),
    )


# ---------- личный кабинет ----------
async def current_user(request: Request) -> User:
    uid = request.session.get("uid")
    user = None
    if uid:
        async with db.Session() as s:
            user = await s.get(User, uid)
    if user is None or user.is_banned:
        request.session.pop("uid", None)
        raise HTTPException(303, headers={"Location": "/login"})
    return user


@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request, user: User = Depends(current_user)):
    async with db.Session() as s:
        fresh = await s.get(User, user.id)
        await services.ensure_ref_code(s, fresh)
        await s.commit()
        keys = list(
            await s.scalars(
                select(Key).where(Key.user_id == user.id, Key.active.is_(True)).order_by(Key.expires_at.desc())
            )
        )
        orders = list(
            await s.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(10))
        )
        plans = await services.active_plans(s)
        prices = await services.user_prices(s, fresh, plans)
        stats = await services.referral_stats(s, fresh)
        usage = await services.bulk_traffic(s, keys)
    link = bot_link()
    return render(
        request, "cabinet.html",
        {"user": fresh, "keys": keys, "orders": orders, "plans": plans, "prices": prices, "usage": usage,
         "stats": stats,
         "trial": services.trial_available(fresh),
         "ref_link": f"{link}?start=r_{fresh.ref_code}" if link else ""},
    )


@router.get("/cabinet/key/{key_id}", response_class=HTMLResponse)
async def cabinet_key(request: Request, key_id: int, user: User = Depends(current_user)):
    async with db.Session() as s:
        key = await s.get(Key, key_id)
        if not key or key.user_id != user.id or not key.active:
            raise HTTPException(404, "Ключ не найден")
        plans = await services.active_plans(s)
        prices = await services.user_prices(s, user, plans)
        servers = await services.key_servers(s, key)
        usage = await services.safe_traffic(s, key)
    url = services.sub_url(key)
    return render(
        request, "key.html",
        {"key": key, "sub_url": url, "servers": servers, "usage": usage, "plans": plans, "prices": prices,
         "qr": services.qr_svg(url)},
    )


@router.post("/cabinet/buy", dependencies=[Depends(csrf_check)])
async def cabinet_buy(
    request: Request, plan_id: int = Form(...), key_id: int = Form(0), user: User = Depends(current_user)
):
    async with db.Session() as s:
        plan = await s.get(Plan, plan_id)
        if plan is None:
            raise HTTPException(404, "Тариф не найден")
        try:
            _, url = await services.create_order(s, user, plan, key_id or None)
        except services.ServiceError as e:
            flash(request, str(e))
            return redirect("/cabinet")
    return redirect(url)


@router.post("/cabinet/trial", dependencies=[Depends(csrf_check)])
async def cabinet_trial(request: Request, user: User = Depends(current_user)):
    async with db.Session() as s:
        fresh = await s.get(User, user.id)
        try:
            key = await services.start_trial(s, fresh)
        except services.ServiceError as e:
            flash(request, str(e))
            return redirect("/cabinet")
    flash(request, "Пробный ключ создан")
    return redirect(f"/cabinet/key/{key.id}")
