"""Настройки, которые администратор меняет из панели (хранятся в БД, кэшируются в памяти)."""
from sqlalchemy import select

from app import db, fmt
from app.config import get_settings
from app.models import Setting

_cache: dict[str, str] = {}


def defaults() -> dict[str, str]:
    c = get_settings()
    return {
        "ref_enabled": "1",
        # --- награда пригласившему ---
        "ref_inviter_mode": "days",  # days — фиксированное число дней; percent — % от срока купленного тарифа
        "ref_inviter_days": "7",
        "ref_inviter_percent": "20",
        "ref_first_only": "1",  # награда только за первую оплату друга
        "ref_min_amount": "0",  # минимальная сумма заказа друга для награды, ₽ (0 — любая)
        # --- приглашённому ---
        "ref_invitee_days": "3",  # бонусные дни к первой покупке
        "ref_invitee_discount": "0",  # скидка на первую покупку, %
        # --- ограничения ---
        "ref_max_rewards": "0",  # максимум наград на одного пригласившего (0 — без лимита)
        "ref_max_days": "0",  # максимум бонусных дней суммарно на одного пригласившего (0 — без лимита)
        "ref_window_days": "0",  # друг должен оплатить не позже чем через N дней после регистрации (0 — без срока)
        # --- бонус за «веху» ---
        "ref_milestone_every": "0",  # каждый N-й оплативший друг (0 — выключено)
        "ref_milestone_days": "0",  # дополнительные дни за веху
        # --- пробный период ---
        "trial_days": str(c.trial_days),
        "trial_gb": str(c.trial_gb),
    }


async def load() -> None:
    async with db.Session() as s:
        rows = (await s.execute(select(Setting.key, Setting.value))).all()
    _cache.clear()
    _cache.update({k: v for k, v in rows})


def get(key: str) -> str:
    return _cache.get(key, defaults()[key])


def get_bool(key: str) -> bool:
    return get(key).strip().lower() in ("1", "true", "on", "yes")


def get_int(key: str) -> int:
    try:
        return int(get(key))
    except ValueError:
        return int(defaults()[key])


async def save(values: dict[str, str]) -> None:
    async with db.Session() as s:
        for k, v in values.items():
            row = await s.get(Setting, k)
            if row is None:
                s.add(Setting(key=k, value=v))
            else:
                row.value = v
        await s.commit()
    _cache.update(values)


def referral_lines() -> list[str]:
    """Условия реферальной программы человеческим языком (для сайта и бота)."""
    out: list[str] = []
    every = "за первую оплату каждого друга" if get_bool("ref_first_only") else "за каждую оплату друга"
    if get('ref_inviter_mode') == "percent" and get_int("ref_inviter_percent") > 0:
        out.append(
            f"Вы получаете {get_int('ref_inviter_percent')}% от срока купленного другом тарифа в днях {every}."
        )
    elif get_int("ref_inviter_days") > 0:
        out.append(f"Вы получаете +{fmt.plural_days(get_int('ref_inviter_days'))} {every}.")
    if get_int("ref_invitee_days") > 0:
        out.append(f"Друг получает +{fmt.plural_days(get_int('ref_invitee_days'))} к первой покупке.")
    if get_int("ref_invitee_discount") > 0:
        out.append(f"Друг получает скидку {get_int('ref_invitee_discount')}% на первую покупку.")
    if get_int("ref_milestone_every") > 0 and get_int("ref_milestone_days") > 0:
        out.append(
            f"За каждого {get_int('ref_milestone_every')}-го оплатившего друга — "
            f"дополнительно +{fmt.plural_days(get_int('ref_milestone_days'))}."
        )
    if get_int("ref_min_amount") > 0:
        out.append(f"Награда начисляется за заказы от {get_int('ref_min_amount')} ₽.")
    if get_int("ref_window_days") > 0:
        out.append(f"Друг должен оплатить в течение {fmt.plural_days(get_int('ref_window_days'))} после регистрации.")
    limits = []
    if get_int("ref_max_rewards") > 0:
        limits.append(f"не более {get_int('ref_max_rewards')} наград")
    if get_int("ref_max_days") > 0:
        limits.append(f"не более {fmt.plural_days(get_int('ref_max_days'))} суммарно")
    if limits:
        out.append("Ограничения: " + ", ".join(limits) + ".")
    return out
