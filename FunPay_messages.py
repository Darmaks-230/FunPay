import asyncio
import json
import logging
import os
import io
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp
import discord

from funpaybotengine import Router
from funpaybotengine.types import Message

from funpay_shared import BASE_DIR, bot as funpay_bot, chat_storage, dispatcher


# =========================
# Настройки
# =========================

DISCORD_TOKEN = os.getenv(
    "DISCORD_TOKEN",
    ""
).strip()

FUNPAY_OWNER_ID = os.getenv(
    "FUNPAY_OWNER_ID",
    ""
).strip()

DISCORD_GUILD_ID = int(
    os.getenv("DISCORD_GUILD_ID", "0")
)

DISCORD_CATEGORY_ID = int(
    os.getenv("DISCORD_CATEGORY_ID", "0")
)

DISCORD_EVENTS_CHANNEL_ID = int(
    os.getenv("DISCORD_EVENTS_CHANNEL_ID", "0")
)

DISCORD_WEBHOOK_NAME = os.getenv(
    "DISCORD_WEBHOOK_NAME",
    "FunPay"
).strip()

DISCORD_WEBHOOK_AVATAR = os.getenv(
    "DISCORD_WEBHOOK_AVATAR",
    ""
).strip() or None

# Картинки больше этого размера не скачиваем, а даём ссылкой
IMAGE_MAX_BYTES = 8 * 1024 * 1024

IMAGE_TEXT = "📷 Картинка"

CHANNEL_DELETE_AFTER_DAYS = 3
CHANNEL_CLEANUP_INTERVAL = 60 * 60

CHANNELS_FILE = BASE_DIR / "funpay_channels.json"


# =========================
# Проверка настроек
# =========================

if not DISCORD_TOKEN:
    raise RuntimeError(
        "В .env не найден DISCORD_TOKEN"
    )

if not FUNPAY_OWNER_ID:
    raise RuntimeError(
        "В .env не найден FUNPAY_OWNER_ID"
    )

if not DISCORD_GUILD_ID:
    raise RuntimeError(
        "В .env не найден DISCORD_GUILD_ID"
    )

if not DISCORD_CATEGORY_ID:
    raise RuntimeError(
        "В .env не найден DISCORD_CATEGORY_ID"
    )

if not DISCORD_EVENTS_CHANNEL_ID:
    raise RuntimeError(
        "В .env не найден DISCORD_EVENTS_CHANNEL_ID"
    )


# =========================
# Discord
# =========================

intents = discord.Intents.default()

discord_client = discord.Client(
    intents=intents
)

discord_guild = None
discord_category = None
discord_events_channel = None

discord_ready = asyncio.Event()
channel_lock = asyncio.Lock()


# =========================
# FunPay
# =========================

# Общий FunPay-клиент (funpay_shared.py), здесь только свои обработчики.
# as_task=True — пока сообщение уходит в Discord,
# опрос FunPay продолжается.
router = Router(name="messages")


# =========================
# Стили событий
# =========================

EVENT_STYLES = {
    "message": {
        "title": "📩 Новое сообщение",
        "color": 0x2B90D9
    },

    "payment": {
        "title": "💰 Оплата заказа",
        "color": 0x2ECC71
    },

    "confirmation": {
        "title": "✅ Заказ подтверждён",
        "color": 0x3498DB
    },

    "review": {
        "title": "⭐ Новый отзыв",
        "color": 0xF1C40F
    },

    "review_deleted": {
        "title": "🗑️ Отзыв удалён",
        "color": 0xE74C3C
    },

    "refund": {
        "title": "↩️ Возврат средств",
        "color": 0xE67E22
    }
}


# =========================
# Работа с JSON
# =========================

def load_channels():
    if not CHANNELS_FILE.exists():
        return {}

    try:
        with CHANNELS_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

            if isinstance(data, dict):
                return data

    except Exception as error:
        logging.error(
            "Ошибка загрузки %s: %s",
            CHANNELS_FILE,
            error
        )

    return {}


def save_channels(data):
    temporary_file = CHANNELS_FILE.with_suffix(
        ".tmp"
    )

    try:
        with temporary_file.open(
            "w",
            encoding="utf-8"
        ) as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=4
            )

        temporary_file.replace(
            CHANNELS_FILE
        )

    except Exception as error:
        logging.error(
            "Ошибка сохранения %s: %s",
            CHANNELS_FILE,
            error
        )


user_channels = load_channels()


# =========================
# Вспомогательные функции
# =========================

def utc_now_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        result = datetime.fromisoformat(
            value
        )

        if result.tzinfo is None:
            result = result.replace(
                tzinfo=timezone.utc
            )

        return result

    except (TypeError, ValueError):
        return None


def clean_channel_name(username: str) -> str:
    username = username.lower().strip()

    username = re.sub(
        r"[^a-zа-яё0-9_-]+",
        "-",
        username
    )

    username = re.sub(
        r"-+",
        "-",
        username
    )

    username = username.strip("-")

    if not username:
        username = "unknown"

    return username[:100]


def get_username(message: Message) -> str:
    username = getattr(
        message,
        "sender_username",
        None
    )

    if username:
        return str(username)

    return "Пользователь FunPay"


def get_owner_username():
    try:
        return funpay_bot.username
    except Exception:
        return None


async def get_buyer_username(
    message: Message,
    chat_id: str,
    sender_id
):
    """
    Ник покупателя (собеседника) для названия канала.

    Отправитель сообщения для этого не подходит: это может быть
    сам продавец, системное сообщение FunPay или сообщение без
    заголовка (тогда ника нет вовсе). Поэтому сначала берём ник
    из превью чата FunPay — там всегда собеседник.
    """

    try:
        preview = await chat_storage.get_chat_preview(
            int(chat_id)
        )
    except (TypeError, ValueError):
        preview = None

    if preview is not None and preview.username:
        return preview.username

    not_buyer = {
        "funpay",
        "вы",
        "пользователь funpay",
        (get_owner_username() or "").lower()
    }

    sender_username = getattr(
        message,
        "sender_username",
        None
    )

    if (
        sender_username
        and not is_owner_id(sender_id)
        and sender_username.lower() not in not_buyer
    ):
        return sender_username

    saved_username = user_channels.get(
        str(chat_id),
        {}
    ).get("username")

    if saved_username and saved_username.lower() not in not_buyer:
        return saved_username

    return None


def get_sender_id(message: Message):
    message_fields = [
        "sender_id",
        "user_id",
        "author_id",
        "user_node_id"
    ]

    for field_name in message_fields:
        value = getattr(
            message,
            field_name,
            None
        )

        if value is not None:
            return str(value).strip()

    sender = getattr(
        message,
        "sender",
        None
    )

    if sender is not None:
        sender_fields = [
            "id",
            "user_id",
            "node_id",
            "user_node_id"
        ]

        for field_name in sender_fields:
            value = getattr(
                sender,
                field_name,
                None
            )

            if value is not None:
                return str(value).strip()

    return None


def is_owner_id(sender_id) -> bool:
    if sender_id is None:
        return False

    return str(sender_id).strip() == FUNPAY_OWNER_ID


def display_username(
    username: str,
    sender_id
) -> str:
    if is_owner_id(sender_id):
        return "Вы"

    return username


def get_avatar_url(message: Message):
    fields = [
        "sender_avatar",
        "sender_avatar_url",
        "avatar_url",
        "user_avatar"
    ]

    for field_name in fields:
        value = getattr(
            message,
            field_name,
            None
        )

        if value:
            return str(value)

    sender = getattr(
        message,
        "sender",
        None
    )

    if sender:
        for field_name in [
            "avatar",
            "avatar_url",
            "photo",
            "photo_url"
        ]:
            value = getattr(
                sender,
                field_name,
                None
            )

            if value:
                return str(value)

    return None


def get_chat_id(message: Message):
    chat_id = getattr(
        message,
        "chat_id",
        None
    )

    if chat_id:
        return str(chat_id)

    chat = getattr(
        message,
        "chat",
        None
    )

    if chat:
        chat_id = (
            getattr(
                chat,
                "node_id",
                None
            )
            or getattr(
                chat,
                "id",
                None
            )
        )

        if chat_id:
            return str(chat_id)

    return None


def get_image_url(message: Message):
    image_url = getattr(
        message,
        "image_url",
        None
    )

    if not image_url:
        return None

    image_url = str(image_url).strip()

    if image_url.startswith("//"):
        return "https:" + image_url

    if image_url.startswith("/"):
        return "https://funpay.com" + image_url

    return image_url


async def download_image(image_url: str):
    """
    Скачивает картинку из чата FunPay и возвращает discord.File.

    Картинку загружаем в Discord файлом, а не ссылкой: так она
    не пропадёт, если ссылка FunPay перестанет открываться.
    """

    file_name = os.path.basename(
        urlparse(image_url).path
    )

    file_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        file_name
    ) or "image.jpg"

    if "." not in file_name:
        file_name += ".jpg"

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            async with session.get(image_url) as response:
                if response.status != 200:
                    logging.warning(
                        "Картинка не скачалась (HTTP %s): %s",
                        response.status,
                        image_url
                    )
                    return None

                data = await response.content.read(
                    IMAGE_MAX_BYTES + 1
                )

    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        logging.warning(
            "Ошибка скачивания картинки %s: %s",
            image_url,
            error
        )
        return None

    if len(data) > IMAGE_MAX_BYTES:
        logging.info(
            "Картинка больше %s МБ, отправляю ссылкой: %s",
            IMAGE_MAX_BYTES // (1024 * 1024),
            image_url
        )
        return None

    return discord.File(
        io.BytesIO(data),
        filename=file_name
    )


def is_ignored_command_or_response(text: str) -> bool:
    """
    Не отправляет в Discord команды /info и /money,
    а также ответы FunPay на эти команды.

    Игнорируются:

    /info ник
    /money ник

    Использование: /info ник
    Использование: /money ник

    📊 Информация об игроке: ник
    💰 Монеты игрока ник: количество
    """

    if not text:
        return False

    normalized_text = (
        str(text)
        .replace("ё", "е")
        .lower()
        .strip()
    )

    # Сам пользователь отправил команду.
    first_line = normalized_text.splitlines()[0].strip()

    if re.match(
        r"^/(?:info|money)(?:\s|$)",
        first_line,
        flags=re.IGNORECASE
    ):
        return True

    # Ответы FunPay на команды /info и /money.
    ignored_phrases = [
        "использование: /info",
        "использование: /money",
        "информация об игроке:",
        "монеты игрока",
        "гемы игрока",
        "баланс игрока:",
        "баланс до заказа:",
        "цель фарма:",
        "прогресс:",
        "не найден в базе данных."
    ]

    for phrase in ignored_phrases:
        if phrase in normalized_text:
            return True

    return False


def detect_event_type(text: str) -> str:
    text = text.lower().strip()

    if any(word in text for word in [
        "отзыв удален",
        "отзыв удалён",
        "удаление отзыва",
        "review deleted"
    ]):
        return "review_deleted"

    if any(word in text for word in [
        "возврат",
        "средства возвращены",
        "деньги возвращены",
        "refund"
    ]):
        return "refund"

    if any(word in text for word in [
        "оплата заказа",
        "заказ оплачен",
        "оплатил заказ",
        "покупатель оплатил",
        "payment",
        "paid"
    ]):
        return "payment"

    if any(word in text for word in [
        "заказ подтвержден",
        "заказ подтверждён",
        "подтвердил заказ",
        "сделка завершена",
        "заказ выполнен",
        "order confirmed"
    ]):
        return "confirmation"

    if any(word in text for word in [
        "новый отзыв",
        "оставил отзыв",
        "вам оставили отзыв",
        "пользователь оценил",
        "review",
        "feedback"
    ]):
        return "review"

    return "message"


def build_embed(
    username: str,
    text: str,
    chat_id,
    event_type: str
):
    style = EVENT_STYLES.get(
        event_type,
        EVENT_STYLES["message"]
    )

    text = str(text).strip()

    if not text:
        text = "[сообщение без текста]"

    title_text = text[:256]

    description_text = None

    if len(text) > 256:
        description_text = text[256:4000]

        if len(text) > 4000:
            description_text = (
                description_text[:3990]
                + "\n..."
            )

    embed = discord.Embed(
        title=title_text,
        description=description_text,
        color=style["color"]
    )

    if chat_id:
        chat_url = (
            f"https://funpay.com/chat/"
            f"?node={chat_id}"
        )

        embed.set_author(
            name=username,
            url=chat_url
        )
    else:
        embed.set_author(
            name=username
        )

    embed.set_footer(
        text=style["title"]
    )

    return embed


# =========================
# Перемещение канала наверх
# =========================

async def move_channel_to_top(channel):
    if channel is None:
        return

    if channel.category_id != DISCORD_CATEGORY_ID:
        return

    try:
        category_channels = (
            discord_category.text_channels
        )

        if category_channels:
            if category_channels[0].id == channel.id:
                return

        await channel.edit(
            position=discord_category.position + 1,
            reason="Новое сообщение FunPay"
        )

        logging.info(
            "Канал #%s поднят наверх",
            channel.name
        )

    except discord.Forbidden:
        logging.error(
            "Нет прав для перемещения канала #%s",
            channel.name
        )

    except discord.HTTPException as error:
        logging.error(
            "Ошибка перемещения канала #%s: %s",
            channel.name,
            error
        )


# =========================
# Каналы и webhook
# =========================

async def rename_channel_if_needed(
    channel,
    buyer_username
):
    if not buyer_username:
        return

    channel_name = clean_channel_name(
        buyer_username
    )

    if channel.name == channel_name:
        return

    try:
        old_name = channel.name

        await channel.edit(
            name=channel_name,
            reason="Название по нику покупателя"
        )

        logging.info(
            "Канал #%s переименован в #%s",
            old_name,
            channel_name
        )

    except discord.HTTPException as error:
        logging.error(
            "Ошибка переименования канала #%s: %s",
            channel.name,
            error
        )


async def get_or_create_user_channel(
    chat_id: str,
    buyer_username
):
    await discord_ready.wait()

    channel_key = str(chat_id)

    async with channel_lock:
        saved_data = user_channels.get(
            channel_key
        )

        if saved_data:
            channel = discord_guild.get_channel(
                saved_data.get("channel_id")
            )

            webhook_url = saved_data.get(
                "webhook_url"
            )

            if channel and webhook_url:
                try:
                    webhook = discord.Webhook.from_url(
                        webhook_url,
                        session=(
                            discord_client
                            .http
                            ._HTTPClient__session
                        )
                    )

                    if buyer_username:
                        saved_data["username"] = buyer_username

                    saved_data["last_activity"] = (
                        utc_now_iso()
                    )

                    user_channels[channel_key] = saved_data
                    save_channels(user_channels)

                    await rename_channel_if_needed(
                        channel,
                        buyer_username
                    )

                    await move_channel_to_top(
                        channel
                    )

                    return channel, webhook

                except Exception as error:
                    logging.error(
                        "Ошибка подключения к webhook: %s",
                        error
                    )

        if buyer_username:
            channel_name = clean_channel_name(
                buyer_username
            )
        else:
            # Ник пока неизвестен — переименуем, когда узнаем
            channel_name = f"chat-{chat_id}"

        channel = discord.utils.get(
            discord_category.text_channels,
            name=channel_name
        )

        if channel is None:
            channel = await discord_guild.create_text_channel(
                name=channel_name,
                category=discord_category,
                reason=f"FunPay chat {chat_id}"
            )

        webhooks = await channel.webhooks()

        webhook = discord.utils.find(
            lambda item: item.name == DISCORD_WEBHOOK_NAME,
            webhooks
        )

        if webhook is None:
            webhook = await channel.create_webhook(
                name=DISCORD_WEBHOOK_NAME,
                reason=f"FunPay webhook for chat {chat_id}"
            )

        user_channels[channel_key] = {
            "chat_id": chat_id,
            "username": buyer_username,
            "channel_id": channel.id,
            "webhook_url": webhook.url,
            "last_activity": utc_now_iso()
        }

        save_channels(user_channels)

        await move_channel_to_top(
            channel
        )

        return channel, webhook


async def send_to_user_channel(
    username: str,
    buyer_username,
    text: str,
    chat_id: str,
    event_type: str,
    image_url=None
):
    channel, webhook = await get_or_create_user_channel(
        chat_id=chat_id,
        buyer_username=buyer_username
    )

    embed = build_embed(
        username=username,
        text=text,
        chat_id=chat_id,
        event_type=event_type
    )

    image_file = None

    if image_url:
        image_file = await download_image(
            image_url
        )

        if image_file is not None:
            embed.set_image(
                url=f"attachment://{image_file.filename}"
            )
        else:
            embed.set_image(
                url=image_url
            )

    send_kwargs = {}

    if image_file is not None:
        send_kwargs["file"] = image_file

    await webhook.send(
        embed=embed,
        username=DISCORD_WEBHOOK_NAME,
        avatar_url=DISCORD_WEBHOOK_AVATAR,
        wait=True,
        **send_kwargs
    )

    print(
        f"Сообщение отправлено в "
        f"#{channel.name} "
        f"(chat_id: {chat_id})"
    )


async def send_to_events_channel(
    username: str,
    text: str,
    chat_id: str,
    event_type: str
):
    if event_type == "message":
        return

    embed = build_embed(
        username=username,
        text=text,
        chat_id=chat_id,
        event_type=event_type
    )

    await discord_events_channel.send(
        embed=embed
    )


# =========================
# Удаление неактивных каналов
# =========================

async def cleanup_inactive_channels():
    await discord_ready.wait()

    while not discord_client.is_closed():
        try:
            inactive_since = (
                datetime.now(timezone.utc)
                - timedelta(
                    days=CHANNEL_DELETE_AFTER_DAYS
                )
            )

            async with channel_lock:
                channels_to_delete = []

                for channel_key, data in list(
                    user_channels.items()
                ):
                    last_activity = parse_iso_datetime(
                        data.get("last_activity")
                    )

                    channel_id = data.get(
                        "channel_id"
                    )

                    channel = discord_guild.get_channel(
                        channel_id
                    )

                    if last_activity is None:
                        if channel is not None:
                            last_activity = (
                                channel.created_at
                            )
                        else:
                            last_activity = datetime.min.replace(
                                tzinfo=timezone.utc
                            )

                    if last_activity < inactive_since:
                        channels_to_delete.append(
                            (
                                channel_key,
                                channel
                            )
                        )

                for channel_key, channel in (
                    channels_to_delete
                ):
                    try:
                        if channel is not None:
                            channel_name = channel.name

                            await channel.delete(
                                reason=(
                                    "Канал FunPay неактивен "
                                    f"{CHANNEL_DELETE_AFTER_DAYS} дня"
                                )
                            )

                            print(
                                f"Удалён неактивный канал "
                                f"#{channel_name}"
                            )

                    except discord.NotFound:
                        pass

                    except discord.Forbidden:
                        logging.error(
                            "Нет прав на удаление канала"
                        )
                        continue

                    except discord.HTTPException as error:
                        logging.error(
                            "Ошибка удаления канала: %s",
                            error
                        )
                        continue

                    user_channels.pop(
                        channel_key,
                        None
                    )

                if channels_to_delete:
                    save_channels(
                        user_channels
                    )

        except Exception:
            logging.exception(
                "Ошибка очистки неактивных каналов"
            )

        await asyncio.sleep(
            CHANNEL_CLEANUP_INTERVAL
        )


# =========================
# Discord ready
# =========================

@discord_client.event
async def on_ready():
    global discord_guild
    global discord_category
    global discord_events_channel

    discord_guild = discord_client.get_guild(
        DISCORD_GUILD_ID
    )

    if discord_guild is None:
        raise RuntimeError(
            "Discord-сервер не найден. "
            "Проверь DISCORD_GUILD_ID."
        )

    discord_category = discord_guild.get_channel(
        DISCORD_CATEGORY_ID
    )

    if not isinstance(
        discord_category,
        discord.CategoryChannel
    ):
        raise RuntimeError(
            "DISCORD_CATEGORY_ID "
            "не является категорией Discord."
        )

    discord_events_channel = discord_guild.get_channel(
        DISCORD_EVENTS_CHANNEL_ID
    )

    if not isinstance(
        discord_events_channel,
        discord.TextChannel
    ):
        raise RuntimeError(
            "DISCORD_EVENTS_CHANNEL_ID "
            "не является текстовым каналом."
        )

    print(
        f"Discord подключён: "
        f"{discord_client.user}"
    )

    print(
        f"Сервер: "
        f"{discord_guild.name}"
    )

    print(
        f"Категория: "
        f"{discord_category.name}"
    )

    print(
        f"Канал событий: "
        f"#{discord_events_channel.name}"
    )

    discord_ready.set()


# =========================
# Сообщения FunPay
# =========================

@router.on_new_message(as_task=True)
async def handle_funpay_message(message: Message):
    chat_id = get_chat_id(
        message
    )

    if not chat_id:
        print(
            "Сообщение пропущено: "
            "не найден chat_id"
        )
        return

    image_url = get_image_url(
        message
    )

    text = getattr(
        message,
        "text",
        None
    ) or (
        IMAGE_TEXT
        if image_url
        else "[сообщение без текста]"
    )

    text = str(text)

    # Игнорируем сами команды и все ответы
    # FunPay на команды /info и /money.
    if is_ignored_command_or_response(text):
        print(
            f"[FunPay] Системное сообщение пропущено: "
            f"{text} "
            f"(chat_id: {chat_id})"
        )
        return

    original_username = get_username(
        message
    )

    sender_id = get_sender_id(
        message
    )

    username = display_username(
        original_username,
        sender_id
    )

    event_type = detect_event_type(
        text
    )

    print(
        f"[FunPay] "
        f"[{event_type}] "
        f"{username}: "
        f"{text} "
        f"(chat_id: {chat_id}, "
        f"sender_id: {sender_id})"
    )

    try:
        buyer_username = await get_buyer_username(
            message,
            chat_id,
            sender_id
        )

        await send_to_user_channel(
            username=username,
            buyer_username=buyer_username,
            text=text,
            chat_id=chat_id,
            event_type=event_type,
            image_url=image_url
        )

        await send_to_events_channel(
            username=username,
            text=text,
            chat_id=chat_id,
            event_type=event_type
        )

    except Exception:
        logging.exception(
            "Ошибка отправки сообщения в Discord"
        )


# =========================
# Запуск
# =========================

async def run_discord():
    """
    Discord-часть: клиент и очистка каналов.
    Вызывается из run_all.py и при отдельном запуске.
    """

    await asyncio.gather(
        discord_client.start(DISCORD_TOKEN),
        cleanup_inactive_channels()
    )


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(message)s"
        )
    )

    dispatcher.connect_router(router)

    await asyncio.gather(
        run_discord(),
        funpay_bot.listen_events(
            dispatcher,
            session_storage=chat_storage
        )
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print(
            "Бот остановлен."
        )

    except Exception as error:
        logging.exception(
            "Критическая ошибка: %s",
            error
        )
