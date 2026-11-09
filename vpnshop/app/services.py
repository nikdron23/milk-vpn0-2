"""Бизнес-логика: серверы, выдача/продление ключей, подписка, заказы, рефералка.
Не зависит от Telegram и веб-слоя."""
import asyncio
import io
import logging
import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlparse

import segno
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto, db, payments
from app import runtime as rt
from app.config import get_settings
from app.models import Key, KeyServer, Order, Plan, ReferralReward, Server, User, UserPrice, utcnow
from app.xui import XUIClient, XUIError, build_vless_link

log = logging.getLogger(__name__)

_fulfil_lock = asyncio.Lock()
_bg_tasks: set[asyncio.Task] = set()


class ServiceError(Exception):
    """Ошибка, текст которой безопасно показать пользователю."""


# ======================================================================
# Пул клиентов 3x-ui (по одному на сервер)
# ======================================================================
_factory = None
_pool: dict[int, tuple[tuple, object]] = {}


def default_factory(server: Server) -> XUIClient:
    return XUIClient(
        server.url, server.username, crypto.decrypt(server.password_enc), server.inbound_id,
        flow=server.flow, verify_tls=server.verify_tls,
        token=crypto.decrypt(server.api_token_enc) if server.api_token_enc else "",
    )


def set_factory(fn) -> None:
    """Подмена создания клиентов (нужна тестам)."""
    global _factory
    _factory = fn
    _pool.clear()


def get_xui(server: Server):
    sig = (server.url, server.username, server.password_enc, server.api_token_enc, server.inbound_id,
           server.flow, server.verify_tls)
    cached = _pool.get(server.id)
    if cached and cached[0] == sig:
        return cached[1]
    client = (_factory or default_factory)(server)
    if cached and hasattr(cached[1], "aclose"):
        _spawn(cached[1].aclose())
    _pool[server.id] = (sig, client)
    return client


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def close_all() -> None:
    for _, client in list(_pool.values()):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    _pool.clear()


def to_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def gb(n: int) -> int:
    return int(n) * 1024**3


# ======================================================================
# Серверы
# ======================================================================
async def active_servers(s: AsyncSession) -> list[Server]:
    return list(await s.scalars(select(Server).where(Server.active.is_(True)).order_by(Server.sort, Server.id)))


async def public_servers(s: AsyncSession) -> list[Server]:
    return await active_servers(s)


def set_server_password(server: Server, plain: str) -> None:
    server.password_enc = crypto.encrypt(plain)


def set_server_token(server: Server, plain: str) -> None:
    server.api_token_enc = crypto.encrypt(plain.strip()) if plain and plain.strip() else None


async def ping_server(server: Server) -> tuple[bool, str]:
    try:
        info = await asyncio.wait_for(get_xui(server).ping(), timeout=8)
    except asyncio.TimeoutError:
        return False, "таймаут"
    except (XUIError, ValueError) as e:
        return False, str(e)[:200]
    return True, info


async def ping_all(servers: list[Server]) -> dict[int, tuple[bool, str]]:
    results = await asyncio.gather(*(ping_server(sv) for sv in servers))
    return {sv.id: r for sv, r in zip(servers, results)}


async def seed_server_from_env(s: AsyncSession) -> None:
    """Первый запуск: если серверов нет, а в .env заданы XUI_* — создаём сервер из них."""
    cfg = get_settings()
    if not cfg.xui_url or await s.scalar(select(func.count(Server.id))):
        return
    server = Server(
        name=cfg.xui_name, country=cfg.xui_country.upper()[:2], url=cfg.xui_url.rstrip("/"),
        username=cfg.xui_username, inbound_id=cfg.xui_inbound_id, flow=cfg.xui_flow,
        verify_tls=cfg.xui_verify_tls, public_host=cfg.xui_public_host,
    )
    set_server_password(server, cfg.xui_password)
    s.add(server)
    await s.commit()
    log.info("Сервер «%s» создан из переменных XUI_* — остальные добавляйте в админке", server.name)


# ======================================================================
# Пользователи, тарифы, рефералка
# ======================================================================
async def _new_ref_code(s: AsyncSession) -> str:
    for _ in range(10):
        code = secrets.token_hex(4)
        if not await s.scalar(select(User.id).where(User.ref_code == code)):
            return code
    return secrets.token_hex(8)


async def ensure_ref_code(s: AsyncSession, user: User) -> str:
    if not user.ref_code:
        user.ref_code = await _new_ref_code(s)
        await s.flush()
    return user.ref_code


async def register_user(
    s: AsyncSession, tg_id: int, username: str | None, first_name: str | None
) -> tuple[User, bool]:
    user = await s.scalar(select(User).where(User.tg_id == tg_id))
    created = user is None
    if created:
        user = User(tg_id=tg_id, username=username, first_name=first_name)
        s.add(user)
    else:
        user.username, user.first_name = username, first_name
    await s.flush()
    await ensure_ref_code(s, user)
    return user, created


async def upsert_user(s: AsyncSession, tg_id: int, username: str | None, first_name: str | None) -> User:
    return (await register_user(s, tg_id, username, first_name))[0]


async def attach_referrer(s: AsyncSession, user: User, code: str) -> User | None:
    """Привязывает нового пользователя к пригласившему. Вызывать только сразу после регистрации."""
    if not rt.get_bool("ref_enabled") or user.referrer_id:
        return None
    inviter = await s.scalar(select(User).where(User.ref_code == code))
    if inviter is None or inviter.id == user.id or inviter.is_banned:
        return None
    user.referrer_id = inviter.id
    await s.flush()
    return inviter


async def referral_stats(s: AsyncSession, user: User) -> dict:
    invited = await s.scalar(select(func.count(User.id)).where(User.referrer_id == user.id))
    rewards = await s.execute(
        select(func.count(ReferralReward.id), func.coalesce(func.sum(ReferralReward.days), 0)).where(
            ReferralReward.inviter_id == user.id
        )
    )
    paid, days = rewards.one()
    return {"invited": invited or 0, "paid": paid or 0, "days": int(days or 0), "pending": user.bonus_days}


async def active_plans(s: AsyncSession) -> list[Plan]:
    return list(
        await s.scalars(
            select(Plan).where(Plan.active.is_(True), Plan.deleted.is_(False)).order_by(Plan.sort, Plan.id)
        )
    )


MIN_PRICE_KOP = 100  # ЮKassa не принимает платежи меньше 1 ₽


def _percent_off(kop: int, percent: int) -> int:
    return int((Decimal(kop) * (100 - percent) / 100).to_integral_value(rounding=ROUND_HALF_UP))


async def has_paid_order(s: AsyncSession, user: User) -> bool:
    return bool(await s.scalar(select(func.count(Order.id)).where(Order.user_id == user.id, Order.status == "paid")))


async def user_prices(s: AsyncSession, user: User, plans: list[Plan]) -> dict[int, tuple[int, int, bool]]:
    """{plan_id: (цена_к_оплате, обычная_цена, скидка_за_приглашение)} для конкретного пользователя.

    Из всех применимых скидок берётся самая выгодная: персональная цена тарифа, общая скидка
    пользователя в %, скидка приглашённому на первую покупку. Цена никогда не выше обычной и не ниже 1 ₽."""
    overrides = dict(
        (await s.execute(select(UserPrice.plan_id, UserPrice.price_kop).where(UserPrice.user_id == user.id))).all()
    )
    ref_pct = 0
    if rt.get_bool("ref_enabled") and user.referrer_id and rt.get_int("ref_invitee_discount") > 0:
        in_window = True
        window = rt.get_int("ref_window_days")
        if window and utcnow() - user.created_at > timedelta(days=window):
            in_window = False
        pending_disc = await s.scalar(
            select(func.count(Order.id)).where(
                Order.user_id == user.id, Order.status == "pending", Order.ref_discount.is_(True),
                Order.created_at > utcnow() - timedelta(days=1),
            )
        )
        if in_window and not pending_disc and not await has_paid_order(s, user):
            ref_pct = rt.get_int("ref_invitee_discount")
    out = {}
    for p in plans:
        best, via_ref = p.price_kop, False
        if p.id in overrides:
            best = min(best, overrides[p.id])
        if user.discount_percent:
            best = min(best, _percent_off(p.price_kop, user.discount_percent))
        if ref_pct:
            cand = _percent_off(p.price_kop, ref_pct)
            if cand < best:
                best, via_ref = cand, True
        final = min(p.price_kop, max(best, MIN_PRICE_KOP))
        out[p.id] = (final, p.price_kop, via_ref and final < p.price_kop)
    return out


def trial_available(user: User) -> bool:
    return rt.get_int("trial_days") > 0 and not user.trial_used


def take_bonus(user: User) -> int:
    """Забирает накопленные бонусные дни пользователя (чтобы добавить их к новому/продляемому ключу)."""
    days, user.bonus_days = max(user.bonus_days or 0, 0), 0
    return days


# ======================================================================
# Ключи: отправка на серверы
# ======================================================================
async def push_key(
    s: AsyncSession, key: Key, servers: list[Server], *, expires: datetime, traffic_gb: int,
    limit_ip: int, reset: bool,
) -> int:
    """Создаёт/обновляет клиента ключа на указанных серверах. Возвращает число успешных.
    Бросает XUIError, если не получилось ни на одном — тогда выдавать нечего."""
    if not servers:
        raise XUIError("нет активных VPN-серверов — добавьте сервер в админ-панели")
    present = set(await s.scalars(select(KeyServer.server_id).where(KeyServer.key_id == key.id)))
    kw = dict(
        client_id=key.client_uuid, email=key.email, total_bytes=gb(traffic_gb), expiry_ms=to_ms(expires),
        sub_id=key.sub_id, limit_ip=limit_ip,
    )

    async def one(srv: Server) -> int:
        x = get_xui(srv)
        if srv.id in present:
            try:
                await x.update_client(**kw)
            except XUIError:  # клиента могли удалить руками — создаём заново
                await x.add_client(**kw)
            if reset:
                try:
                    await x.reset_traffic(key.email)
                except XUIError as e:
                    log.warning("reset_traffic failed on %s: %s", srv.name, e)
        else:
            try:
                await x.add_client(**kw)
            except XUIError:  # уже существует (например, после переноса) — обновляем
                await x.update_client(**kw)
        return srv.id

    results = await asyncio.gather(*(one(sv) for sv in servers), return_exceptions=True)
    ok = 0
    for srv, res in zip(servers, results):
        if isinstance(res, BaseException):
            log.warning("сервер «%s»: не удалось обновить ключ %s: %s", srv.name, key.email, res)
            continue
        ok += 1
        if srv.id not in present:
            s.add(KeyServer(key_id=key.id, server_id=srv.id))
    if ok == 0:
        raise XUIError("ни один VPN-сервер не ответил")
    await s.flush()
    return ok


async def issue_key(
    s: AsyncSession, user: User, *, days: int, traffic_gb: int, limit_ip: int = 0,
    plan: Plan | None = None, is_trial: bool = False, extra_days: int = 0,
) -> Key:
    servers = await active_servers(s)
    if not is_trial:
        extra_days += take_bonus(user)
    expires = utcnow() + timedelta(days=days + extra_days)
    key = Key(
        user_id=user.id, plan_id=plan.id if plan else None, email=f"tg{user.tg_id}-{secrets.token_hex(3)}",
        client_uuid=str(uuid.uuid4()), sub_id=secrets.token_hex(8), expires_at=expires,
        traffic_gb=traffic_gb, limit_ip=limit_ip, is_trial=is_trial,
    )
    key.user, key.plan = user, plan  # связи заданы явно — ими можно пользоваться после закрытия сессии
    s.add(key)
    await s.flush()
    await push_key(s, key, servers, expires=expires, traffic_gb=traffic_gb, limit_ip=limit_ip, reset=False)
    return key


async def renew_key(s: AsyncSession, key: Key, plan: Plan, extra_days: int = 0) -> Key:
    servers = await active_servers(s)
    extra_days += take_bonus(key.user)
    new_exp = max(utcnow(), key.expires_at) + timedelta(days=plan.days + extra_days)
    await push_key(
        s, key, servers, expires=new_exp, traffic_gb=plan.traffic_gb, limit_ip=plan.limit_ip, reset=True
    )
    key.expires_at, key.plan, key.plan_id = new_exp, plan, plan.id
    key.traffic_gb, key.limit_ip = plan.traffic_gb, plan.limit_ip
    key.is_trial, key.active, key.notice_stage = False, True, 0
    await s.flush()
    return key


async def extend_key(s: AsyncSession, key: Key, days: int) -> Key:
    servers = await active_servers(s)
    new_exp = max(utcnow(), key.expires_at) + timedelta(days=days)
    await push_key(
        s, key, servers, expires=new_exp, traffic_gb=key.traffic_gb, limit_ip=key.limit_ip, reset=False
    )
    key.expires_at, key.active, key.notice_stage = new_exp, True, 0
    await s.flush()
    return key


async def revoke_key(s: AsyncSession, key: Key) -> None:
    rows = (
        await s.execute(
            select(Server).join(KeyServer, KeyServer.server_id == Server.id).where(KeyServer.key_id == key.id)
        )
    ).scalars().all()

    async def one(srv: Server):
        await get_xui(srv).delete_client(key.client_uuid, key.email)

    results = await asyncio.gather(*(one(sv) for sv in rows), return_exceptions=True)
    failed = []
    for srv, res in zip(rows, results):
        if isinstance(res, BaseException):
            log.warning("delete_client failed on %s: %s", srv.name, res)
            failed.append(srv.name)
        else:
            await s.execute(
                KeyServer.__table__.delete().where(KeyServer.key_id == key.id, KeyServer.server_id == srv.id)
            )
    if failed:
        await s.flush()
        raise ServiceError("Не удалось удалить ключ на серверах: " + ", ".join(failed) + ". Повторите позже.")
    key.active = False
    await s.flush()


async def grant_key(s: AsyncSession, user: User, plan: Plan) -> Key:
    """Выдать ключ по тарифу бесплатно (из админки)."""
    try:
        return await issue_key(
            s, user, days=plan.days, traffic_gb=plan.traffic_gb, limit_ip=plan.limit_ip, plan=plan
        )
    except XUIError as e:
        log.error("grant_key failed: %s", e)
        raise ServiceError(f"VPN-серверы недоступны, ключ не выдан ({e})") from e


async def start_trial(s: AsyncSession, user: User) -> Key:
    if not trial_available(user):
        raise ServiceError("Пробный период недоступен")
    try:
        key = await issue_key(
            s, user, days=rt.get_int("trial_days"), traffic_gb=rt.get_int("trial_gb"), is_trial=True
        )
    except XUIError as e:
        log.error("trial issue failed: %s", e)
        raise ServiceError("Сервис временно недоступен, попробуйте позже") from e
    user.trial_used = True
    await s.commit()
    return key


# ======================================================================
# Подписка и ссылки
# ======================================================================
def sub_url(key: Key) -> str:
    return f"{get_settings().public_url}/sub/{key.sub_id}"


async def key_servers(s: AsyncSession, key: Key, only_active: bool = True) -> list[Server]:
    stmt = (
        select(Server).join(KeyServer, KeyServer.server_id == Server.id).where(KeyServer.key_id == key.id)
        .order_by(Server.sort, Server.id)
    )
    if only_active:
        stmt = stmt.where(Server.active.is_(True))
    return list(await s.scalars(stmt))


async def key_links(s: AsyncSession, key: Key) -> list[tuple[Server, str]]:
    """vless://-ссылки ключа — по одной на каждый доступный сервер."""
    servers = await key_servers(s, key)

    async def one(srv: Server) -> str | None:
        inbound = await asyncio.wait_for(get_xui(srv).get_inbound(), timeout=8)
        if inbound.get("protocol") != "vless":
            log.warning("сервер «%s»: inbound не VLESS — в подписку не включён", srv.name)
            return None
        host = srv.public_host.strip() or urlparse(srv.url).hostname or ""
        return build_vless_link(
            inbound, key.client_uuid, key.email, host, srv.flow, remark=f"{srv.flag} {srv.name}"
        )

    results = await asyncio.gather(*(one(sv) for sv in servers), return_exceptions=True)
    out = []
    for srv, res in zip(servers, results):
        if isinstance(res, BaseException):
            log.warning("сервер «%s» пропущен в подписке: %s", srv.name, res)
        elif res:
            out.append((srv, res))
    return out


async def bulk_traffic(s: AsyncSession, keys: list[Key]) -> dict[int, tuple[int, int] | None]:
    """{key_id: (использовано_байт, лимит_байт)} — сумма по всем серверам; None, если никто не ответил."""
    if not keys:
        return {}
    by_key = {k.id: k for k in keys}
    rows = (
        await s.execute(
            select(KeyServer.key_id, Server)
            .join(Server, Server.id == KeyServer.server_id)
            .where(KeyServer.key_id.in_(list(by_key)), Server.active.is_(True))
        )
    ).all()

    async def one(kid: int, srv: Server) -> tuple[int, int]:
        t = await asyncio.wait_for(get_xui(srv).get_traffic(by_key[kid].email), timeout=6)
        return kid, (int(t.get("up", 0)) + int(t.get("down", 0))) if t else 0

    results = await asyncio.gather(*(one(kid, sv) for kid, sv in rows), return_exceptions=True)
    used: dict[int, int] = defaultdict(int)
    answered: dict[int, int] = defaultdict(int)
    for r in results:
        if isinstance(r, BaseException):
            continue
        kid, u = r
        used[kid] += u
        answered[kid] += 1
    return {k.id: ((used[k.id], gb(k.traffic_gb)) if answered[k.id] else None) for k in keys}


async def safe_traffic(s: AsyncSession, key: Key) -> tuple[int, int] | None:
    return (await bulk_traffic(s, [key])).get(key.id)


def qr_svg(text: str) -> str:
    return segno.make(text, error="m").svg_inline(scale=5, border=2, dark="#14161b", light="#ffffff")


def qr_png(text: str) -> bytes:
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=3)
    return buf.getvalue()


# ======================================================================
# Заказы и оплата
# ======================================================================
async def create_order(
    s: AsyncSession, user: User, plan: Plan, renew_key_id: int | None = None
) -> tuple[Order, str]:
    cfg = get_settings()
    if user.is_banned:
        raise ServiceError("Аккаунт заблокирован")
    if not plan.active or plan.deleted:
        raise ServiceError("Тариф недоступен")
    if not payments.configured():
        raise ServiceError("Оплата временно недоступна")
    if renew_key_id:
        target = await s.get(Key, renew_key_id)
        if not target or target.user_id != user.id or not target.active:
            renew_key_id = None
    price, base, via_ref = (await user_prices(s, user, [plan]))[plan.id]
    order = Order(
        user_id=user.id, plan_id=plan.id, renew_key_id=renew_key_id, amount_kop=price, base_kop=base,
        ref_discount=via_ref,
    )
    s.add(order)
    await s.commit()  # номер заказа нужен ЮKassa; транзакцию не держим на время сетевого запроса
    try:
        payment_id, url = await payments.create_payment(
            order_id=order.id, amount_kop=price, description=f"{cfg.brand_name}: {plan.name}",
            return_url=f"{cfg.public_url}/payment/return?order={order.id}",
        )
    except payments.PaymentError as e:
        order.status = "canceled"
        await s.commit()
        log.error("не удалось создать платёж для заказа %s: %s", order.id, e)
        raise ServiceError("Не удалось создать платёж. Попробуйте чуть позже.") from e
    order.payment_id = payment_id
    await s.commit()
    return order, url


@dataclass
class FulfillResult:
    order: Order
    key: Key | None
    user: User
    renewed: bool
    newly_paid: bool
    invitee_bonus: int = 0  # дней, добавленных этому пользователю как приглашённому
    inviter: User | None = None
    reward_days: int = 0  # дней, начисленных пригласившему (вместе с бонусом за веху)
    milestone_days: int = 0  # из них — бонус за «веху» (каждый N-й друг)
    reward_applied: bool = False  # уже добавлены к ключу пригласившего


def inviter_reward_days(inviter: User, plan: Plan) -> int:
    """Сколько дней получает пригласивший за этот заказ (до применения ограничений)."""
    if inviter.ref_reward_days is not None:  # личные условия имеют приоритет
        return max(inviter.ref_reward_days, 0)
    if rt.get("ref_inviter_mode") == "percent":
        pct = rt.get_int("ref_inviter_percent")
        return max(1, round(plan.days * pct / 100)) if pct > 0 else 0
    return rt.get_int("ref_inviter_days")


async def _calc_reward(
    s: AsyncSession, inviter: User, user: User, order: Order, plan: Plan, first_paid: bool
) -> tuple[int, int]:
    """(всего_дней, из_них_за_веху) награды пригласившему за оплаченный заказ друга."""
    if inviter.is_banned or not (first_paid or not rt.get_bool("ref_first_only")):
        return 0, 0
    min_amount = rt.get_int("ref_min_amount")
    if min_amount and order.amount_kop < min_amount * 100:
        return 0, 0
    given, given_days = (
        await s.execute(
            select(func.count(ReferralReward.id), func.coalesce(func.sum(ReferralReward.days), 0)).where(
                ReferralReward.inviter_id == inviter.id
            )
        )
    ).one()
    max_rewards, max_days = rt.get_int("ref_max_rewards"), rt.get_int("ref_max_days")
    if max_rewards and given >= max_rewards:
        return 0, 0

    base = inviter_reward_days(inviter, plan)
    milestone = 0
    every, m_days = rt.get_int("ref_milestone_every"), rt.get_int("ref_milestone_days")
    if every > 0 and m_days > 0:
        known = await s.scalar(
            select(func.count(ReferralReward.id)).where(
                ReferralReward.inviter_id == inviter.id, ReferralReward.invitee_id == user.id
            )
        )
        if not known:  # считаем друзей, а не оплаты
            friends = await s.scalar(
                select(func.count(func.distinct(ReferralReward.invitee_id))).where(
                    ReferralReward.inviter_id == inviter.id
                )
            )
            if (friends + 1) % every == 0:
                milestone = m_days
    total = base + milestone
    if max_days:
        total = min(total, max(max_days - int(given_days), 0))
        milestone = min(milestone, total)
    return total, milestone


async def fulfill_order(s: AsyncSession, order_id: int) -> FulfillResult:
    """Идемпотентно: оплачивает заказ, выдаёт/продлевает ключ, начисляет реферальные бонусы.

    Если серверы недоступны — бросает исключение ДО пометки «оплачен», поэтому повторное
    уведомление ЮKassa, сверка платежей или кнопка в админке доделают работу.
    """
    async with _fulfil_lock:
        order = await s.get(Order, order_id, populate_existing=True)
        if order is None:
            raise ServiceError("Заказ не найден")
        user = await s.get(User, order.user_id)
        if order.status == "paid":
            key = await s.get(Key, order.key_id) if order.key_id else None
            return FulfillResult(order, key, user, bool(order.renew_key_id), False)

        plan = await s.get(Plan, order.plan_id)
        target = await s.get(Key, order.renew_key_id) if order.renew_key_id else None

        first_paid = not await has_paid_order(s, user)
        inviter = None
        if rt.get_bool("ref_enabled") and user.referrer_id:
            inviter = await s.get(User, user.referrer_id)
            window = rt.get_int("ref_window_days")
            if window and inviter and (utcnow() - user.created_at) > timedelta(days=window):
                inviter = None  # срок действия приглашения истёк
        invitee_bonus = rt.get_int("ref_invitee_days") if (inviter and first_paid) else 0

        if target and target.active and target.user_id == user.id:
            key, renewed = await renew_key(s, target, plan, extra_days=invitee_bonus), True
        else:
            key = await issue_key(
                s, user, days=plan.days, traffic_gb=plan.traffic_gb, limit_ip=plan.limit_ip, plan=plan,
                extra_days=invitee_bonus,
            )
            renewed = False
        order.status, order.paid_at, order.key_id = "paid", utcnow(), key.id

        reward_days = milestone = 0
        if inviter:
            reward_days, milestone = await _calc_reward(s, inviter, user, order, plan, first_paid)
            if reward_days > 0:
                s.add(ReferralReward(inviter_id=inviter.id, invitee_id=user.id, order_id=order.id, days=reward_days))
                inviter.bonus_days = (inviter.bonus_days or 0) + reward_days
        await s.commit()

        applied = False
        if reward_days > 0:
            try:
                applied = bool(await apply_bonus(s, inviter))
            except Exception:  # noqa: BLE001 — платёж уже проведён, бонус дождётся фоновой задачи
                log.exception("не удалось сразу применить бонус пригласившему %s", inviter.id)
        return FulfillResult(
            order, key, user, renewed, True, invitee_bonus,
            inviter if reward_days > 0 else None, reward_days, milestone, applied,
        )


# ---------- сверка платежей ЮKassa ----------
_paid_notifier = None


def set_paid_notifier(fn) -> None:
    """Функция, сообщающая в Telegram об оплате (устанавливается при старте приложения)."""
    global _paid_notifier
    _paid_notifier = fn


async def settle_payment(payment_id: str) -> str:
    """Проверяет платёж в ЮKassa и, если он успешен, выдаёт ключ. Безопасно вызывать сколько угодно раз.

    Возвращает: paid | canceled | pending | unknown | mismatch.
    Исключения (XUIError, ServiceError, PaymentError) означают «временный сбой» — вызывающий должен
    попросить ЮKassa повторить уведомление (HTTP 5xx)."""
    payment = await payments.get_payment(payment_id)  # источник истины — API ЮKassa, а не входящий запрос
    try:
        order_id = int((payment.get("metadata") or {}).get("order_id", 0))
    except (TypeError, ValueError):
        order_id = 0
    async with db.Session() as s:
        order = await s.get(Order, order_id) if order_id else None
        if order is None or (order.payment_id and order.payment_id != payment_id):
            log.warning("платёж %s не связан с известным заказом (metadata=%s)", payment_id, payment.get("metadata"))
            return "unknown"
        status = payment.get("status")
        if status == "succeeded" and payment.get("paid"):
            if not payments.amount_matches(payment, order.amount_kop):
                log.error("платёж %s: сумма %s не совпадает с заказом №%s (%s коп.)",
                          payment_id, payment.get("amount"), order.id, order.amount_kop)
                return "mismatch"
            res = await fulfill_order(s, order.id)
            if res.newly_paid and _paid_notifier is not None:
                try:
                    await _paid_notifier(res)
                except Exception:  # noqa: BLE001 — уведомление не должно ломать выдачу
                    log.exception("не удалось отправить уведомление об оплате заказа %s", order.id)
            return "paid"
        if status == "canceled":
            if order.status == "pending":
                order.status = "canceled"
                await s.commit()
            return "canceled"
        return "pending"


async def reconcile_pending_orders() -> None:
    """Страховка на случай потерянных уведомлений: проверяем свежие неоплаченные заказы."""
    if not payments.configured():
        return
    async with db.Session() as s:
        ids = list(
            await s.scalars(
                select(Order.payment_id).where(
                    Order.status == "pending", Order.payment_id.is_not(None),
                    Order.created_at > utcnow() - timedelta(days=3),
                ).order_by(Order.id.desc()).limit(50)
            )
        )
    for pid in ids:
        try:
            await settle_payment(pid)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("сверка платежа %s: %s", pid, e)


async def apply_bonus(s: AsyncSession, user: User) -> int:
    """Добавляет накопленные бонусные дни к самому свежему ключу пользователя. Возвращает число дней."""
    if (user.bonus_days or 0) <= 0:
        return 0
    key = await s.scalar(
        select(Key).where(Key.user_id == user.id, Key.active.is_(True)).order_by(Key.expires_at.desc()).limit(1)
    )
    if key is None:
        return 0  # ключа пока нет — бонус применится к первой покупке
    days = user.bonus_days
    await extend_key(s, key, days)
    user.bonus_days = 0
    await s.commit()
    return days


# ======================================================================
# Фоновая синхронизация
# ======================================================================
async def sync_missing_keys(only_server_id: int | None = None) -> tuple[int, int]:
    """Доводит действующие ключи до всех активных серверов (новый сервер, сбой при выдаче и т. п.).
    Возвращает (синхронизировано, не удалось)."""
    done = failed = 0
    async with db.Session() as s:
        servers = await active_servers(s)
        if only_server_id is not None:
            servers = [sv for sv in servers if sv.id == only_server_id]
        if not servers:
            return 0, 0
        present = set(
            (kid, sid) for kid, sid in (await s.execute(select(KeyServer.key_id, KeyServer.server_id))).all()
        )
        keys = list(await s.scalars(select(Key).where(Key.active.is_(True), Key.expires_at > utcnow())))
        for key in keys:
            missing = [sv for sv in servers if (key.id, sv.id) not in present]
            if not missing:
                continue
            try:
                await push_key(
                    s, key, missing, expires=key.expires_at, traffic_gb=key.traffic_gb,
                    limit_ip=key.limit_ip, reset=False,
                )
                await s.commit()
                done += 1
            except XUIError as e:
                await s.rollback()
                failed += 1
                log.warning("sync key %s: %s", key.email, e)
    return done, failed


async def apply_pending_bonuses() -> None:
    async with db.Session() as s:
        users = list(await s.scalars(select(User).where(User.bonus_days > 0)))
        for u in users:
            try:
                await apply_bonus(s, u)
            except Exception:  # noqa: BLE001
                await s.rollback()
                log.warning("apply_bonus failed for user %s", u.id, exc_info=True)


async def sync_loop(interval: int = 300) -> None:
    while True:
        try:
            await sync_missing_keys()
            await apply_pending_bonuses()
            await reconcile_pending_orders()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("sync loop failed")
        await asyncio.sleep(interval)


def spawn_sync(only_server_id: int | None = None) -> None:
    _spawn(sync_missing_keys(only_server_id))
