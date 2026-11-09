"""Клиент API панели 3x-ui (MHSanaei). Работает с клиентами VLESS внутри одного inbound.

Поддерживаются два поколения API — выбор делается автоматически при первом запросе:
  * «v3» (3x-ui 3.x): клиенты управляются через /panel/api/clients/* и идентифицируются по email.
    Начиная с v3.9.0 старые методы изменения клиентов через inbound больше не работают.
  * «legacy» (2.x): /panel/api/inbounds/addClient, updateClient/{uuid}, {id}/delClient/{uuid}.

Авторизация: либо API-токен (заголовок Authorization: Bearer …, рекомендуется для 3.x),
либо логин/пароль (cookie-сессия).
"""
import asyncio
import json
import logging
import time
from urllib.parse import quote, urlencode

import httpx

log = logging.getLogger(__name__)


class XUIError(Exception):
    pass


class XUIConnectionError(XUIError):
    """Панель недоступна (сеть, TLS, таймаут) или не удалось войти."""


class XUIAuthError(XUIError):
    """Неверный логин/пароль или API-токен."""


class XUIApiError(XUIError):
    """Панель ответила, но отклонила операцию ({"success": false, "msg": ...})."""


class XUIClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        inbound_id: int,
        flow: str = "",
        verify_tls: bool = True,
        token: str = "",
    ):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = (token or "").strip()
        self.inbound_id = inbound_id
        self.flow = flow
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(20.0), verify=verify_tls)
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._mode: str | None = None  # "v3" | "legacy"
        self._inbound_cache: tuple[float, dict | None] = (0.0, None)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- транспорт ----------
    async def _login(self) -> None:
        if not self.username or not self.password:
            raise XUIAuthError("не задан ни API-токен, ни логин с паролем")
        try:
            r = await self._http.post(
                f"{self.base}/login", data={"username": self.username, "password": self.password}
            )
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise XUIConnectionError(f"login failed: {e}") from e
        if not data.get("success"):
            raise XUIAuthError(f"login rejected: {data.get('msg')}")
        self._logged_in = True

    async def _request(self, method: str, path: str, **kw):
        last = ""
        for _ in range(1 if self.token else 2):
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            elif not self._logged_in:
                async with self._lock:
                    if not self._logged_in:
                        await self._login()
            try:
                r = await self._http.request(method, f"{self.base}{path}", headers=headers, **kw)
            except httpx.HTTPError as e:
                raise XUIConnectionError(f"{method} {path}: {e}") from e
            if self.token and r.status_code in (401, 403):
                raise XUIAuthError("API-токен отклонён панелью (проверьте токен и его права)")
            is_json = "json" in r.headers.get("content-type", "")
            # Без валидной сессии 3x-ui отвечает 404/не-JSON — логинимся заново
            if r.status_code in (401, 403, 404) or not is_json:
                last = f"HTTP {r.status_code}"
                if not self.token:
                    self._logged_in = False
                    continue
                raise XUIError(f"{method} {path}: {last}")
            data = r.json()
            if not data.get("success"):
                raise XUIApiError(data.get("msg") or "unknown error")
            return data.get("obj")
        raise XUIError(f"{method} {path}: not authorized ({last})")

    async def _api(self) -> str:
        """Определяет поколение API панели (кэшируется)."""
        if self._mode:
            return self._mode
        try:
            await self._request("GET", "/panel/api/clients/list")
            self._mode = "v3"
        except (XUIConnectionError, XUIAuthError):
            raise
        except XUIError:
            self._mode = "legacy"
        log.debug("3x-ui %s: API %s", self.base, self._mode)
        return self._mode

    async def _call(self, v3, legacy):
        """Выполняет операцию в нужном поколении API. Неожиданный сбой сбрасывает определённый режим —
        на случай, если панель обновили между запросами."""
        mode = await self._api()
        try:
            return await (v3() if mode == "v3" else legacy())
        except XUIApiError:
            raise
        except XUIError:
            self._mode = None
            raise

    # ---------- клиенты ----------
    def _client(self, *, client_id, email, total_bytes, expiry_ms, sub_id, limit_ip, enable) -> dict:
        return {
            "id": client_id,
            "flow": self.flow,
            "email": email,
            "limitIp": limit_ip,
            "totalGB": total_bytes,
            "expiryTime": expiry_ms,
            "enable": enable,
            "subId": sub_id,
            "comment": "",
        }

    async def add_client(
        self, *, client_id, email, total_bytes, expiry_ms, sub_id, limit_ip=0, enable=True
    ) -> None:
        c = self._client(
            client_id=client_id, email=email, total_bytes=total_bytes, expiry_ms=expiry_ms,
            sub_id=sub_id, limit_ip=limit_ip, enable=enable,
        )
        await self._call(
            lambda: self._request(
                "POST", "/panel/api/clients/add", json={"client": c, "inboundIds": [self.inbound_id]}
            ),
            lambda: self._request(
                "POST", "/panel/api/inbounds/addClient",
                json={"id": self.inbound_id, "settings": json.dumps({"clients": [c]})},
            ),
        )

    async def update_client(
        self, *, client_id, email, total_bytes, expiry_ms, sub_id, limit_ip=0, enable=True
    ) -> None:
        c = self._client(
            client_id=client_id, email=email, total_bytes=total_bytes, expiry_ms=expiry_ms,
            sub_id=sub_id, limit_ip=limit_ip, enable=enable,
        )
        await self._call(
            lambda: self._request("POST", f"/panel/api/clients/update/{quote(email, safe='')}", json=c),
            lambda: self._request(
                "POST", f"/panel/api/inbounds/updateClient/{client_id}",
                json={"id": self.inbound_id, "settings": json.dumps({"clients": [c]})},
            ),
        )

    async def delete_client(self, client_id: str, email: str = "") -> None:
        async def v3():
            if not email:
                raise XUIApiError("для удаления в API 3.x нужен email клиента")
            return await self._request("POST", f"/panel/api/clients/del/{quote(email, safe='')}")

        await self._call(
            v3,
            lambda: self._request("POST", f"/panel/api/inbounds/{self.inbound_id}/delClient/{client_id}"),
        )

    async def reset_traffic(self, email: str) -> None:
        await self._call(
            lambda: self._request("POST", f"/panel/api/clients/resetTraffic/{quote(email, safe='')}"),
            lambda: self._request(
                "POST", f"/panel/api/inbounds/{self.inbound_id}/resetClientTraffic/{quote(email, safe='')}"
            ),
        )

    async def get_traffic(self, email: str) -> dict | None:
        """{"up": байт, "down": байт} либо None, если клиента на панели нет."""
        q = quote(email, safe="")

        async def v3():
            try:
                return await self._request("GET", f"/panel/api/clients/traffic/{q}")
            except XUIApiError:
                raise
            except XUIError:  # на случай иной формы ответа — пробуем классический метод
                return await self._request("GET", f"/panel/api/inbounds/getClientTraffics/{q}")

        obj = await self._call(v3, lambda: self._request("GET", f"/panel/api/inbounds/getClientTraffics/{q}"))
        return normalize_traffic(obj)

    # ---------- inbound ----------
    async def get_inbound(self, force: bool = False) -> dict:
        ts, cached = self._inbound_cache
        if cached and not force and time.monotonic() - ts < 300:
            return cached
        try:
            obj = await self._request("GET", f"/panel/api/inbounds/get/{self.inbound_id}")
        except XUIError:
            if cached and not force:  # панель недоступна — отдаём устаревшие настройки, они меняются редко
                return cached
            raise
        if not obj:
            raise XUIError(f"inbound {self.inbound_id} not found")
        self._inbound_cache = (time.monotonic(), obj)
        return obj

    async def ping(self) -> str:
        ib = await self.get_inbound(force=True)
        mode = await self._api()
        return (
            f"{ib.get('protocol', '?')}:{ib.get('port', '?')} «{ib.get('remark', '')}» · "
            f"API {'3.x' if mode == 'v3' else '2.x'}, {'токен' if self.token else 'пароль'}"
        )


def normalize_traffic(obj) -> dict | None:
    """Приводит ответы разных версий панели к {"up", "down"}."""
    if not obj:
        return None
    if isinstance(obj, list):
        obj = obj[0] if obj else None
        if not obj:
            return None
    if not isinstance(obj, dict):
        return None
    if "traffic" in obj and isinstance(obj["traffic"], dict):
        obj = obj["traffic"]
    up = obj.get("up", obj.get("upload", 0)) or 0
    down = obj.get("down", obj.get("download", 0)) or 0
    return {"up": int(up), "down": int(down)}


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return json.loads(value)
    return {}


def build_vless_link(
    inbound: dict, client_id: str, email: str, host: str, flow: str = "", remark: str | None = None
) -> str:
    """Собирает vless:// ссылку из настроек inbound (tcp/ws/grpc + none/tls/reality)."""
    stream = _as_dict(inbound.get("streamSettings"))
    net = stream.get("network", "tcp")
    sec = stream.get("security", "none")
    params: dict[str, str] = {"type": net, "security": sec, "encryption": "none"}

    if net == "ws":
        ws = stream.get("wsSettings", {})
        params["path"] = ws.get("path", "/")
        h = (ws.get("headers") or {}).get("Host") or ws.get("host")
        if h:
            params["host"] = h
    elif net == "grpc":
        g = stream.get("grpcSettings", {})
        params["serviceName"] = g.get("serviceName", "")
        if g.get("multiMode"):
            params["mode"] = "multi"

    if sec == "reality":
        r = stream.get("realitySettings", {})
        rs = r.get("settings", {})
        params["pbk"] = rs.get("publicKey") or r.get("publicKey", "")
        params["fp"] = rs.get("fingerprint") or r.get("fingerprint", "chrome")
        if r.get("serverNames"):
            params["sni"] = r["serverNames"][0]
        if r.get("shortIds"):
            params["sid"] = r["shortIds"][0]
        spx = rs.get("spiderX") or r.get("spiderX")
        if spx:
            params["spx"] = spx
        pqv = rs.get("mldsa65Verify") or r.get("mldsa65Verify")  # постквантовая подпись (новые версии Xray)
        if pqv:
            params["pqv"] = pqv
    elif sec == "tls":
        t = stream.get("tlsSettings", {})
        params["sni"] = t.get("serverName") or host
        params["fp"] = (t.get("settings") or {}).get("fingerprint", "chrome")
        if t.get("alpn"):
            params["alpn"] = ",".join(t["alpn"])

    if flow and net == "tcp":
        params["flow"] = flow

    tag = quote(remark if remark else f"{inbound.get('remark') or 'VPN'}-{email}")
    return f"vless://{client_id}@{host}:{inbound['port']}?{urlencode(params, quote_via=quote)}#{tag}"
