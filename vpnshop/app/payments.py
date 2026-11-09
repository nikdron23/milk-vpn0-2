"""Интеграция с ЮKassa (API v3): создание платежа и получение его состояния.

Схема оплаты:
  1. Создаём платёж (POST /v3/payments, confirmation=redirect) и отправляем пользователя по confirmation_url.
  2. ЮKassa присылает уведомление на /payment/yookassa; пользователь возвращается на /payment/return.
  3. В обоих случаях состояние платежа ПЕРЕПРОВЕРЯЕТСЯ запросом GET /v3/payments/{id} с нашими ключами —
     содержимому уведомления и адресной строки не доверяем (у уведомлений нет подписи).
"""
import logging
import uuid
from decimal import Decimal

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

API = "https://api.yookassa.ru/v3"
_transport: httpx.AsyncBaseTransport | None = None  # подмена в тестах


class PaymentError(Exception):
    """Не удалось обратиться к ЮKassa (сеть, ключи, отказ)."""


class PaymentNotFound(PaymentError):
    """ЮKassa не знает такого платежа."""


def configured() -> bool:
    return get_settings().yookassa_configured


def fmt_sum(amount_kop: int) -> str:
    return f"{amount_kop // 100}.{amount_kop % 100:02d}"


def _client() -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        auth=(s.yookassa_shop_id, s.yookassa_secret_key), timeout=httpx.Timeout(20.0), transport=_transport
    )


def build_receipt(description: str, amount_kop: int) -> dict:
    s = get_settings()
    receipt = {
        "customer": {"email": s.receipt_email},
        "items": [
            {
                "description": description[:128],
                "quantity": "1.00",
                "amount": {"value": fmt_sum(amount_kop), "currency": "RUB"},
                "vat_code": s.yookassa_vat_code,
                "payment_mode": "full_payment",
                "payment_subject": "service",
            }
        ],
    }
    if s.yookassa_tax_system:
        receipt["tax_system_code"] = s.yookassa_tax_system
    return receipt


def build_payment_body(*, order_id: int, amount_kop: int, description: str, return_url: str) -> dict:
    description = description[:128]
    body = {
        "amount": {"value": fmt_sum(amount_kop), "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description,
        "metadata": {"order_id": str(order_id)},
    }
    if get_settings().yookassa_receipt:
        body["receipt"] = build_receipt(description, amount_kop)
    return body


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
        return f"{data.get('code', r.status_code)}: {data.get('description', '')}".strip(": ")
    except ValueError:
        return f"HTTP {r.status_code}"


async def create_payment(
    *, order_id: int, amount_kop: int, description: str, return_url: str
) -> tuple[str, str]:
    """Возвращает (id платежа, ссылка на оплату)."""
    body = build_payment_body(
        order_id=order_id, amount_kop=amount_kop, description=description, return_url=return_url
    )
    try:
        async with _client() as c:
            r = await c.post(f"{API}/payments", json=body, headers={"Idempotence-Key": str(uuid.uuid4())})
    except httpx.HTTPError as e:
        raise PaymentError(f"ЮKassa недоступна: {e}") from e
    if r.status_code != 200:
        raise PaymentError(f"ЮKassa отклонила платёж ({_error_text(r)})")
    data = r.json()
    url = (data.get("confirmation") or {}).get("confirmation_url")
    if not data.get("id") or not url:
        raise PaymentError("ЮKassa вернула неожиданный ответ")
    return data["id"], url


async def get_payment(payment_id: str) -> dict:
    try:
        async with _client() as c:
            r = await c.get(f"{API}/payments/{payment_id}")
    except httpx.HTTPError as e:
        raise PaymentError(f"ЮKassa недоступна: {e}") from e
    if r.status_code == 404:
        raise PaymentNotFound(payment_id)
    if r.status_code != 200:
        raise PaymentError(f"ЮKassa: {_error_text(r)}")
    return r.json()


def amount_matches(payment: dict, amount_kop: int) -> bool:
    amount = payment.get("amount") or {}
    try:
        return amount.get("currency") == "RUB" and Decimal(str(amount.get("value"))) == Decimal(amount_kop) / 100
    except ArithmeticError:
        return False
