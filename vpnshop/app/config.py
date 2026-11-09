from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- общее ---
    brand_name: str = "Milk VPN"
    base_url: str = "http://localhost:8000"  # публичный https-адрес сервиса
    secret_key: str = ""
    timezone: str = "Europe/Moscow"
    database_url: str = "sqlite+aiosqlite:///./data/vpnshop.db"
    contact_email: str = ""  # показывается на страницах «Условия» и «Конфиденциальность»

    # --- Telegram ---
    bot_token: str = ""
    run_bot: bool = True
    admin_tg_ids: str = ""  # через запятую
    support_username: str = ""  # без @

    # --- админ-панель ---
    admin_login: str = "admin"
    admin_password: str = ""

    # --- первый VPN-сервер (необязательно) ---
    # Если в админке ещё нет серверов, а эти поля заполнены — сервер создастся автоматически.
    # Остальные серверы добавляйте в админке: раздел «Серверы».
    xui_url: str = ""
    xui_username: str = ""
    xui_password: str = ""
    xui_inbound_id: int = 1
    xui_flow: str = ""
    xui_verify_tls: bool = True
    xui_public_host: str = ""
    xui_country: str = "NL"
    xui_name: str = "Основной"

    # --- ЮKassa ---
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_receipt: bool = False  # формировать чек 54-ФЗ (нужно, если в ЮKassa включена «Чеки от ЮKassa»)
    yookassa_tax_system: int = 0  # СНО: 1 ОСН, 2 УСН доходы, 3 УСН доходы-расходы, 4 ЕНВД, 5 ЕСХН, 6 патент; 0 — по умолчанию магазина
    yookassa_vat_code: int = 1  # 1 — без НДС, 2 — 0%, 3 — 10%, 4 — 20%, ...
    yookassa_receipt_email: str = ""  # e-mail для чеков (если пусто — CONTACT_EMAIL)

    # --- значения по умолчанию для пробного периода (дальше меняются в админке) ---
    trial_days: int = 1
    trial_gb: int = 1

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_tg_ids.replace(" ", "").split(",") if x.strip().isdigit()}

    @property
    def yookassa_configured(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def receipt_email(self) -> str:
        return self.yookassa_receipt_email or self.contact_email

    @property
    def public_url(self) -> str:
        return self.base_url.rstrip("/")

    def problems(self) -> list[str]:
        """Критичные ошибки конфигурации — с ними запускаться нельзя."""
        out = []
        if len(self.secret_key) < 16 or self.secret_key == "change-me":
            out.append("SECRET_KEY должен быть случайной строкой от 16 символов")
        if len(self.admin_password) < 8:
            out.append("ADMIN_PASSWORD должен быть не короче 8 символов")
        if bool(self.yookassa_shop_id) != bool(self.yookassa_secret_key):
            out.append("YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY нужно задавать вместе")
        if self.yookassa_receipt and not self.receipt_email:
            out.append("для чеков (YOOKASSA_RECEIPT=true) укажите YOOKASSA_RECEIPT_EMAIL или CONTACT_EMAIL")
        if not 0 <= self.yookassa_tax_system <= 6:
            out.append("YOOKASSA_TAX_SYSTEM: число от 0 до 6")
        if not 1 <= self.yookassa_vat_code <= 12:
            out.append("YOOKASSA_VAT_CODE: число от 1 до 12")
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
