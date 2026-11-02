"""
Бот Starvell — то же, что боты FunPay, но для starvell.com:

- команды покупателей в чате: /money, /info, /exp, /help
- пересылка чатов Starvell в Discord (каналы starvell-ник рядом с funpay-ник)
  и события заказов в канал событий
- новые продажи -> карточка «➕ В таблицу» в общей Discord-таблице,
  подтверждение и возврат убирают аккаунт из таблицы
- сообщение покупателю о конце фарма и кнопка
  «Подтвердить выполнение заказа»
- автоподнятие лотов

Запускается из run_all.py вместе с ботами FunPay. Работает через
cookie «session» (STARVELL_SESSION в .env); без неё не запускается.
"""

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp
import discord

import FunPay_commands as commands
import FunPay_messages as funpay_messages
from FunPay_sales import close_order, is_starvell_id, save_sale
from starvell_api import (
    BASE_DIR,
    StarvellError,
    chat_link,
    offer_link,
    order_link,
    starvell,
)


log = logging.getLogger("starvell")


# =========================
# Настройки
# =========================

def env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Чаты Starvell идут в ту же категорию и тот же канал событий, что и FunPay;
# каналы покупателей отличаются приставкой: starvell-ник и funpay-ник.
STARVELL_WEBHOOK_NAME = os.getenv("STARVELL_WEBHOOK_NAME", "Starvell").strip() or "Starvell"

STARVELL_RAISE = os.getenv("STARVELL_RAISE", "true").lower() in {"1", "true", "yes", "on"}
# Как часто пробовать поднять лоты, если Starvell не сказал, сколько ждать
STARVELL_RAISE_MINUTES = env_int("STARVELL_RAISE_MINUTES", 30)

CHATS_POLL_SECONDS = 5
ORDERS_POLL_SECONDS = 10
FARM_CHECK_SECONDS = 30
LOTS_CACHE_SECONDS = 60 * 60
CHANNEL_DELETE_AFTER_DAYS = funpay_messages.CHANNEL_DELETE_AFTER_DAYS

STATE_FILE = BASE_DIR / "starvell_state.json"
CHANNELS_FILE = BASE_DIR / "starvell_channels.json"

CDN_URL = "https://cdn.starvell.com"


# =========================
# Сохранённое состояние
# =========================

def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass
    except Exception as error:
        log.error("Ошибка загрузки %s: %s", path.name, error)
    return default


def save_json(path, data):
    temporary_file = path.with_suffix(".tmp")
    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        temporary_file.replace(path)
    except Exception as error:
        log.error("Ошибка сохранения %s: %s", path.name, error)


# chats: {chat_id: id последнего обработанного сообщения}
# orders: {order_id: статус}
# При первом запуске (нет chats_ready / orders_ready) старые сообщения
# и заказы только запоминаются, без ответов и карточек.
state = load_json(STATE_FILE, {})
state.setdefault("chats", {})
state.setdefault("orders", {})
# Заказы, по которым уже нажата «Подтвердить выполнение заказа»
state.setdefault("seller_completed", [])

# Опрос чатов идёт из цикла и из обработки новой продажи
chats_lock = asyncio.Lock()

user_channels = load_json(CHANNELS_FILE, {})

started_at = datetime.now(timezone.utc)

# Покупатель -> его чат (для новых продаж)
buyer_chats: dict[int, str] = {}
# Чат -> ник собеседника
chat_usernames: dict[str, str] = {}


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


# =========================
# Лоты (для /exp и поднятия)
# =========================

_lots: list[dict] = []
_lots_loaded_at = 0.0


async def get_lots(refresh: bool = False) -> list[dict]:
    """Категории профиля с лотами, кэш на час."""
    global _lots, _lots_loaded_at

    if refresh or not _lots or time.monotonic() - _lots_loaded_at > LOTS_CACHE_SECONDS:
        _lots = await starvell.profile_offers()
        _lots_loaded_at = time.monotonic()

    return _lots


def offer_title(offer: dict) -> str:
    descriptions = (offer.get("descriptions") or {}).get("rus") or {}
    return descriptions.get("briefDescription") or descriptions.get("description") or ""


async def get_tower_lot_link(tower_name: str) -> str:
    try:
        for category in await get_lots():
            for offer in category.get("offers") or []:
                if commands.tower_name_from_title(offer_title(offer)) == tower_name:
                    return offer_link(offer.get("publicId") or offer.get("id"))
    except Exception:
        log.exception("Не удалось получить лоты Starvell для /exp")
    return ""


# =========================
# Команды
# =========================

async def command_reply(text: str, chat_id: str, admin: bool) -> str | None:
    text = (text or "").strip()

    if not text.startswith("/"):
        return None

    reply = commands.money_reply(text, chat_id, admin)
    if reply is None:
        reply = commands.info_reply(text, chat_id, admin)
    if reply is None:
        reply = await commands.exp_reply(text, get_tower_lot_link)
    if reply is None and text.lower().startswith("/help"):
        reply = commands.HELP_TEXT

    return reply


async def send_starvell_message(chat_id, text: str):
    await starvell.send_message(str(chat_id), text)


# =========================
# Discord
# =========================

def discord_guild():
    return funpay_messages.discord_guild


def channel_name_for(username: str, chat_id: str) -> str:
    return funpay_messages.clean_channel_name(username or f"chat-{chat_id[:8]}", "starvell")


# Общий с FunPay замок: каналы обеих площадок в одной категории
channel_lock = funpay_messages.channel_lock

funpay_messages.managed_channel_sources.append(
    lambda: [data.get("channel_id") for data in user_channels.values()]
)


async def get_or_create_user_channel(chat_id: str, username: str):
    await funpay_messages.discord_ready.wait()

    category = funpay_messages.discord_category
    session = funpay_messages.discord_client.http._HTTPClient__session

    async with channel_lock:
        saved = user_channels.get(chat_id)

        if saved:
            channel = discord_guild().get_channel(saved.get("channel_id"))
            if channel is not None and saved.get("webhook_url"):
                saved["last_activity"] = datetime.now(timezone.utc).isoformat()
                if username:
                    saved["username"] = username
                name = channel_name_for(saved.get("username") or "", chat_id)
                if channel.name != name:
                    try:
                        await channel.edit(name=name)
                    except discord.HTTPException as error:
                        log.error("Ошибка переименования канала #%s: %s", channel.name, error)
                save_json(CHANNELS_FILE, user_channels)
                await funpay_messages.move_channel_to_top(channel)
                return channel, discord.Webhook.from_url(saved["webhook_url"], session=session)

        name = channel_name_for(username, chat_id)
        channel = discord.utils.get(category.text_channels, name=name)

        if channel is None:
            await funpay_messages.free_category_slot()

            channel = await discord_guild().create_text_channel(
                name=name,
                category=category,
                reason=f"Starvell chat {chat_id}"
            )

        webhooks = await channel.webhooks()
        webhook = discord.utils.find(lambda item: item.name == STARVELL_WEBHOOK_NAME, webhooks)

        if webhook is None:
            webhook = await channel.create_webhook(
                name=STARVELL_WEBHOOK_NAME,
                reason=f"Starvell webhook for chat {chat_id}"
            )

        user_channels[chat_id] = {
            "chat_id": chat_id,
            "username": username,
            "channel_id": channel.id,
            "webhook_url": webhook.url,
            "last_activity": datetime.now(timezone.utc).isoformat(),
        }
        save_json(CHANNELS_FILE, user_channels)
        await funpay_messages.move_channel_to_top(channel)

        return channel, webhook


def build_embed(author: str, text: str, chat_id: str, event_type: str, order_id: str = ""):
    style = funpay_messages.EVENT_STYLES.get(event_type, funpay_messages.EVENT_STYLES["message"])
    text = (text or "").strip() or "[сообщение без текста]"

    embed = discord.Embed(
        title=text[:256],
        description=text[256:4000] or None,
        color=style["color"]
    )

    url = order_link(order_id) if order_id else chat_link(chat_id)
    if order_id:
        embed.url = url
    embed.set_author(name=author, url=url)
    embed.set_footer(text=f"{style['title']} · Starvell")
    return embed


async def download_image(url: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    return None
                data = await response.read()
    except Exception as error:
        log.error("Не удалось скачать картинку %s: %s", url, error)
        return None

    if len(data) > funpay_messages.IMAGE_MAX_BYTES:
        return None

    return discord.File(io.BytesIO(data), filename=url.rsplit("/", 1)[-1])


async def forward_to_discord(chat_id: str, username: str, author: str, text: str,
                             event_type: str, order_id: str = "", image_url: str = "", review=None):
    try:
        channel, webhook = await get_or_create_user_channel(chat_id, username)

        embed = build_embed(author, text, chat_id, event_type, order_id)
        funpay_messages.add_review_fields(embed, review)

        send_kwargs = {}
        if image_url:
            image_file = await download_image(image_url)
            if image_file is not None:
                embed.set_image(url=f"attachment://{image_file.filename}")
                send_kwargs["file"] = image_file
            else:
                embed.set_image(url=image_url)

        await webhook.send(
            embed=embed,
            username=STARVELL_WEBHOOK_NAME,
            avatar_url=funpay_messages.DISCORD_WEBHOOK_AVATAR,
            wait=True,
            **send_kwargs
        )

        # Оплату не дублируем: по каждой продаже уже есть карточка «В таблицу»
        if event_type not in ("message", "payment"):
            embed = build_embed(author, text, chat_id, event_type, order_id)
            funpay_messages.add_review_fields(embed, review)
            await funpay_messages.discord_events_channel.send(embed=embed)

    except Exception:
        log.exception("Ошибка отправки сообщения Starvell в Discord")


async def cleanup_inactive_channels():
    await funpay_messages.discord_ready.wait()

    while True:
        try:
            await delete_inactive_channels()
        except Exception:
            log.exception("Ошибка очистки неактивных каналов Starvell")

        await asyncio.sleep(funpay_messages.CHANNEL_CLEANUP_INTERVAL)


async def delete_inactive_channels():
    inactive_since = datetime.now(timezone.utc) - timedelta(days=CHANNEL_DELETE_AFTER_DAYS)

    async with channel_lock:
        for chat_id, data in list(user_channels.items()):
            last_activity = parse_time(data.get("last_activity"))
            if last_activity and last_activity >= inactive_since:
                continue

            channel = discord_guild().get_channel(data.get("channel_id"))
            try:
                if channel is not None:
                    await channel.delete(reason=f"Канал Starvell неактивен {CHANNEL_DELETE_AFTER_DAYS} дн.")
                    log.info("Удалён неактивный канал #%s", channel.name)
            except discord.NotFound:
                pass
            except discord.HTTPException as error:
                log.error("Ошибка удаления канала: %s", error)
                continue

            user_channels.pop(chat_id, None)

        save_json(CHANNELS_FILE, user_channels)


# =========================
# Сообщения чатов
# =========================

# Уведомления Starvell о заказах: тип -> (событие, текст)
NOTIFICATIONS = {
    "ORDER_PAYMENT": ("payment", "Покупатель {buyer} оплатил заказ #{order}"),
    "ORDER_SELLER_COMPLETED": ("message", "Продавец отметил заказ #{order} выполненным"),
    "ORDER_COMPLETED": ("confirmation", "Покупатель {buyer} подтвердил выполнение заказа #{order}"),
    "ORDER_REFUND": ("refund", "Деньги за заказ #{order} возвращены покупателю {buyer}"),
    "REVIEW_CREATED": ("review", "Покупатель {buyer} написал отзыв к заказу #{order}"),
}


async def fetch_review(order_id: str):
    try:
        review = (await starvell.page(f"order/{order_id}")).get("review") or {}
    except Exception:
        log.exception("Не удалось получить отзыв к заказу %s", order_id)
        return None

    if not review.get("rating") and not review.get("content"):
        return None

    return SimpleNamespace(rating=int(review.get("rating") or 0), text=review.get("content") or "")


# Признаки вредоносного кода в сообщении (XSS: <img onerror=eval(atob(...))>).
# Такой код с вашего аккаунта рассылает вирус, запущенный в браузере на starvell.com.
SUSPICIOUS_CODE = re.compile(r"onerror\s*=|<\s*script|javascript:|eval\s*\(|atob\s*\(", re.IGNORECASE)

SUSPICIOUS_WARNING = (
    "⚠️ ВРЕДОНОСНЫЙ КОД — не открывайте этот чат на starvell.com в браузере!\n"
)


def message_image_url(message: dict) -> str:
    for image in message.get("images") or []:
        if isinstance(image, dict) and image.get("id"):
            return f"{CDN_URL}/messages/{image['id']}.{image.get('extension') or 'png'}"
    return ""


async def handle_message(chat: dict, message: dict):
    chat_id = chat["id"]
    username = chat_usernames.get(chat_id, "")
    author_id = message.get("authorId")
    is_mine = author_id == starvell.user_id

    visible_to = message.get("visibleTo")
    if visible_to not in (None, starvell.user_id, str(starvell.user_id)):
        return

    if message.get("type") == "NOTIFICATION":
        metadata = message.get("metadata") or {}
        notification = NOTIFICATIONS.get(metadata.get("notificationType"))
        if notification is None:
            return

        event_type, template = notification
        order_id = metadata.get("orderId") or ""
        buyer = (message.get("buyer") or {}).get("username") or username
        text = template.format(buyer=buyer, order=metadata.get("orderShortId") or order_id[:8])

        # Закрытие заказа: страховка к опросу заказов (повтор не страшен)
        order = message.get("order") or {}
        if event_type == "confirmation":
            close_order(order_id, "confirmed")
        elif event_type == "refund" and order.get("refundType") in (None, "FULL"):
            close_order(order_id, "refunded")

        review = await fetch_review(order_id) if event_type == "review" else None
        log.info("[Starvell] [%s] %s (chat %s)", event_type, text, chat_id)
        await forward_to_discord(chat_id, username, "Starvell", text, event_type, order_id, review=review)
        return

    text = message.get("content") or ""
    image_url = message_image_url(message)

    # Команда покупателя (или продавца — тогда без привязки чата)
    reply = await command_reply(text, chat_id, admin=is_mine)
    if reply:
        try:
            await send_starvell_message(chat_id, reply)
        except Exception:
            log.exception("Не удалось ответить на команду в чате %s", chat_id)

    if funpay_messages.is_ignored_command_or_response(text):
        return

    if not text and not image_url:
        return

    author = starvell.username if is_mine else (username or "Покупатель")

    if SUSPICIOUS_CODE.search(text):
        text = SUSPICIOUS_WARNING + text
        log.warning("Подозрительное сообщение в чате Starvell %s (отправил %s)", chat_id, author)

    log.info("[Starvell] %s: %s (chat %s)", author, text or funpay_messages.IMAGE_TEXT, chat_id)
    await forward_to_discord(
        chat_id,
        username,
        author,
        text or funpay_messages.IMAGE_TEXT,
        "message",
        image_url=image_url
    )


async def poll_chats():
    async with chats_lock:
        await _poll_chats(first_run=not state.get("chats_ready"))


async def _poll_chats(first_run: bool):
    chats = await starvell.chats()
    changed = first_run
    state["chats_ready"] = True

    for chat in chats:
        chat_id = chat.get("id")
        if not chat_id:
            continue

        for participant in chat.get("participants") or []:
            if participant.get("id") != starvell.user_id:
                chat_usernames[chat_id] = participant.get("username") or ""
                buyer_chats[participant.get("id")] = chat_id

        last_id = (chat.get("lastMessage") or {}).get("id") or ""
        seen_id = state["chats"].get(chat_id)

        if first_run:
            state["chats"][chat_id] = last_id
            changed = True
            continue

        if not last_id or last_id == seen_id:
            continue

        messages = await starvell.messages(chat_id, 20)

        # ID сообщений Starvell — UUIDv7, по ним можно сортировать по времени
        new_messages = sorted(
            (item for item in messages if item.get("id") and item["id"] > (seen_id or "")),
            key=lambda item: item["id"]
        )

        for message in new_messages:
            # Новый для бота чат: только сообщения после запуска
            if seen_id is None:
                created = parse_time(message.get("createdAt"))
                if created is None or created < started_at - timedelta(minutes=1):
                    continue
            await handle_message(chat, message)

        state["chats"][chat_id] = max([last_id] + [item["id"] for item in new_messages])
        changed = True

    if changed:
        save_json(STATE_FILE, state)


def is_site_down(error: Exception) -> bool:
    """Starvell недоступен (502, техработы, нет соединения) — не ошибка бота."""
    if isinstance(error, StarvellError):
        return error.status >= 500
    return isinstance(error, (aiohttp.ClientConnectionError, asyncio.TimeoutError))


# Пока сайт лежит, пишем в лог один раз, а не каждые 5 секунд
site_down_since: float | None = None


def report_poll_error(what: str, error: Exception):
    global site_down_since

    if is_site_down(error):
        if site_down_since is None:
            site_down_since = time.monotonic()
            log.warning("Starvell недоступен (%s), жду восстановления: %s", what, error)
        return

    if isinstance(error, StarvellError):
        log.error("Ошибка опроса %s Starvell: %s", what, error)
    else:
        log.exception("Ошибка опроса %s Starvell", what)


def report_poll_ok():
    global site_down_since

    if site_down_since is not None:
        log.info("Starvell снова доступен (не работал %s мин.)", int((time.monotonic() - site_down_since) // 60))
        site_down_since = None


async def chats_loop():
    while True:
        try:
            await poll_chats()
            report_poll_ok()
        except Exception as error:
            report_poll_error("чатов", error)
            await asyncio.sleep(30)
        await asyncio.sleep(CHATS_POLL_SECONDS)


# =========================
# Заказы
# =========================

def sale_title(order: dict) -> str:
    """Название как у FunPay, чтобы его разобрал parse_sale_title: '..., Услуги, 3000 шт.'"""
    details = order.get("offerDetails") or {}
    descriptions = (details.get("descriptions") or {}).get("rus") or {}
    parts = [
        descriptions.get("briefDescription") or (details.get("category") or {}).get("name") or "",
        (details.get("subCategory") or {}).get("name") or "",
        f"{order.get('quantity') or 0} шт.",
    ]
    return ", ".join(part for part in parts if part)


async def handle_new_order(order: dict):
    order_id = order["id"]
    buyer = order.get("user") or {}
    chat_id = buyer_chats.get(order.get("buyerId") or buyer.get("id"))

    if chat_id is None:
        # Чат с новым покупателем мог ещё не попасть в список
        await poll_chats()
        chat_id = buyer_chats.get(order.get("buyerId") or buyer.get("id"), "")

    price = f"{(order.get('totalPrice') or 0) / 100:g} ₽"
    title = sale_title(order)

    save_sale(
        order_id=order_id,
        buyer=buyer.get("username") or "",
        chat_id=chat_id or "",
        title=title,
        price=price,
        short_id=order.get("shortId") or "",
    )
    log.info("Новая продажа Starvell #%s: %s (%s)", order.get("shortId"), title, price)


async def poll_orders():
    first_run = not state.get("orders_ready")
    orders = await starvell.orders(limit=20)
    changed = first_run
    state["orders_ready"] = True

    for order in orders:
        order_id = order.get("id")
        status = order.get("status")
        if not order_id or not status:
            continue

        old_status = state["orders"].get(order_id)
        if old_status == status:
            continue

        state["orders"][order_id] = status
        changed = True

        if first_run:
            continue

        # CREATED — заказ оплачен (PRE_CREATED — ещё нет)
        if status == "CREATED" and old_status in (None, "PRE_CREATED"):
            await handle_new_order(order)
        elif status == "COMPLETED":
            close_order(order_id, "confirmed")
            log.info("Заказ Starvell #%s подтверждён", order.get("shortId"))
        elif status == "REFUND" and order.get("refundType") in (None, "FULL"):
            close_order(order_id, "refunded")
            log.info("Возврат по заказу Starvell #%s", order.get("shortId"))

    # Не храним историю бесконечно: только заказы из последнего списка
    if len(state["orders"]) > 500:
        recent = {order.get("id") for order in orders}
        state["orders"] = {key: value for key, value in state["orders"].items() if key in recent}

    if changed:
        save_json(STATE_FILE, state)


async def orders_loop():
    while True:
        try:
            await poll_orders()
            report_poll_ok()
        except Exception as error:
            report_poll_error("заказов", error)
            await asyncio.sleep(30)
        await asyncio.sleep(ORDERS_POLL_SECONDS)


def finished_starvell_orders() -> set[str]:
    """Заказы Starvell, по которым аккаунт нафармил (main.py пишет их в finished_orders)."""
    order_ids = set()

    for database_path in commands.database_paths():
        try:
            with sqlite3.connect(database_path) as connection:
                for (order_id,) in connection.execute("SELECT order_id FROM finished_orders"):
                    if is_starvell_id(order_id):
                        order_ids.add(order_id)
        except sqlite3.Error as error:
            log.error("Ошибка чтения finished_orders из %s: %s", database_path.name, error)

    return order_ids


async def confirm_finished_orders():
    """
    Аккаунт нафармил — нажимаем «Подтвердить выполнение заказа», чтобы
    покупатель мог подтвердить заказ. Один раз на заказ.
    """
    done = set(state.setdefault("seller_completed", []))

    for order_id in sorted(finished_starvell_orders() - done):
        try:
            await starvell.mark_seller_completed(order_id)
        except StarvellError as error:
            log.error("Не удалось подтвердить выполнение заказа Starvell %s: %s", order_id, error)
            continue

        log.info("Выполнение заказа Starvell %s подтверждено", order_id)
        state["seller_completed"] = (state["seller_completed"] + [order_id])[-200:]
        save_json(STATE_FILE, state)


async def farm_loop():
    while True:
        try:
            await commands.check_farm(send_starvell_message, starvell=True)
            await confirm_finished_orders()
        except Exception:
            log.exception("Ошибка проверки фарма Starvell")
        await asyncio.sleep(FARM_CHECK_SECONDS)


# =========================
# Автоподнятие лотов
# =========================

async def raise_loop():
    if not STARVELL_RAISE:
        log.info("Автоподнятие Starvell выключено (STARVELL_RAISE=false)")
        await asyncio.Event().wait()

    # Игра -> когда можно поднимать снова
    next_raise: dict[int, float] = {}

    while True:
        try:
            games: dict[int, list[int]] = {}
            for category in await get_lots(refresh=True):
                game_id = category.get("gameId") or (category.get("game") or {}).get("id")
                if game_id and category.get("id") and category.get("offers"):
                    games.setdefault(int(game_id), []).append(int(category["id"]))

            for game_id, category_ids in games.items():
                if time.monotonic() < next_raise.get(game_id, 0):
                    continue

                wait = STARVELL_RAISE_MINUTES * 60
                try:
                    await starvell.bump(game_id, category_ids)
                    log.info("Лоты Starvell подняты: игра %s, категории %s", game_id, category_ids)
                except StarvellError as error:
                    retry_after = (error.data or {}).get("retryAfterSeconds")
                    if error.code == "OFFERS_BUMP_COOLDOWN" and retry_after:
                        wait = int(retry_after) + 5
                        log.info("Starvell: поднять игру %s можно через %s мин.", game_id, wait // 60)
                    else:
                        log.error("Не удалось поднять лоты Starvell (игра %s): %s", game_id, error)

                next_raise[game_id] = time.monotonic() + wait
                await asyncio.sleep(3)

        except StarvellError as error:
            log.error("Ошибка автоподнятия Starvell: %s", error)
        except Exception:
            log.exception("Ошибка автоподнятия Starvell")

        await asyncio.sleep(60)


# =========================
# Запуск
# =========================

async def run():
    """Все задачи Starvell. Вызывается из run_all.py."""

    if not starvell.enabled:
        log.warning("STARVELL_SESSION не задан в .env — бот Starvell не запущен")
        # Не даём супервизору перезапускать без конца
        await asyncio.Event().wait()

    # Техработы на Starvell: ждём молча, без падения и трассировки в логе
    while True:
        try:
            user = await starvell.login()
            break
        except Exception as error:
            if not is_site_down(error):
                raise
            report_poll_error("входа", error)
            await asyncio.sleep(60)

    report_poll_ok()
    log.info("Starvell подключён: %s (id %s)", user.get("username"), user.get("id"))

    try:
        await asyncio.gather(
            chats_loop(),
            orders_loop(),
            farm_loop(),
            raise_loop(),
            cleanup_inactive_channels(),
        )
    finally:
        await starvell.close()
