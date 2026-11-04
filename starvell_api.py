"""
Небольшой асинхронный клиент Starvell.

У Starvell нет публичного API: используются те же запросы, что делает сайт.
Авторизация — cookie «session» из браузера (STARVELL_SESSION в .env).
"""

import asyncio
import json
import logging
import os
import time
from html.parser import HTMLParser
from pathlib import Path

import aiohttp
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

STARVELL_SESSION = os.getenv("STARVELL_SESSION", "").strip()
BASE_URL = "https://starvell.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

log = logging.getLogger("starvell")


class StarvellError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = "", data=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.data = data or {}


class _NextDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.inside = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.chunks.append(data)


def _error_text(text: str) -> tuple[str, str, dict]:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        if (text or "").lstrip().startswith("<"):
            return "HTML вместо ответа API (защита от ботов или техработы)", "", {}
        return " ".join((text or "").split())[:200], "", {}

    if not isinstance(parsed, dict):
        return str(parsed)[:200], "", {}

    message = parsed.get("message") or parsed.get("error") or ""
    if isinstance(message, list):
        message = "; ".join(map(str, message))
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    return str(message)[:200], str(data.get("code") or parsed.get("code") or ""), data


class StarvellClient:
    def __init__(self, session_cookie: str = STARVELL_SESSION):
        self.session_cookie = session_cookie
        self._http: aiohttp.ClientSession | None = None
        self._build_id = ""
        self._build_id_at = 0.0
        self._lock = asyncio.Lock()
        self.user: dict = {}

    @property
    def enabled(self) -> bool:
        return bool(self.session_cookie)

    @property
    def user_id(self) -> int:
        return int(self.user.get("id") or 0)

    @property
    def username(self) -> str:
        return str(self.user.get("username") or "")

    # -------------------------------------------------
    # HTTP
    # -------------------------------------------------

    def _cookies(self) -> dict:
        return {
            "session": self.session_cookie,
            "starvell.theme": "dark",
            "starvell.time_zone": "Europe/Moscow",
        }

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                headers={
                    "user-agent": USER_AGENT,
                    "accept-language": "ru,en;q=0.9",
                },
            )
        return self._http

    async def close(self):
        if self._http is not None and not self._http.closed:
            await self._http.close()

    async def _request(self, method: str, path: str, *, referer: str = "", attempts: int = 3, **kwargs) -> str:
        headers = {
            "accept": "*/*",
            "origin": BASE_URL,
            "referer": referer or f"{BASE_URL}/",
            **kwargs.pop("headers", {}),
        }
        url = path if path.startswith("http") else BASE_URL + path

        for attempt in range(attempts):
            session = await self._session()
            try:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    cookies=self._cookies(),
                    allow_redirects=False,
                    **kwargs,
                ) as response:
                    text = await response.text()
                    status = response.status
                    retry_after = response.headers.get("Retry-After")
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                if attempt + 1 == attempts:
                    raise
                await asyncio.sleep(2 ** attempt)
                continue

            if status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                try:
                    delay = min(30.0, float(retry_after))
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                await asyncio.sleep(delay)
                continue

            if status >= 300:
                message, code, data = _error_text(text)
                raise StarvellError(f"{method} {path}: HTTP {status} {message}", status, code, data)

            return text

        raise StarvellError(f"{method} {path}: нет ответа")

    async def api(self, method: str, path: str, payload=None, *, referer: str = ""):
        kwargs = {}
        if payload is not None:
            kwargs["json"] = payload
            kwargs["headers"] = {"content-type": "application/json"}
        text = await self._request(method, path, referer=referer, **kwargs)
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise StarvellError(f"{method} {path}: ответ не JSON") from error

    # -------------------------------------------------
    # Next.js страницы (/_next/data/<buildId>/...json)
    # -------------------------------------------------

    async def build_id(self, refresh: bool = False) -> str:
        async with self._lock:
            if refresh or not self._build_id or time.monotonic() - self._build_id_at > 1800:
                html = await self._request("GET", "/", headers={"accept": "text/html"})
                parser = _NextDataParser()
                parser.feed(html)
                data = json.loads("".join(parser.chunks) or "{}")
                self._build_id = str(data.get("buildId") or "")
                self._build_id_at = time.monotonic()
                if not self._build_id:
                    raise StarvellError("Не найден buildId на главной странице Starvell")
            return self._build_id

    async def page(self, page_path: str, params: dict | None = None, referer: str = "") -> dict:
        """pageProps страницы сайта, например page("chat") или page("account/sells")."""
        for attempt in range(2):
            build_id = await self.build_id(refresh=attempt > 0)
            try:
                text = await self._request(
                    "GET",
                    f"/_next/data/{build_id}/{page_path}.json",
                    referer=referer or f"{BASE_URL}/{page_path}",
                    headers={"x-nextjs-data": "1"},
                    params=params,
                )
            except StarvellError as error:
                if error.status == 404 and attempt == 0:
                    continue
                raise
            data = json.loads(text)
            props = data.get("pageProps") or (data.get("props") or {}).get("pageProps") or {}
            if props.get("__N_REDIRECT"):
                raise StarvellError("Cookie Starvell недействительна: сайт просит войти", 401)
            return props
        raise StarvellError(f"Страница {page_path} не найдена")

    # -------------------------------------------------
    # Аккаунт
    # -------------------------------------------------

    async def login(self) -> dict:
        props = await self.page("index")
        user = props.get("user")
        if not isinstance(user, dict) or not user.get("id"):
            data = await self.api("GET", "/api/profiles/me")
            user = data.get("user", data) if isinstance(data, dict) else None
        if not isinstance(user, dict) or not user.get("id"):
            raise StarvellError("Cookie Starvell недействительна: не удалось войти", 401)
        self.user = user
        return user

    # -------------------------------------------------
    # Чаты и сообщения
    # -------------------------------------------------

    async def chats(self) -> list[dict]:
        props = await self.page("chat")
        for container in (props, props.get("bff") or {}):
            if isinstance(container.get("chats"), list):
                return container["chats"]
        return []

    async def messages(self, chat_id: str, limit: int = 20) -> list[dict]:
        """Последние сообщения чата, от новых к старым."""
        data = await self.api(
            "POST",
            "/api/messages/list-v2",
            {"chatId": str(chat_id), "limit": max(1, min(50, limit))},
            referer=f"{BASE_URL}/chat/{chat_id}",
        )
        if isinstance(data, list):
            return data
        for container in (data, data.get("data") or {}, data.get("result") or {}):
            if isinstance(container, dict) and isinstance(container.get("items"), list):
                return container["items"]
        return []

    async def send_message(self, chat_id: str, text: str) -> dict:
        text = (text or "").strip()
        if len(text) > 1000:
            text = text[:997] + "..."
        return await self.api(
            "POST",
            "/api/messages/send",
            {"chatId": str(chat_id), "content": text},
            referer=f"{BASE_URL}/chat/{chat_id}",
        )

    async def mark_read(self, chat_id: str):
        await self.api(
            "POST",
            "/api/chats/read",
            {"chatId": str(chat_id)},
            referer=f"{BASE_URL}/chat/{chat_id}",
        )

    # -------------------------------------------------
    # Заказы
    # -------------------------------------------------

    async def orders(self, limit: int = 20, offset: int = 0, status: str | None = None) -> list[dict]:
        order_filter = {"userType": "seller"}
        if status:
            order_filter["status"] = status
        data = await self.api(
            "POST",
            "/api/orders/list",
            {
                "filter": order_filter,
                "limit": limit,
                "offset": offset,
                "with": {"buyer": True},
            },
            referer=f"{BASE_URL}/account/sells",
        )
        if isinstance(data, list):
            return data
        for key in ("data", "items", "orders"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("items"), list):
                return value["items"]
        return []

    async def mark_seller_completed(self, order_id: str):
        """Кнопка «Подтвердить выполнение заказа» со стороны продавца."""
        await self.api(
            "POST",
            f"/api/orders/{order_id}/mark-seller-completed",
            {},
            referer=f"{BASE_URL}/order/{order_id}",
        )

    async def order(self, order_id: str) -> dict:
        data = await self.api("GET", f"/api/orders/{order_id}", referer=f"{BASE_URL}/order/{order_id}")
        return data.get("data", data) if isinstance(data, dict) else {}

    # -------------------------------------------------
    # Лоты и поднятие
    # -------------------------------------------------

    async def profile_offers(self) -> list[dict]:
        """Категории профиля с лотами: [{id, gameId, game, offers: [...]}, ...]."""
        name = (self.username or str(self.user_id)).lower()
        props = await self.page(f"profile/{name}", params={"username": name})
        for container in (props, props.get("bff") or {}):
            catalog = container.get("catalogUserProfileOffersResult")
            if isinstance(catalog, dict) and isinstance(catalog.get("categories"), list):
                return catalog["categories"]
            for key in ("userProfileOffers", "categoriesWithOffers"):
                if isinstance(container.get(key), list):
                    return container[key]
        return []

    async def offer(self, public_id: str) -> dict:
        data = await self.api("GET", f"/api/offers/{public_id}", referer=offer_link(public_id))
        return data.get("data", data) if isinstance(data, dict) else {}

    async def set_offer_description(self, public_id: str, description: str) -> dict:
        """
        Меняет полное описание лота. Сайт сохраняет лот целиком, поэтому
        берём текущий лот и отправляем его с новым описанием — как форма
        редактирования на starvell.com.
        """
        # Данные страницы редактирования: в них полный список тегов
        props = await self.page(f"offers/edit/{public_id}", params={"offer_id": public_id})
        offer = props.get("offer") or {}
        if not offer:
            raise StarvellError(f"Лот {public_id} не найден")

        descriptions = (offer.get("descriptions") or {}).get("rus") or {}
        attributes = offer.get("attributes") or []

        payload = {
            **offer,
            "descriptions": {
                "rus": {
                    "briefDescription": descriptions.get("briefDescription"),
                    "description": description.strip(),
                }
            },
            "tags": offer.get("tags") or [],
            "goods": [],
            "goodsInstruction": offer.get("goodsInstruction"),
            "basicAttributes": [item for item in attributes if item.get("optionId") is not None],
            "numericAttributes": [
                {**item, "numericValue": float(item["numericValue"])}
                for item in attributes
                if item.get("numericValue") is not None
            ],
            # Цену Starvell принимает только с 2 знаками после запятой
            "price": str(offer.get("price") or "0"),
            "availability": int(offer.get("availability") or 0),
        }
        for key in ("attributes", "images"):
            payload.pop(key, None)

        try:
            return await self.api(
                "POST",
                f"/api/offers/{public_id}/update",
                payload,
                referer=f"{BASE_URL}/offers/edit/{public_id}",
            )
        except StarvellError as error:
            if error.status == 400 and "цен" in str(error).lower():
                raise StarvellError(
                    f"цена лота {payload['price']} ₽ — больше 2 знаков после запятой, "
                    "Starvell не сохранит лот, пока цену не округлить",
                    error.status, error.code, error.data,
                ) from error
            raise

    async def bump(self, game_id: int, category_ids: list[int]) -> dict:
        return await self.api(
            "POST",
            "/api/offers/bump",
            {"gameId": int(game_id), "categoryIds": [int(item) for item in category_ids]},
        )


def order_link(order_id) -> str:
    return f"{BASE_URL}/order/{order_id}"


def chat_link(chat_id) -> str:
    return f"{BASE_URL}/chat/{chat_id}"


def offer_link(offer_id) -> str:
    return f"{BASE_URL}/offers/{offer_id}"


starvell = StarvellClient()
