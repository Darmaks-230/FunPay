import os
import re
import sqlite3
import contextvars
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import asyncio

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from FunPay_sales import (
    CREATE_CLOSED_ORDERS,
    CREATE_FINISHED_ORDERS,
    CREATE_PENDING_SALES,
    order_id_from_link,
)


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TABLE_CHANNEL_ID = int(os.getenv("TABLE_CHANNEL_ID", "0"))
ORDER_ROLE_ID = int(os.getenv("ORDER_ROLE_ID", "0"))
STALE_CHANNEL_ID = int(os.getenv("STALE_CHANNEL_ID", "0"))  # Новый канал для уведомлений о просрочке

BASE_DIR = Path(__file__).resolve().parent

# База основного сервера (того, где канал TABLE_CHANNEL_ID).
# У остальных серверов свои файлы orders_<id сервера>.sqlite3.
DB_FILE = str(BASE_DIR / "orders.sqlite3")
MAX_DISCORD_LENGTH = 2000
DEFAULT_BLOCKS = [3, 3, 3]
IMAGE_FILE_PATH = "itogiconkursa.png"

delay = 5


# -----------------------------
# База данных
# -----------------------------

# -----------------------------
# Свои таблицы на каждом сервере
# -----------------------------
#
# Раньше база и канал таблицы были одни на всех: команда на любом
# сервере меняла таблицу там, где лежит TABLE_CHANNEL_ID.
# Теперь у каждого сервера свой файл базы. Код ниже по-прежнему
# пишет просто db.execute(...), а запрос уходит в базу того сервера,
# с которым сейчас идёт работа (current_guild_id). Его выставляют
# команды, реакции, сообщения Match Won и фоновая проверка.

current_guild_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_guild_id",
    default=None
)

_connections: dict[str, sqlite3.Connection] = {}


def _read_main_guild_id() -> Optional[int]:
    if not os.path.exists(DB_FILE):
        return None

    connection = sqlite3.connect(DB_FILE)
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = 'guild_id'"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        connection.close()

    return int(row[0]) if row and row[0] else None


# Сервер, которому принадлежит orders.sqlite3 (определяется в on_ready)
MAIN_GUILD_ID: Optional[int] = _read_main_guild_id()


def is_main_guild(guild_id: Optional[int] = None) -> bool:
    if guild_id is None:
        guild_id = current_guild_id.get()

    return guild_id is None or guild_id == MAIN_GUILD_ID


def database_path(guild_id: Optional[int]) -> str:
    if is_main_guild(guild_id):
        return DB_FILE

    return str(BASE_DIR / f"orders_{guild_id}.sqlite3")


def known_guild_ids() -> list[int]:
    """Серверы, у которых уже есть своя таблица."""
    result = []

    if MAIN_GUILD_ID:
        result.append(MAIN_GUILD_ID)

    for path in BASE_DIR.glob("orders_*.sqlite3"):
        match = re.fullmatch(r"orders_(\d+)\.sqlite3", path.name)
        if match and int(match.group(1)) not in result:
            result.append(int(match.group(1)))

    return result


@contextmanager
def use_guild(guild_id: Optional[int]):
    token = current_guild_id.set(guild_id)
    try:
        yield
    finally:
        current_guild_id.reset(token)


def get_connection(guild_id: Optional[int]) -> sqlite3.Connection:
    path = database_path(guild_id)

    if path not in _connections:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        _connections[path] = connection

        # Новая база сервера: создаём таблицы и стандартные блоки
        with use_guild(guild_id):
            init_db()
            initialize_table()

    return _connections[path]


class GuildDatabase:
    """db.execute(...) идёт в базу текущего сервера."""

    def connection(self) -> sqlite3.Connection:
        return get_connection(current_guild_id.get())

    def execute(self, *args):
        return self.connection().execute(*args)

    def executescript(self, script: str):
        return self.connection().executescript(script)

    def commit(self):
        self.connection().commit()


db = GuildDatabase()


def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_number INTEGER NOT NULL UNIQUE,
            position INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_number INTEGER NOT NULL,
            row_number INTEGER NOT NULL,
            player TEXT NOT NULL DEFAULT '',
            before_coins INTEGER NOT NULL DEFAULT 0,
            paid_coins INTEGER NOT NULL DEFAULT 0,
            link TEXT NOT NULL DEFAULT '',
            changed_by TEXT NOT NULL DEFAULT '',
            changed_at TEXT NOT NULL DEFAULT '',
            changed INTEGER NOT NULL DEFAULT 0,
            created_by_id TEXT DEFAULT '',
            ping_sent INTEGER DEFAULT 0,
            account_coins INTEGER NOT NULL DEFAULT 0,
            account_coins_updated_at TEXT,
            adjust_coins INTEGER NOT NULL DEFAULT 0,
            last_stale_message_id INTEGER DEFAULT 0,
            UNIQUE(block_number, row_number)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS table_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL UNIQUE,
            channel_id INTEGER NOT NULL,
            position INTEGER NOT NULL
        );
        """
    )
    db.execute(CREATE_PENDING_SALES)
    db.execute(CREATE_CLOSED_ORDERS)
    db.execute(CREATE_FINISHED_ORDERS)
    db.commit()

    # Проверяем и добавляем отсутствующие колонки
    columns = db.execute("PRAGMA table_info(rows)").fetchall()
    col_names = [col["name"] for col in columns]

    needed_cols = {
        "account_coins": "INTEGER NOT NULL DEFAULT 0",
        "account_coins_updated_at": "TEXT",
        "adjust_coins": "INTEGER NOT NULL DEFAULT 0",
        "before_gems": "INTEGER NOT NULL DEFAULT 0",
        "paid_gems": "INTEGER NOT NULL DEFAULT 0",
        "account_gems": "INTEGER NOT NULL DEFAULT 0",
        "adjust_gems": "INTEGER NOT NULL DEFAULT 0",
        "currency": "TEXT NOT NULL DEFAULT 'coins'",
        "created_by_id": "TEXT DEFAULT ''",
        "ping_sent": "INTEGER DEFAULT 0",
        "last_stale_message_id": "INTEGER DEFAULT 0",
        "finish_message_id": "INTEGER DEFAULT 0",
        "chat_id": "TEXT",
        "farm_finished": "INTEGER DEFAULT 0",
        "mark": "TEXT DEFAULT ''",
    }

    for col_name, col_type in needed_cols.items():
        if col_name not in col_names:
            db.execute(f"ALTER TABLE rows ADD COLUMN {col_name} {col_type}")
            db.commit()

    # Инициализируем глобальные настройки, если их нет
    if get_setting("global_ping_users") is None:
        set_setting("global_ping_users", "")
    
    if get_setting("stale_minutes") is None:
        set_setting("stale_minutes", "30")


def get_setting(key: str) -> Optional[str]:
    row = db.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,)
    ).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str):
    db.execute(
        """
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value)
    )
    db.commit()


def guild_setting_int(key: str, main_default: int) -> int:
    value = get_setting(key)

    if value:
        return int(value)

    return main_default if is_main_guild() else 0


def table_channel_id() -> int:
    return guild_setting_int("table_channel_id", TABLE_CHANNEL_ID)


def stale_channel_id() -> int:
    return guild_setting_int("stale_channel_id", STALE_CHANNEL_ID)


def order_role_id() -> int:
    return guild_setting_int("order_role_id", ORDER_ROLE_ID)


def initialize_table():
    exists = db.execute("SELECT COUNT(*) AS count FROM blocks").fetchone()["count"]

    if exists:
        return

    for block_number, row_count in enumerate(DEFAULT_BLOCKS, start=1):
        db.execute(
            "INSERT INTO blocks(block_number, position) VALUES (?, ?)",
            (block_number, block_number)
        )

        for row_number in range(1, row_count + 1):
            db.execute(
                """
                INSERT INTO rows(block_number, row_number)
                VALUES (?, ?)
                """,
                (block_number, row_number)
            )

    db.commit()


# -----------------------------
# Работа со структурой таблицы
# -----------------------------

def blocks():
    return db.execute(
        "SELECT * FROM blocks ORDER BY position"
    ).fetchall()


def get_row(row_name: str):
    match = re.fullmatch(r"(\d+)\.(\d+)", row_name.strip())

    if not match:
        return None

    block_number = int(match.group(1))
    row_number = int(match.group(2))

    return db.execute(
        """
        SELECT *
        FROM rows
        WHERE block_number = ? AND row_number = ?
        """,
        (block_number, row_number)
    ).fetchone()


def normalize_rows(block_number: int):
    current_rows = db.execute(
        """
        SELECT id
        FROM rows
        WHERE block_number = ?
        ORDER BY row_number
        """,
        (block_number,)
    ).fetchall()

    for number, row in enumerate(current_rows, start=1):
        db.execute(
            "UPDATE rows SET row_number = ? WHERE id = ?",
            (number, row["id"])
        )


def normalize_blocks():
    current_blocks = db.execute(
        "SELECT block_number FROM blocks ORDER BY position"
    ).fetchall()

    for index, row in enumerate(current_blocks, start=1000):
        db.execute(
            "UPDATE blocks SET block_number = ? WHERE block_number = ?",
            (index, row["block_number"])
        )
        db.execute(
            "UPDATE rows SET block_number = ? WHERE block_number = ?",
            (index, row["block_number"])
        )

    temporary = db.execute(
        "SELECT block_number FROM blocks ORDER BY position"
    ).fetchall()

    for new_number, row in enumerate(temporary, start=1):
        old_number = row["block_number"]

        db.execute(
            "UPDATE blocks SET block_number = ? WHERE block_number = ?",
            (new_number, old_number)
        )
        db.execute(
            "UPDATE rows SET block_number = ? WHERE block_number = ?",
            (new_number, old_number)
        )

    db.commit()


def add_row(block_number: int, position: int):
    block = db.execute(
        "SELECT * FROM blocks WHERE block_number = ?",
        (block_number,)
    ).fetchone()

    if not block:
        raise ValueError("Такого блока не существует.")

    count = db.execute(
        "SELECT COUNT(*) AS count FROM rows WHERE block_number = ?",
        (block_number,)
    ).fetchone()["count"]

    if position < 1 or position > count + 1:
        raise ValueError(
            f"Позиция должна быть от 1 до {count + 1}."
        )

    db.execute(
        """
        UPDATE rows
        SET row_number = row_number + 1
        WHERE block_number = ? AND row_number >= ?
        """,
        (block_number, position)
    )

    db.execute(
        """
        INSERT INTO rows(block_number, row_number)
        VALUES (?, ?)
        """,
        (block_number, position)
    )

    db.commit()


def delete_row(row_name: str):
    row = get_row(row_name)

    if not row:
        raise ValueError("Указанная строка не существует.")

    db.execute("DELETE FROM rows WHERE id = ?", (row["id"],))

    db.execute(
        """
        UPDATE rows
        SET row_number = row_number - 1
        WHERE block_number = ? AND row_number > ?
        """,
        (row["block_number"], row["row_number"])
    )

    db.commit()


def add_block(position: int, row_count: int):
    count = db.execute(
        "SELECT COUNT(*) AS count FROM blocks"
    ).fetchone()["count"]

    if position < 1 or position > count + 1:
        raise ValueError(f"Позиция блока должна быть от 1 до {count + 1}.")

    if row_count < 1 or row_count > 100:
        raise ValueError("Количество строк должно быть от 1 до 100.")

    db.execute(
        """
        UPDATE blocks
        SET position = position + 1
        WHERE position >= ?
        """,
        (position,)
    )

    temporary_number = 100000 + count

    db.execute(
        """
        INSERT INTO blocks(block_number, position)
        VALUES (?, ?)
        """,
        (temporary_number, position)
    )

    for row_number in range(1, row_count + 1):
        db.execute(
            """
            INSERT INTO rows(block_number, row_number)
            VALUES (?, ?)
            """,
            (temporary_number, row_number)
        )

    db.commit()
    normalize_blocks()


def delete_block(block_number: int):
    block = db.execute(
        "SELECT * FROM blocks WHERE block_number = ?",
        (block_number,)
    ).fetchone()

    if not block:
        raise ValueError("Такого блока не существует.")

    db.execute(
        "DELETE FROM rows WHERE block_number = ?",
        (block_number,)
    )
    db.execute(
        "DELETE FROM blocks WHERE block_number = ?",
        (block_number,)
    )
    db.execute(
        """
        UPDATE blocks
        SET position = position - 1
        WHERE position > ?
        """,
        (block["position"],)
    )

    db.commit()
    normalize_blocks()


def resize_block(block_number: int, new_count: int):
    if new_count < 1 or new_count > 100:
        raise ValueError("Количество строк должно быть от 1 до 100.")

    current_count = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM rows
        WHERE block_number = ?
        """,
        (block_number,)
    ).fetchone()["count"]

    if current_count == 0:
        raise ValueError("Такого блока не существует.")

    if new_count > current_count:
        for number in range(current_count + 1, new_count + 1):
            db.execute(
                """
                INSERT INTO rows(block_number, row_number)
                VALUES (?, ?)
                """,
                (block_number, number)
            )
    elif new_count < current_count:
        db.execute(
            """
            DELETE FROM rows
            WHERE block_number = ? AND row_number > ?
            """,
            (block_number, new_count)
        )

    db.commit()


# -----------------------------
# Форматирование таблицы
# -----------------------------

def marketplace(link):
    if "funpay" in link:
        return "FunPay"
    
    if "starvell" in link:
        return "Starvell"

    return ""


def format_timestamp(dt_str: str) -> str:
    """Преобразует ISO-строку в Discord timestamp (относительное время)"""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str)
        timestamp = int(dt.timestamp())
        return f"<t:{timestamp}:R>"
    except:
        return ""


def format_row(row) -> str:
    player = row["player"] or "-"
    currency = row["currency"] or "coins"

    if currency == "gems":
        before = row["before_gems"] or 0
        paid = row["paid_gems"] or 0
        account_balance = row["account_gems"] or 0
        adjust = row["adjust_gems"] or 0
        currency_name = "💎 Гемы"
        unit = ""
    else:
        before = row["before_coins"] or 0
        paid = row["paid_coins"] or 0
        account_balance = row["account_coins"] or 0
        adjust = row["adjust_coins"] or 0
        currency_name = "💰 Монеты"
        unit = "к"

    total_balance = account_balance + adjust if account_balance > 0 else "-"
    link = row["link"] or ""
    updated_at = row["account_coins_updated_at"] or row["changed_at"] or ""

    balance_text = f"| {currency_name}: {total_balance}" if player != "-" else ""

    time_str = f"| {format_timestamp(updated_at)}" if player != "-" else ""

    marketplace_name = marketplace(link)
    marketplace_text = f"| [{marketplace_name}]({link})" if link else ""

    ping_sent = row["ping_sent"] or 0
    ping_status = "🔔" if ping_sent else ""

    mark = row["mark"] or ""
    if mark:
        marks_list = [m.strip() for m in mark.split(",") if m.strip()]
        mark_emojis_map = {"gems": "💎", "coins": "💰", "afk": "⏸️"}
        mark_emoji = " ".join(mark_emojis_map.get(m, "") for m in marks_list if mark_emojis_map.get(m))
    else:
        mark_emoji = ""

    return (
        f'{row["block_number"]}.{row["row_number"]}. '
        f'{player} | {before}{unit} | {paid}{unit} | Цель: {before + paid}{unit}'
        f'{balance_text} {marketplace_text} {time_str} {ping_status} {mark_emoji}'
    )


def build_table_for_block(block_number) -> str:
    """Собирает текст таблицы только для одного блока"""
    block_rows = db.execute(
        """
        SELECT *
        FROM rows
        WHERE block_number = ?
        ORDER BY row_number
        """,
        (block_number,)
    ).fetchall()

    separator = "═" * 25
    output = [f"{separator} Блок {block_number} {separator}\n"]
    for row_index, row in enumerate(block_rows):
        output.append(format_row(row))
        if row_index < len(block_rows) - 1:
            separator = "—" * 49
            output.append(separator)

    # Невидимый пробел в конце, чтобы Discord не сворачивал текст
    output.append("\u200b")

    return "\n".join(output)


def parse_coins(value: str) -> Optional[int]:
    if not value:
        return None

    value = value.replace(" ", "").replace(",", "")

    match = re.search(r"\d+", value)

    if not match:
        return None

    return int(match.group())


def parse_match_won(message: discord.Message):
    """Извлекает Player, Coins и Gems из обычного текста или Discord embed.

    Поддерживает оба варианта:
    Player\n    Eveidro46

    и markdown-вид:
    **Player**\n    Eveidro46
    """
    texts = []

    if message.content:
        texts.append(message.content)

    for embed in message.embeds:
        if embed.title:
            texts.append(embed.title)
        if embed.description:
            texts.append(embed.description)
        if embed.footer and embed.footer.text:
            texts.append(embed.footer.text)
        for field in embed.fields:
            texts.append(field.name)
            texts.append(field.value)

    full_text = "\n".join(texts)

    # Приводим строки к простому виду: убираем markdown ** и лишние пробелы.
    lines = []
    for raw_line in full_text.splitlines():
        line = raw_line.replace("\u200b", "").strip()
        line = re.sub(r"^\*{1,3}|\*{1,3}$", "", line).strip()
        if line:
            lines.append(line)

    def value_after_label(label: str):
        label_lower = label.lower()
        for i, line in enumerate(lines):
            clean = line.strip().rstrip(":").strip().lower()
            if clean == label_lower and i + 1 < len(lines):
                return lines[i + 1].strip()

            # Поддержка формата "Player: Eveidro46" / "**Player**: Eveidro46".
            match = re.match(rf"^{re.escape(label)}\s*:\s*(.+)$", line, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    player = value_after_label("Player")
    coins_raw = value_after_label("Coins")
    gems_raw = value_after_label("Gems")

    coins = parse_coins(coins_raw) if coins_raw else None
    gems = parse_coins(gems_raw) if gems_raw else None

    if not player or (coins is None and gems is None):
        print("Не удалось найти Player и Coins/Gems.")
        print("Полученный текст:")
        print(full_text)
        return None

    return player.strip(), coins, gems


def clear_row_by_id(row_id: int, reason: str = "Очистка по реакции ✅"):
    """Очищает строку (сбрасывает все поля)"""
    now_iso = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        UPDATE rows
        SET player = '',
            before_coins = 0,
            paid_coins = 0,
            before_gems = 0,
            paid_gems = 0,
            currency = 'coins',
            account_coins = 0,
            account_gems = 0,
            account_coins_updated_at = ?,
            adjust_coins = 0,
            adjust_gems = 0,
            link = '',
            changed = 1,
            changed_by = ?,
            changed_at = ?,
            created_by_id = '',
            ping_sent = 0,
            last_stale_message_id = 0,
            finish_message_id = 0,
            chat_id = '',
            farm_finished = 0,
            mark = ''
        WHERE id = ?
        """,
        (now_iso, reason, now_iso, row_id)
    )
    db.commit()


async def delete_bot_message(channel_id: int, message_id: int) -> bool:
    """
    Удаляет сообщение бота одним запросом.

    Раньше сообщение сначала загружалось через fetch_message, а для
    этого нужно право «Читать историю сообщений». Без него удаление
    молча не срабатывало. Свои сообщения бот удаляет без доп. прав.

    Возвращает True, если сообщения больше нет в канале.
    """
    if not channel_id or not message_id:
        return True

    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)

        await channel.get_partial_message(message_id).delete()
        print(f"[delete_bot_message] Удалено уведомление {message_id}")
        return True
    except discord.NotFound:
        return True
    except discord.Forbidden:
        print(f"[delete_bot_message] Нет прав на удаление сообщения {message_id}")
    except Exception as e:
        print(f"[delete_bot_message] Ошибка удаления {message_id}: {e}")

    return False


async def delete_previous_stale_message(row_id: int):
    """
    Удаляет уведомление о просрочке строки — например, когда
    строка снова начала обновляться.
    """
    row = db.execute(
        "SELECT last_stale_message_id FROM rows WHERE id = ?",
        (row_id,)
    ).fetchone()

    if not row or not row["last_stale_message_id"]:
        return

    deleted = await delete_bot_message(stale_channel_id(), row["last_stale_message_id"])

    if not deleted:
        # ID оставляем: следующая проверка (каждые 5 минут) попробует снова
        return

    db.execute(
        "UPDATE rows SET last_stale_message_id = 0 WHERE id = ?",
        (row_id,)
    )
    db.commit()


async def delete_row_notifications(*row_ids: int):
    """
    Удаляет все уведомления строк (просрочка и «завершил фарм»).
    Вызывается перед очисткой/удалением строки — иначе в базе
    ID обнулялся, а само сообщение оставалось в канале навсегда.
    """
    for row_id in row_ids:
        row = db.execute(
            "SELECT last_stale_message_id, finish_message_id FROM rows WHERE id = ?",
            (row_id,)
        ).fetchone()

        if not row:
            continue

        await delete_bot_message(stale_channel_id(), row["last_stale_message_id"])
        await delete_bot_message(table_channel_id(), row["finish_message_id"])

        db.execute(
            "UPDATE rows SET last_stale_message_id = 0, finish_message_id = 0 WHERE id = ?",
            (row_id,)
        )

    db.commit()


async def check_and_notify(row_id: int):
    """Проверяет суммарный баланс (account_coins + adjust_coins) на достижение цели."""
    row = db.execute(
        "SELECT * FROM rows WHERE id = ?",
        (row_id,)
    ).fetchone()
    if not row:
        print(f"[check_and_notify] Строка {row_id} не найдена")
        return

    if (row["currency"] or "coins") == "gems":
        total = row["before_gems"] + row["paid_gems"]
        currency_name = "гемов"
    else:
        total = (row["before_coins"] + row["paid_coins"]) * 1000
        currency_name = "монет"

    print(f"[check_and_notify] total target = {total} {currency_name}")

    if total <= 0:
        print("[check_and_notify] total <= 0, уведомление не требуется")
        return

    if (row["currency"] or "coins") == "gems":
        total_balance = (row["account_gems"] or 0) + (row["adjust_gems"] or 0)
    else:
        total_balance = (row["account_coins"] or 0) + (row["adjust_coins"] or 0)

    ping_sent = row["ping_sent"] or 0
    print(f"[check_and_notify] total_balance (account+adjust) = {total_balance}, ping_sent = {ping_sent}")

    if total_balance >= total and ping_sent == 0:
        print("[check_and_notify] УСЛОВИЕ ВЫПОЛНЕНО! Освобождаем строку и отправляем уведомление.")
        await finish_farm(row, total_balance, total, currency_name)
    else:
        print("[check_and_notify] Условие НЕ выполнено, уведомление НЕ отправлено.")


def format_big(value) -> str:
    value = float(value)
    text = f"{int(value):,}" if value.is_integer() else f"{value:,.2f}"
    return text.replace(",", " ")


async def finish_farm(row, total_balance, total, currency_name: str):
    """
    Аккаунт нафармил: пишем об этом и сразу убираем его из таблицы.
    Сообщение удалится само, когда покупатель подтвердит заказ на FunPay
    (см. process_closed_orders), или вручную реакцией ✅.
    """
    channel = bot.get_channel(table_channel_id())
    if channel is None:
        try:
            channel = await bot.fetch_channel(table_channel_id())
        except Exception:
            print("[finish_farm] Не удалось получить канал для уведомления")
            return

    user_ids = (get_setting("global_ping_users") or "").split()
    mentions = " ".join(f"<@{uid}>" for uid in user_ids if uid)

    player_name = row["player"] or "Игрок"
    row_str = f"{row['block_number']}.{row['row_number']}"
    link = row["link"] or ""
    order_id = order_id_from_link(link)

    lines = [
        f"{mentions} ✅ {player_name} (строка {row_str}) завершил фарм!".strip(),
        f"Нафармлено: {format_big(total_balance)} из {format_big(total)} {currency_name}.",
        "Аккаунт убран из таблицы, строка свободна.",
    ]

    if link:
        lines.append(f"Заказ: <{link}>")

    if order_id:
        lines.append("Сообщение удалится, когда покупатель подтвердит заказ.")

    try:
        msg = await channel.send(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.all()
        )
        await msg.add_reaction("✅")
    except Exception as e:
        print(f"[finish_farm] Ошибка отправки уведомления: {e}")
        return

    db.execute(
        """
        INSERT INTO finished_orders(
            order_id, player, row_name, currency, balance, goal,
            chat_id, channel_id, message_id, buyer_notified, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            player_name,
            row_str,
            row["currency"] or "coins",
            total_balance,
            total,
            row["chat_id"] or "",
            channel.id,
            msg.id,
            # Если FunPay-бот уже успел написать покупателю — второй раз не пишем
            row["farm_finished"] or 0,
            datetime.now(timezone.utc).isoformat()
        )
    )
    db.commit()

    await delete_row_notifications(row["id"])
    clear_row_by_id(row["id"], "Фарм завершён")
    print(f"[finish_farm] Строка {row_str} освобождена: {player_name} нафармил.")

    await update_table_messages()


async def process_closed_orders():
    """
    Покупатель подтвердил заказ на FunPay (FunPay_sales.py кладёт его
    в closed_orders): убираем аккаунт из таблицы, если он ещё там,
    и удаляем сообщение «завершил фарм».
    """
    with use_guild(None):
        closed = db.execute(
            "SELECT order_id FROM closed_orders WHERE processed = 0"
        ).fetchall()

    for item in closed:
        order_id = item["order_id"]

        for guild_id in known_guild_ids():
            with use_guild(guild_id):
                rows = db.execute(
                    """
                    SELECT *
                    FROM rows
                    WHERE link LIKE ? OR link LIKE ?
                    """,
                    (f"%/orders/{order_id}/%", f"%/orders/{order_id}")
                ).fetchall()

                for row in rows:
                    await delete_row_notifications(row["id"])
                    clear_row_by_id(row["id"], "Заказ подтверждён покупателем")
                    print(
                        f"[closed_orders] Заказ #{order_id} подтверждён: "
                        f"строка {row['block_number']}.{row['row_number']} очищена"
                    )

                notices = db.execute(
                    "SELECT * FROM finished_orders WHERE order_id = ?",
                    (order_id,)
                ).fetchall()

                for notice in notices:
                    await delete_bot_message(notice["channel_id"], notice["message_id"])
                    db.execute(
                        "DELETE FROM finished_orders WHERE id = ?",
                        (notice["id"],)
                    )
                    print(f"[closed_orders] Заказ #{order_id} подтверждён: сообщение удалено")

                db.commit()

                if rows:
                    try:
                        await update_table_messages()
                    except Exception as error:
                        print(f"[closed_orders] Ошибка обновления таблицы: {error}")

        with use_guild(None):
            db.execute(
                "UPDATE closed_orders SET processed = 1 WHERE order_id = ?",
                (order_id,)
            )
            db.commit()


async def check_stale_updates():
    """Проверяет строки, где монеты не обновлялись более заданного времени, и отправляет уведомление в отдельный канал."""
    stale_minutes = int(get_setting("stale_minutes") or "30")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=stale_minutes)
    print(f"[check_stale_updates] Проверка в {now.isoformat()}, cutoff={cutoff.isoformat()}")

    rows = db.execute(
        """
        SELECT *
        FROM rows
        WHERE player != ''
          AND (mark IS NULL OR mark = '' OR mark NOT LIKE '%afk%')
          AND (
              (account_coins_updated_at != '' AND account_coins_updated_at IS NOT NULL)
              OR (changed_at != '' AND changed_at IS NOT NULL)
          )
        """
    ).fetchall()

    print(f"[check_stale_updates] Найдено строк с игроком и временем: {len(rows)}")

    for row in rows:
        time_str = row["account_coins_updated_at"] or row["changed_at"]
        if not time_str:
            continue
        try:
            last_update = datetime.fromisoformat(time_str)
            last_update_utc = last_update.astimezone(timezone.utc)
        except Exception as e:
            print(f"[check_stale_updates] Ошибка парсинга времени '{time_str}': {e}")
            continue

        print(f"[check_stale_updates] Строка {row['block_number']}.{row['row_number']} "
              f"последнее обновление: {last_update_utc.isoformat()}")

        is_stale = last_update_utc < cutoff

        if is_stale:
            # Строка просрочена - отправляем уведомление в канал просрочек
            print(f"[check_stale_updates] Строка {row['block_number']}.{row['row_number']} ПРОСРОЧЕНА! Отправляем уведомление в канал просрочек.")

            # Удаляем предыдущее уведомление для этой строки
            await delete_previous_stale_message(row["id"])
            
            # Проверяем, указан ли канал для просрочек
            if not stale_channel_id():
                print("[check_stale_updates] Канал просрочек на сервере не задан (/menu set-stale-channel)")
                continue
            
            ping_users_str = get_setting("global_ping_users") or ""
            user_ids = ping_users_str.split()
            mentions = " ".join(f"<@{uid}>" for uid in user_ids if uid) if user_ids else ""

            player_name = row["player"]
            row_str = f"{row['block_number']}.{row['row_number']}"
            
            current_time_str = format_timestamp(now.isoformat())
            msg_text = (
                f"{mentions} ⚠️ ВНИМАНИЕ! {player_name} (строка {row_str}) "
                f"не обновлял монеты более {stale_minutes} минут!\n"
                f"Последнее обновление: {format_timestamp(time_str)}\n"
                f"Текущее время: {current_time_str}\n"
                f"Пожалуйста, обновите данные!"
                if mentions else
                f"⚠️ ВНИМАНИЕ! {player_name} (строка {row_str}) не обновлял монеты более {stale_minutes} минут!\n"
                f"Последнее обновление: {format_timestamp(time_str)}\n"
                f"Текущее время: {current_time_str}\n"
                f"Пожалуйста, обновите данные!"
            )

            channel = bot.get_channel(stale_channel_id())
            if channel is None:
                try:
                    channel = await bot.fetch_channel(stale_channel_id())
                except Exception as e:
                    print(f"[check_stale_updates] Не удалось получить канал просрочек: {e}")
                    continue

            try:
                msg = await channel.send(msg_text, allowed_mentions=discord.AllowedMentions.all())
                await msg.add_reaction("✅")
                
                # Сохраняем ID нового сообщения в БД
                db.execute(
                    "UPDATE rows SET last_stale_message_id = ? WHERE id = ?",
                    (msg.id, row["id"])
                )
                db.commit()
                
                print(f"[check_stale_updates] Уведомление отправлено в канал просрочек для строки {row_str}, ID сообщения: {msg.id}")
            except Exception as e:
                print(f"[check_stale_updates] Ошибка отправки: {e}")
                continue
        else:
            # Если строка больше не просрочена и есть старое уведомление - удаляем его
            if row["last_stale_message_id"]:
                print(f"[check_stale_updates] Строка {row['block_number']}.{row['row_number']} больше не просрочена, удаляем уведомление")
                await delete_previous_stale_message(row["id"])
            else:
                print(f"[check_stale_updates] Строка {row['block_number']}.{row['row_number']} актуальна")


async def update_coins_from_match(message: discord.Message):
    result = parse_match_won(message)

    if not result:
        return False

    player, coins, gems = result

    # Сначала таблица сервера, где пришло сообщение, потом остальные
    guild_ids = known_guild_ids()
    if message.guild and message.guild.id in guild_ids:
        guild_ids.remove(message.guild.id)
        guild_ids.insert(0, message.guild.id)

    row = None

    for guild_id in guild_ids:
        current_guild_id.set(guild_id)
        row = db.execute(
            """
            SELECT *
            FROM rows
            WHERE LOWER(player) = LOWER(?)
            LIMIT 1
            """,
            (player,)
        ).fetchone()

        if row:
            break

    if not row:
        print(
            f"Игрок {player} найден в Match Won, "
            "но отсутствует в таблице."
        )
        return False

    currency = row["currency"] or "coins"

    if currency == "gems":
        if gems is None:
            print(f"Для игрока {player} выбраны Gems, но Gems в сообщении не найдены.")
            return False
        value = gems
    else:
        if coins is None:
            print(f"Для игрока {player} выбраны Coins, но Coins в сообщении не найдены.")
            return False
        value = coins

    now_iso = datetime.now(timezone.utc).isoformat()
    stale_msg_id = row["last_stale_message_id"]

    if currency == "gems":
        db.execute(
            """
            UPDATE rows
            SET account_gems = ?,
                account_coins_updated_at = ?,
                changed_by = ?,
                changed_at = ?,
                changed = 1
            WHERE id = ?
            """,
            (
                value,
                now_iso,
                "Match Won Bot",
                now_iso,
                row["id"]
            )
        )
    else:
        db.execute(
            """
            UPDATE rows
            SET account_coins = ?,
                account_coins_updated_at = ?,
                changed_by = ?,
                changed_at = ?,
                changed = 1
            WHERE id = ?
            """,
            (
                value,
                now_iso,
                "Match Won Bot",
                now_iso,
                row["id"]
            )
        )

    db.commit()

    print(f"Обновлены {currency} игрока {player}: {value} (время: {now_iso})")

    # Удаляем сообщение о просрочке, если оно было
    if stale_msg_id:
        await delete_previous_stale_message(row["id"])

    await update_table_messages()
    await check_and_notify(row["id"])

    return True


# -----------------------------
# Discord
# -----------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)





def is_manager(interaction: discord.Interaction) -> bool:
    permissions = interaction.user.guild_permissions
    return permissions.administrator or permissions.manage_guild


def can_use_order(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True

    role_id = order_role_id()

    if not role_id:
        return False

    return any(role.id == role_id for role in interaction.user.roles)


async def deny(interaction: discord.Interaction):
    text = "У вас нет прав для использования этой команды."

    if interaction.response.is_done():
        await send_result(interaction, text)
    else:
        await interaction.response.send_message(text, ephemeral=True)


# Последний отправленный текст каждого сообщения таблицы:
# неизменённые блоки не редактируем повторно.
table_message_texts: dict[int, str] = {}

# Чтобы две команды подряд не обновляли таблицу одновременно
# (иначе могут появиться дубли сообщений).
table_update_lock = asyncio.Lock()


async def update_table_messages(force: bool = False):
    """
    Обновляет сообщения таблицы.

    Раньше каждое обновление заново скачивало все сообщения таблицы
    и переписывало каждый блок с паузой 1.5 с, поэтому команды
    отвечали по несколько секунд. Теперь редактируются только
    изменившиеся блоки, без лишних запросов и пауз
    (лимиты Discord discord.py соблюдает сам).
    """
    async with table_update_lock:
        await _update_table_messages(force)


async def _update_table_messages(force: bool):
    channel_id = table_channel_id()

    if not channel_id:
        raise RuntimeError(
            "На этом сервере не выбран канал таблицы. "
            "Выполните /menu set-channel в нужном канале."
        )

    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.NotFound:
            raise RuntimeError(
                "Канал таблицы не существует. Выберите новый: /menu set-channel."
            )
        except discord.Forbidden:
            raise RuntimeError(
                "Бот не имеет доступа к каналу таблицы."
            )

    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(
            "Канал таблицы должен быть текстовым."
        )

    guild_id = current_guild_id.get()

    if guild_id is not None and channel.guild.id != guild_id:
        # Защита: таблица сервера никогда не пишется в канал другого сервера
        raise RuntimeError(
            "Канал таблицы находится на другом сервере. "
            "Выполните /menu set-channel в канале этого сервера."
        )

    permissions = channel.permissions_for(channel.guild.me)

    missing = []

    if not permissions.view_channel:
        missing.append("Просмотр канала")
    if not permissions.send_messages:
        missing.append("Отправка сообщений")
    if not permissions.read_message_history:
        missing.append("Чтение истории сообщений")
    if not permissions.manage_messages:
        missing.append("Управление сообщениями")

    if missing:
        raise RuntimeError(
            "Недостаточно прав в канале: " + ", ".join(missing)
        )

    block_texts = [
        build_table_for_block(block["block_number"])
        for block in blocks()
    ]

    message_ids = [
        item["message_id"]
        for item in db.execute(
            """
            SELECT message_id
            FROM table_messages
            ORDER BY position
            """
        ).fetchall()
    ]

    async def send_new(index: int, text: str) -> int:
        message = await channel.send(content=text)
        db.execute(
            """
            INSERT INTO table_messages(message_id, channel_id, position)
            VALUES (?, ?, ?)
            """,
            (message.id, channel.id, index)
        )
        db.commit()
        table_message_texts[message.id] = text
        return message.id

    # Обновляем/создаём сообщения (по одному на блок)
    for index, text in enumerate(block_texts):
        if index >= len(message_ids):
            message_ids.append(await send_new(index, text))
            continue

        message_id = message_ids[index]

        if not force and table_message_texts.get(message_id) == text:
            continue

        try:
            await channel.get_partial_message(message_id).edit(
                content=text,
                embed=None
            )
            table_message_texts[message_id] = text
        except discord.NotFound:
            # Сообщение таблицы удалили вручную — создаём заново
            db.execute(
                "DELETE FROM table_messages WHERE message_id = ?",
                (message_id,)
            )
            db.commit()
            table_message_texts.pop(message_id, None)
            message_ids[index] = await send_new(index, text)
        except discord.HTTPException as e:
            print(f"Не удалось обновить сообщение {message_id}: {e}")

    # Удаляем лишние сообщения
    for message_id in message_ids[len(block_texts):]:
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            print(
                f"Бот не может удалить сообщение {message_id}. "
                "Нужно право «Управление сообщениями»."
            )
        db.execute(
            "DELETE FROM table_messages WHERE message_id = ?",
            (message_id,)
        )
        table_message_texts.pop(message_id, None)

    db.commit()


async def delete_later(message, seconds: float):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception:
        pass


async def send_result(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            msg = await interaction.followup.send(text[:2000], ephemeral=True, wait=True)
            # Удаляем в фоне, чтобы команда не ждала 5 секунд
            asyncio.create_task(delete_later(msg, delay))
        else:
            if len(text) <= 2000:
                await interaction.response.send_message(text, ephemeral=True, delete_after=delay)
            else:
                await interaction.response.send_message(
                    text[:2000],
                    ephemeral=True,
                    delete_after=delay
                )
    except discord.NotFound:
        pass


# -----------------------------
# /order
# -----------------------------

order_group = app_commands.Group(
    name="order",
    description="Управление заказами"
)


@order_group.command(name="add", description="Добавить заказ")
@app_commands.choices(currency=[
    app_commands.Choice(name="Coins", value="coins"),
    app_commands.Choice(name="Gems", value="gems"),
])
@app_commands.describe(
    row="Номер строки, например 1.1",
    player="Ник игрока",
    before="Количество валюты до заказа",
    paid="Количество оплаченной валюты",
    currency="Валюта: Coins или Gems",
    link="Ссылка на заказ"
)
async def order_add(
    interaction: discord.Interaction,
    row: str,
    player: str,
    before: int,
    paid: int,
    currency: str = "coins",
    link: str = ""
):
    if not can_use_order(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    target = get_row(row)

    if not target:
        return await send_result(
            interaction,
            "Ошибка: указанная строка не существует."
        )

    if is_row_filled(target):
        return await send_result(
            interaction,
            "Эта строка уже занята. Используйте /order change для изменения данных."
        )

    fill_order_row(target["id"], player, before, paid, currency, link, interaction.user)

    await update_table_messages()
    await send_result(interaction, "Заказ добавлен.")


def is_row_filled(target) -> bool:
    return bool(
        target["player"]
        or target["before_coins"]
        or target["paid_coins"]
        or target["before_gems"]
        or target["paid_gems"]
    )


def first_free_row():
    """Первая пустая строка таблицы (по порядку блоков и строк)."""
    return db.execute(
        """
        SELECT rows.*
        FROM rows
        JOIN blocks ON blocks.block_number = rows.block_number
        WHERE rows.player = ''
          AND COALESCE(rows.before_coins, 0) = 0
          AND COALESCE(rows.paid_coins, 0) = 0
          AND COALESCE(rows.before_gems, 0) = 0
          AND COALESCE(rows.paid_gems, 0) = 0
        ORDER BY blocks.position, rows.row_number
        LIMIT 1
        """
    ).fetchone()


def fill_order_row(row_id: int, player: str, before, paid, currency: str, link: str, user):
    """Записывает новый заказ в пустую строку."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        UPDATE rows
        SET player = ?,
            before_coins = ?,
            paid_coins = ?,
            before_gems = ?,
            paid_gems = ?,
            currency = ?,
            account_coins = 0,
            account_gems = 0,
            account_coins_updated_at = ?,
            adjust_coins = 0,
            adjust_gems = ?,
            link = ?,
            changed = 1,
            changed_by = ?,
            changed_at = ?,
            created_by_id = ?,
            ping_sent = 0,
            last_stale_message_id = 0,
            chat_id = '',
            farm_finished = 0,
            mark = ''
        WHERE id = ?
        """,
        (
            player,
            before if currency == "coins" else 0,
            paid if currency == "coins" else 0,
            before if currency == "gems" else 0,
            paid if currency == "gems" else 0,
            currency,
            now_iso,
            0,
            link,
            str(user),
            now_iso,
            str(user.id),
            row_id
        )
    )
    db.commit()


@order_group.command(name="change", description="Изменить заказ")
@app_commands.choices(currency=[
    app_commands.Choice(name="Coins", value="coins"),
    app_commands.Choice(name="Gems", value="gems"),
])
@app_commands.describe(
    row="Номер строки, например 1.1",
    player="Ник игрока",
    before="Количество монет до заказа",
    paid="Количество оплаченных монет",
    link="Ссылка на заказ (опционально)"
)
async def order_change(
    interaction: discord.Interaction,
    row: str,
    player: str,
    before: int,
    paid: int,
    currency: str = "coins",
    link: str = ""
):
    if not can_use_order(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    target = get_row(row)

    if not target:
        return await send_result(
            interaction,
            "Ошибка: указанная строка не существует."
        )

    if not target["player"] and not target["before_coins"] and not target["paid_coins"] and not target["before_gems"] and not target["paid_gems"]:
        return await send_result(
            interaction,
            "Строка пуста. Используйте /order add для создания нового заказа."
        )

    now_iso = datetime.now(timezone.utc).isoformat()

    reset_ping = False
    old_currency = target["currency"] or "coins"
    if (
        old_currency != currency
        or (before if currency == "coins" else 0) != target["before_coins"]
        or (paid if currency == "coins" else 0) != target["paid_coins"]
        or (before if currency == "gems" else 0) != target["before_gems"]
        or (paid if currency == "gems" else 0) != target["paid_gems"]
    ):
        reset_ping = True

    stale_msg_id = target["last_stale_message_id"]

    try:
        db.execute(
            """
            UPDATE rows
            SET player = ?,
                before_coins = ?,
                paid_coins = ?,
                before_gems = ?,
                paid_gems = ?,
                currency = ?,
                link = ?,
                changed = 1,
                changed_by = ?,
                changed_at = ?,
                created_by_id = ?
                {reset_ping_sql}
            WHERE id = ?
            """.format(reset_ping_sql=", ping_sent = 0" if reset_ping else ""),
            (
                player,
                before if currency == "coins" else 0,
                paid if currency == "coins" else 0,
                before if currency == "gems" else 0,
                paid if currency == "gems" else 0,
                currency,
                link,
                str(interaction.user),
                now_iso,
                str(interaction.user.id),
                target["id"]
            )
        )
        db.commit()
        
        # Удаляем сообщение о просрочке, если оно было
        if stale_msg_id:
            await delete_previous_stale_message(target["id"])
    except Exception as e:
        await send_result(interaction, f"Ошибка при обновлении: {e}")
        return

    await update_table_messages()
    await send_result(interaction, "Заказ успешно изменён.")


@order_group.command(name="del", description="Очистить заказ")
async def order_delete(
    interaction: discord.Interaction,
    row: str
):
    if not can_use_order(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    target = get_row(row)

    if not target:
        await send_result(interaction, "Ошибка: указанная строка не существует.")
        return

    await delete_row_notifications(target["id"])

    now_iso = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        UPDATE rows
        SET player = '',
            before_coins = 0,
            paid_coins = 0,
            before_gems = 0,
            paid_gems = 0,
            currency = 'coins',
            account_coins = 0,
            account_gems = 0,
            account_coins_updated_at = ?,
            adjust_coins = 0,
            adjust_gems = 0,
            link = '',
            changed = 1,
            changed_by = ?,
            changed_at = ?,
            created_by_id = '',
            ping_sent = 0,
            last_stale_message_id = 0,
            mark = ''
        WHERE id = ?
        """,
        (
            now_iso,
            str(interaction.user),
            now_iso,
            target["id"]
        )
    )
    db.commit()

    await update_table_messages()
    await send_result(interaction, "Строка очищена.")


@order_group.command(name="finish", description="Завершить заказ: вычесть сумму, очистить, отправить уведомление с пингом создателя")
async def order_finish(
    interaction: discord.Interaction,
    row: str
):
    if not can_use_order(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    target = get_row(row)
    if not target:
        await send_result(interaction, "Ошибка: указанная строка не существует.")
        return

    if not target["player"]:
        await send_result(interaction, "Строка пуста. Нечего завершать.")
        return

    currency = target["currency"] or "coins"
    if currency == "gems":
        total = (target["before_gems"] + target["paid_gems"]) * 1000
        total_balance = (target["account_gems"] or 0) + (target["adjust_gems"] or 0)
        currency_name = "гемов"
    else:
        total = (target["before_coins"] + target["paid_coins"]) * 1000
        total_balance = (target["account_coins"] or 0) + (target["adjust_coins"] or 0)
        currency_name = "монет"
    new_total = total_balance - total
    now_iso = datetime.now(timezone.utc).isoformat()

    player_name = target["player"]
    creator_id = target["created_by_id"] or ""

    await delete_row_notifications(target["id"])

    db.execute(
        """
        UPDATE rows
        SET account_coins = 0,
            account_gems = 0,
            account_coins_updated_at = ?,
            adjust_coins = 0,
            adjust_gems = 0,
            player = '',
            before_coins = 0,
            paid_coins = 0,
            before_gems = 0,
            paid_gems = 0,
            currency = 'coins',
            link = '',
            changed = 1,
            changed_by = ?,
            changed_at = ?,
            created_by_id = '',
            ping_sent = 0,
            last_stale_message_id = 0,
            mark = ''
        WHERE id = ?
        """,
        (
            now_iso,
            str(interaction.user),
            now_iso,
            target["id"]
        )
    )
    db.commit()

    mention = f"<@{creator_id}> " if creator_id else ""
    await interaction.channel.send(
        f"{mention}{player_name} закончил фарм.",
        allowed_mentions=discord.AllowedMentions.all()
    )

    await update_table_messages()

    await send_result(
        interaction,
        f"Заказ {row} завершён. Снято {total} {currency_name}. Остаток баланса: {new_total}."
    )


@order_group.command(name="adjust", description="Изменить ручной баланс валюты")
@app_commands.choices(currency=[
    app_commands.Choice(name="Coins", value="coins"),
    app_commands.Choice(name="Gems", value="gems"),
])
@app_commands.describe(
    row="Номер строки, например 1.1",
    amount="Количество тысяч валюты (положительное - добавить, отрицательное - вычесть)",
    currency="Валюта: Coins или Gems"
)
async def order_adjust(
    interaction: discord.Interaction,
    row: str,
    amount: int,
    currency: str = "coins"
):
    if not can_use_order(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    target = get_row(row)
    if not target:
        await send_result(interaction, "Ошибка: указанная строка не существует.")
        return

    if (target["currency"] or "coins") != currency:
        selected = "Gems" if (target["currency"] or "coins") == "gems" else "Coins"
        await send_result(interaction,
            f"Ошибка: в строке выбрана валюта {selected}."
        )
        return

    current_adjust = (
        target["adjust_gems"] if currency == "gems"
        else target["adjust_coins"]
    ) or 0

    delta = amount * 1000
    new_adjust = current_adjust + delta

    now_iso = datetime.now(timezone.utc).isoformat()
    stale_msg_id = target["last_stale_message_id"]

    if currency == "gems":
        db.execute(
            """
            UPDATE rows
            SET adjust_gems = ?,
                account_coins_updated_at = ?,
                changed = 1,
                changed_by = ?,
                changed_at = ?,
                ping_sent = 0
            WHERE id = ?
            """,
            (new_adjust, now_iso, str(interaction.user), now_iso, target["id"])
        )
    else:
        db.execute(
            """
            UPDATE rows
            SET adjust_coins = ?,
                account_coins_updated_at = ?,
                changed = 1,
                changed_by = ?,
                changed_at = ?,
                ping_sent = 0
            WHERE id = ?
            """,
            (new_adjust, now_iso, str(interaction.user), now_iso, target["id"])
        )

    db.commit()

    # Удаляем сообщение о просрочке, если оно было
    if stale_msg_id:
        await delete_previous_stale_message(target["id"])

    await update_table_messages()
    await check_and_notify(target["id"])

    await send_result(interaction,
        f"Ручной баланс {currency} изменён: {current_adjust} -> {new_adjust} (изменение на {delta})."
    )


@order_group.command(name="ping-add", description="Добавить пользователей в глобальный список для пинга")
@app_commands.describe(
    users="Пользователи для упоминания (через пробел, например @user1 @user2)"
)
async def order_ping_add(
    interaction: discord.Interaction,
    users: str
):
    if not can_use_order(interaction):
        return await deny(interaction)

    user_ids = []
    for word in users.split():
        match = re.search(r'<@!?(\d+)>', word)
        if match:
            user_ids.append(match.group(1))

    if not user_ids:
        return await send_result(interaction, "Не найдено ни одного упоминания пользователя. Используйте @user.")

    current = get_setting("global_ping_users") or ""
    existing = set(current.split()) if current else set()
    existing.update(user_ids)
    new_ping_users = " ".join(existing)

    set_setting("global_ping_users", new_ping_users)

    await send_result(interaction, f"Пользователи для пинга добавлены: {', '.join(f'<@{uid}>' for uid in user_ids)}.")


@order_group.command(name="ping-remove", description="Удалить пользователя из глобального списка пингов")
@app_commands.describe(
    user="Пользователь для удаления (упоминание)"
)
async def order_ping_remove(
    interaction: discord.Interaction,
    user: str
):
    if not can_use_order(interaction):
        return await deny(interaction)

    match = re.search(r'<@!?(\d+)>', user)
    if not match:
        return await send_result(interaction, "Не найдено упоминание пользователя.")
    uid = match.group(1)

    current = get_setting("global_ping_users") or ""
    if not current:
        return await send_result(interaction, "Список пингов пуст.")

    existing = set(current.split())
    if uid not in existing:
        return await send_result(interaction, f"Пользователь <@{uid}> не найден в списке пингов.")

    existing.remove(uid)
    new_ping_users = " ".join(existing)

    set_setting("global_ping_users", new_ping_users)

    await send_result(interaction, f"Пользователь <@{uid}> удалён из списка пингов.")


@order_group.command(name="ping-list", description="Показать глобальный список пользователей для пинга")
async def order_ping_list(interaction: discord.Interaction):
    if not can_use_order(interaction):
        return await deny(interaction)

    ping_users = get_setting("global_ping_users") or ""
    if not ping_users:
        return await send_result(interaction, "Список пингов пуст.")

    user_ids = ping_users.split()
    mentions = ", ".join(f"<@{uid}>" for uid in user_ids)
    await send_result(interaction, f"Глобальные пользователи для пинга: {mentions}")


@order_group.command(name="mark", description="Добавить или удалить статус строки: gems, coins или afk")
@app_commands.choices(mark=[
    app_commands.Choice(name="Gems", value="gems"),
    app_commands.Choice(name="Coins", value="coins"),
    app_commands.Choice(name="AFK", value="afk"),
])
@app_commands.describe(
    row="Номер строки, например 1.1",
    mark="Статус: gems, coins или afk"
)
async def order_mark(
    interaction: discord.Interaction,
    row: str,
    mark: str
):
    if not can_use_order(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    target = get_row(row)

    if not target:
        await send_result(interaction, "Ошибка: указанная строка не существует.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    current_mark = target["mark"] or ""
    marks_list = [m.strip() for m in current_mark.split(",") if m.strip()]
    
    mark_emojis_map = {"gems": "💎", "coins": "💰", "afk": "⏸️"}
    emoji = mark_emojis_map.get(mark, "")

    if mark in marks_list:
        marks_list.remove(mark)
        new_mark = ",".join(marks_list)
        db.execute(
            """
            UPDATE rows
            SET mark = ?,
                changed = 1,
                changed_by = ?,
                changed_at = ?
            WHERE id = ?
            """,
            (
                new_mark,
                str(interaction.user),
                now_iso,
                target["id"]
            )
        )
        db.commit()
        await send_result(interaction, f"Маркер {emoji} {mark} удалён со строки {row}.")
    else:
        marks_list.append(mark)
        new_mark = ",".join(marks_list)
        db.execute(
            """
            UPDATE rows
            SET mark = ?,
                changed = 1,
                changed_by = ?,
                changed_at = ?
            WHERE id = ?
            """,
            (
                new_mark,
                str(interaction.user),
                now_iso,
                target["id"]
            )
        )
        db.commit()
        await send_result(interaction, f"Маркер {emoji} {mark} добавлен строке {row}.")

    await update_table_messages()


@order_group.command(name="stale-time", description="Установить время до уведомления о неактивности (в минутах)")
@app_commands.describe(
    minutes="Время в минутах (по умолчанию 30)"
)
async def order_stale_time(
    interaction: discord.Interaction,
    minutes: int
):
    if not is_manager(interaction):
        return await deny(interaction)

    if minutes < 1 or minutes > 1440:
        return await send_result(
            interaction,
            "Время должно быть от 1 до 1440 минут (1 день)."
        )

    set_setting("stale_minutes", str(minutes))
    await send_result(interaction, f"Время до уведомления об неактивности изменено: {minutes} минут.")


bot.tree.add_command(order_group)


# -----------------------------
# /menu
# -----------------------------

menu_group = app_commands.Group(
    name="menu",
    description="Управление структурой таблицы"
)


@menu_group.command(name="add-row", description="Добавить строку")
async def menu_add_row(
    interaction: discord.Interaction,
    block: int,
    position: int
):
    if not is_manager(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    try:
        add_row(block, position)
        await update_table_messages()
        await send_result(interaction, "Строка добавлена.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="del-row", description="Удалить строку")
async def menu_del_row(
    interaction: discord.Interaction,
    row: str,
    confirm: bool = False
):
    if not is_manager(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    target = get_row(row)

    if not target:
        await send_result(interaction, "Строка не существует.")
        return

    filled = bool(
        target["player"]
        or target["before_coins"]
        or target["paid_coins"]
        or target["before_gems"]
        or target["paid_gems"]
    )

    if filled and not confirm:
        await send_result(interaction,
            "Строка заполнена. Для удаления укажите confirm:true."
        )
        return

    await delete_row_notifications(target["id"])

    try:
        delete_row(row)
        await update_table_messages()
        await send_result(interaction, "Строка удалена.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="add-block", description="Добавить блок")
async def menu_add_block(
    interaction: discord.Interaction,
    position: int,
    rows: int
):
    if not is_manager(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    try:
        add_block(position, rows)
        await update_table_messages()
        await send_result(interaction, "Блок добавлен.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="del-block", description="Удалить блок")
async def menu_del_block(
    interaction: discord.Interaction,
    block: int,
    confirm: bool = True
):
    if not is_manager(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    block_row_ids = [
        item["id"]
        for item in db.execute(
            "SELECT id FROM rows WHERE block_number = ?",
            (block,)
        ).fetchall()
    ]

    try:
        await delete_row_notifications(*block_row_ids)
        delete_block(block)
        await update_table_messages()
        await send_result(interaction, "Блок удалён.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="edit-row", description="Изменить количество строк")
async def menu_edit_row(
    interaction: discord.Interaction,
    block: int,
    rows: int,
    confirm: bool = True
):
    if not is_manager(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    current = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM rows
        WHERE block_number = ?
        """,
        (block,)
    ).fetchone()["count"]

    if not current:
        await send_result(interaction, "Такого блока не существует.")
        return

    if rows < current:
        filled = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM rows
            WHERE block_number = ?
              AND row_number > ?
              AND (
                  player != ''
                  OR before_coins != 0
                  OR paid_coins != 0
                  OR before_gems != 0
                  OR paid_gems != 0
              )
            """,
            (block, rows)
        ).fetchone()["count"]

        if filled and not confirm:
            await send_result(interaction,
                "Удаляемые строки заполнены. Повторите с confirm:true."
            )
            return

    try:
        resize_block(block, rows)
        await update_table_messages()
        await send_result(interaction, "Количество строк изменено.")
    except ValueError as error:
        await send_result(interaction, str(error))


@menu_group.command(name="show", description="Показать таблицу и настройки")
async def menu_show(interaction: discord.Interaction):
    if not is_manager(interaction):
        return await deny(interaction)

    block_count = db.execute(
        "SELECT COUNT(*) AS count FROM blocks"
    ).fetchone()["count"]

    row_count = db.execute(
        "SELECT COUNT(*) AS count FROM rows"
    ).fetchone()["count"]

    ping_users = get_setting("global_ping_users") or "(пусто)"
    stale_minutes = get_setting("stale_minutes") or "30"

    text = (
        f"Блоков: {block_count}\n"
        f"Строк: {row_count}\n"
        f"Канал таблицы: <#{table_channel_id()}>\n"
        f"Канал просрочек: <#{stale_channel_id()}>\n"
        f"Роль для /order: {order_role_id() or '(только админы)'}\n"
        f"Сообщений таблицы: {db.execute('SELECT COUNT(*) AS count FROM table_messages').fetchone()['count']}\n"
        f"Пользователи для пинга: {ping_users}\n"
        f"Время уведомления: {stale_minutes} минут"
    )

    await send_result(interaction, text)


@menu_group.command(name="refresh", description="Обновить таблицу")
async def menu_refresh(interaction: discord.Interaction):
    if not is_manager(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    await update_table_messages(force=True)
    await send_result(interaction, "Таблица обновлена.")


@menu_group.command(name="reset", description="Сбросить таблицу")
async def menu_reset(
    interaction: discord.Interaction,
    confirm: bool = False
):
    if not is_manager(interaction):
        return await deny(interaction)

    if not confirm:
        return await send_result(
            interaction,
            "Все данные будут удалены. Повторите команду с confirm:true."
        )

    await interaction.response.defer(ephemeral=True)

    await delete_row_notifications(
        *(item["id"] for item in db.execute("SELECT id FROM rows").fetchall())
    )

    db.execute("DELETE FROM rows")
    db.execute("DELETE FROM blocks")
    db.commit()

    initialize_table()
    await update_table_messages()
    await send_result(interaction, "Таблица сброшена.")


@menu_group.command(name="set-channel", description="Сделать этот канал каналом таблицы на этом сервере")
async def menu_set_channel(interaction: discord.Interaction):
    if not is_manager(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    # Удаляем сообщения таблицы из старого канала
    old_channel_id = table_channel_id()
    for item in db.execute("SELECT message_id FROM table_messages").fetchall():
        if old_channel_id and old_channel_id != interaction.channel_id:
            await delete_bot_message(old_channel_id, item["message_id"])
        table_message_texts.pop(item["message_id"], None)

    db.execute("DELETE FROM table_messages")
    set_setting("table_channel_id", str(interaction.channel_id))

    await update_table_messages(force=True)
    await send_result(interaction, "Теперь таблица этого сервера в этом канале.")


@menu_group.command(name="set-sales-channel", description="Сюда будут приходить карточки новых продаж FunPay")
async def menu_set_sales_channel(interaction: discord.Interaction):
    if not is_manager(interaction):
        return await deny(interaction)

    # Очередь продаж одна (в базе основного сервера), поэтому и канал там
    with use_guild(None):
        set_setting("sales_channel_id", str(interaction.channel_id))

    await send_result(interaction, "Карточки новых продаж FunPay будут приходить в этот канал.")


@menu_group.command(name="set-stale-channel", description="Сделать этот канал каналом уведомлений о просрочке")
async def menu_set_stale_channel(interaction: discord.Interaction):
    if not is_manager(interaction):
        return await deny(interaction)

    set_setting("stale_channel_id", str(interaction.channel_id))
    await send_result(interaction, "Уведомления о просрочке будут приходить в этот канал.")


@menu_group.command(name="set-role", description="Роль, которой разрешены команды /order на этом сервере")
@app_commands.describe(role="Роль для команд /order")
async def menu_set_role(interaction: discord.Interaction, role: discord.Role):
    if not is_manager(interaction):
        return await deny(interaction)

    set_setting("order_role_id", str(role.id))
    await send_result(interaction, f"Команды /order доступны роли {role.mention}.")


bot.tree.add_command(menu_group)


async def set_interaction_guild(interaction: discord.Interaction) -> bool:
    """Каждая команда работает с таблицей своего сервера."""
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Команды работают только на сервере.",
            ephemeral=True
        )
        return False

    current_guild_id.set(interaction.guild_id)
    return True


async def on_command_error(interaction: discord.Interaction, error: Exception):
    original = getattr(error, "original", error)
    print(f"Ошибка команды: {original!r}")

    try:
        await send_result(interaction, f"Ошибка: {original}")
    except Exception:
        pass


bot.tree.interaction_check = set_interaction_guild
bot.tree.on_error = on_command_error


# -----------------------------
# Продажи FunPay: карточка с кнопкой «В таблицу»
# -----------------------------
#
# FunPay-бот (FunPay_sales.py) кладёт оплаченные продажи в pending_sales
# базы основного сервера. Здесь они публикуются карточками в канал продаж
# (по умолчанию — канал таблицы основного сервера). Кнопка открывает
# форму с уже заполненными ссылкой, количеством и валютой, и заказ
# записывается в таблицу того сервера, где нажали кнопку.

SALES_CHECK_SECONDS = 10


def sale_link(order_id: str) -> str:
    return f"https://funpay.com/orders/{order_id}/"


def get_pending_sale(order_id: str):
    with use_guild(None):
        return db.execute(
            "SELECT * FROM pending_sales WHERE order_id = ?",
            (order_id,)
        ).fetchone()


def update_pending_sale(order_id: str, **fields):
    columns = ", ".join(f"{name} = ?" for name in fields)

    with use_guild(None):
        db.execute(
            f"UPDATE pending_sales SET {columns} WHERE order_id = ?",
            (*fields.values(), order_id)
        )
        db.commit()


def format_amount(value) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def parse_amount(text: str):
    """'50', '50к', '27.965', '27,965' -> число (монеты — в тысячах)."""
    clean = (
        text.strip()
        .lower()
        .replace(" ", "")
        .replace("\u00a0", "")
        .replace(",", ".")
        .rstrip("кk")
    )
    value = float(clean)

    if value < 0:
        raise ValueError

    return int(value) if value.is_integer() else value


def parse_currency(text: str) -> Optional[str]:
    value = text.strip().lower()

    if value in ("coins", "coin", "монеты", "монета", "м", "c"):
        return "coins"

    if value in ("gems", "gem", "гемы", "гем", "г", "g"):
        return "gems"

    return None


def sale_paid_text(sale) -> Optional[str]:
    amount = sale["amount"] or 0

    if not amount:
        return None

    if sale["currency"] == "gems":
        return str(amount)

    # Монеты в таблице хранятся в тысячах: 50 000 шт. -> 50
    return format_amount(amount / 1000)


def build_sale_embed(sale) -> discord.Embed:
    gems = sale["currency"] == "gems"
    status = sale["status"]

    embed = discord.Embed(
        title=f"🛒 Продажа #{sale['order_id']}",
        url=sale_link(sale["order_id"]),
        color=0x2ECC71 if status == "new" else 0x95A5A6
    )

    amount = f"{sale['amount']:,}".replace(",", " ") if sale["amount"] else "?"

    embed.add_field(name="Покупатель", value=sale["buyer"] or "—")
    embed.add_field(
        name="Количество",
        value=f"{amount} шт. ({'💎 гемы' if gems else '💰 монеты'})"
    )
    embed.add_field(name="Сумма", value=sale["price"] or "—")
    embed.add_field(
        name="Ник в игре",
        value=sale["player"] or "пока неизвестен — покупатель может написать /info ник в чат FunPay",
        inline=False
    )

    if status == "added":
        embed.set_footer(text=f"✅ Записано в строку {sale['row_name']} — {sale['done_by']}")
    elif status == "skipped":
        embed.set_footer(text=f"Пропущено — {sale['done_by']}")

    return embed


def sale_view(order_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SaleButton("add", order_id))
    view.add_item(SaleButton("skip", order_id))
    return view


async def edit_sale_card(sale, with_buttons: bool):
    if not sale["card_message_id"]:
        return

    try:
        channel = bot.get_channel(sale["card_channel_id"])
        if channel is None:
            channel = await bot.fetch_channel(sale["card_channel_id"])

        await channel.get_partial_message(sale["card_message_id"]).edit(
            embed=build_sale_embed(sale),
            view=sale_view(sale["order_id"]) if with_buttons else None
        )
    except discord.HTTPException as error:
        print(f"[sales] Не удалось обновить карточку #{sale['order_id']}: {error}")


class SaleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sale:(?P<action>add|skip):(?P<order_id>[A-Za-z0-9]+)"
):
    """Кнопки карточки. Работают и после перезапуска бота."""

    def __init__(self, action: str, order_id: str):
        if action == "add":
            button = discord.ui.Button(
                label="В таблицу",
                emoji="➕",
                style=discord.ButtonStyle.success,
                custom_id=f"sale:add:{order_id}"
            )
        else:
            button = discord.ui.Button(
                label="Пропустить",
                emoji="✖️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"sale:skip:{order_id}"
            )

        super().__init__(button)
        self.action = action
        self.order_id = order_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["order_id"])

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return

        current_guild_id.set(interaction.guild_id)

        if not can_use_order(interaction):
            return await deny(interaction)

        sale = get_pending_sale(self.order_id)

        if sale is None or sale["status"] != "new":
            return await interaction.response.send_message(
                "Эта продажа уже обработана.",
                ephemeral=True
            )

        if self.action == "skip":
            update_pending_sale(
                self.order_id,
                status="skipped",
                done_by=str(interaction.user)
            )
            await interaction.response.edit_message(
                embed=build_sale_embed(get_pending_sale(self.order_id)),
                view=None
            )
            return

        await interaction.response.send_modal(SaleModal(sale))


class SaleModal(discord.ui.Modal):
    def __init__(self, sale):
        super().__init__(title=f"Заказ #{sale['order_id']} в таблицу", timeout=600)

        self.order_id = sale["order_id"]
        unit = "" if sale["currency"] == "gems" else " (в тысячах)"

        self.player = discord.ui.TextInput(
            label="Ник в игре",
            default=sale["player"] or None,
            max_length=100
        )
        self.paid = discord.ui.TextInput(
            label=f"Оплачено{unit}",
            default=sale_paid_text(sale),
            max_length=20
        )
        self.before = discord.ui.TextInput(
            label=f"До заказа{unit}",
            placeholder="Баланс аккаунта до заказа",
            max_length=20
        )
        self.currency = discord.ui.TextInput(
            label="Валюта: coins или gems",
            default=sale["currency"],
            max_length=10
        )
        self.row = discord.ui.TextInput(
            label="Строка (пусто — первая свободная)",
            placeholder="например 1.2",
            required=False,
            max_length=10
        )

        for item in (self.player, self.paid, self.before, self.currency, self.row):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        current_guild_id.set(interaction.guild_id)

        async def fail(text: str):
            await interaction.response.send_message(text, ephemeral=True)

        sale = get_pending_sale(self.order_id)

        if sale is None or sale["status"] != "new":
            return await fail("Эта продажа уже обработана.")

        player = self.player.value.strip()
        if not player:
            return await fail("Укажите ник в игре.")

        currency = parse_currency(self.currency.value)
        if currency is None:
            return await fail("Валюта должна быть coins или gems.")

        try:
            paid = parse_amount(self.paid.value)
            before = parse_amount(self.before.value)
        except ValueError:
            return await fail("«Оплачено» и «До заказа» должны быть числами.")

        row_name = self.row.value.strip()

        if row_name:
            target = get_row(row_name)
            if not target:
                return await fail(f"Строки {row_name} нет в таблице.")
        else:
            target = first_free_row()
            if not target:
                return await fail("Свободных строк нет. Добавьте строку: /menu add-row.")

        row_name = f"{target['block_number']}.{target['row_number']}"

        if is_row_filled(target):
            return await fail(f"Строка {row_name} уже занята.")

        await interaction.response.defer(ephemeral=True)

        fill_order_row(
            target["id"],
            player,
            before,
            paid,
            currency,
            sale_link(self.order_id),
            interaction.user
        )

        update_pending_sale(
            self.order_id,
            status="added",
            player=player,
            done_by=str(interaction.user),
            row_name=row_name
        )
        await edit_sale_card(get_pending_sale(self.order_id), with_buttons=False)

        await update_table_messages()
        await send_result(interaction, f"Заказ #{self.order_id} записан в строку {row_name}.")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[sales] Ошибка формы #{self.order_id}: {error!r}")
        try:
            await send_result(interaction, f"Ошибка: {error}")
        except Exception:
            pass


bot.add_dynamic_items(SaleButton)


async def publish_sale_cards():
    with use_guild(None):
        sales = db.execute(
            """
            SELECT *
            FROM pending_sales
            WHERE status = 'new'
              AND (card_message_id = 0 OR card_player != player)
            ORDER BY created_at
            """
        ).fetchall()

        if not sales:
            return

        channel_id = guild_setting_int("sales_channel_id", 0) or table_channel_id()

    if not channel_id:
        print("[sales] Канал для карточек продаж не задан (/menu set-sales-channel)")
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    for sale in sales:
        if sale["card_message_id"]:
            # Покупатель прислал ник — обновляем карточку
            await edit_sale_card(sale, with_buttons=True)
        else:
            message = await channel.send(
                embed=build_sale_embed(sale),
                view=sale_view(sale["order_id"])
            )
            update_pending_sale(
                sale["order_id"],
                card_channel_id=channel.id,
                card_message_id=message.id
            )

        update_pending_sale(sale["order_id"], card_player=sale["player"])


async def sales_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            await publish_sale_cards()
        except Exception as error:
            print(f"[sales] Ошибка публикации продаж: {error!r}")

        try:
            await process_closed_orders()
        except Exception as error:
            print(f"[closed_orders] Ошибка: {error!r}")

        await asyncio.sleep(SALES_CHECK_SECONDS)


# -----------------------------
# Обработчик реакций (удаление уведомлений и очистка строки)
# -----------------------------

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    if payload.guild_id is None or payload.guild_id not in known_guild_ids():
        return

    current_guild_id.set(payload.guild_id)

    if str(payload.emoji) != "✅":
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    if message.author.id != bot.user.id:
        return

    content = message.content

    # Раньше текст сверялся с текущей настройкой stale-time и словом
    # «монеты»/«гемы»: после смены времени старые уведомления
    # переставали распознаваться и не удалялись.
    is_finish = "завершил фарм" in content
    is_stale = re.search(r"не обновлял \w+ более \d+ минут", content) is not None

    if not is_finish and not is_stale:
        return

    # Удалять могут пингуемые пользователи и те, у кого есть
    # «Управление сообщениями» (раньше при пустом списке пингов
    # уведомления о просрочке не мог удалить никто).
    user_ids = (get_setting("global_ping_users") or "").split()
    member = payload.member
    can_manage = (
        member is not None
        and hasattr(channel, "permissions_for")
        and channel.permissions_for(member).manage_messages
    )

    if str(payload.user_id) not in user_ids and not can_manage:
        return

    match = re.search(r"строка (\d+\.\d+)", content)
    target = get_row(match.group(1)) if match else None

    if is_finish:
        # Теперь строка освобождается сразу, когда аккаунт нафармил, и в ней
        # уже может быть новый заказ — её не трогаем. Очищаем только строку
        # из старого уведомления (до обновления), которое к ней привязано.
        if target and target["finish_message_id"] == message.id:
            await delete_row_notifications(target["id"])
            clear_row_by_id(target["id"])
            print(f"Строка {match.group(1)} очищена по реакции от {payload.user_id}")
            await update_table_messages()

        db.execute(
            "DELETE FROM finished_orders WHERE message_id = ?",
            (message.id,)
        )
        db.commit()

    if is_stale and target and target["last_stale_message_id"] == message.id:
        db.execute(
            "UPDATE rows SET last_stale_message_id = 0 WHERE id = ?",
            (target["id"],)
        )
        db.commit()
        print(f"Сброшен ID уведомления для строки {match.group(1)} по реакции от {payload.user_id}")

    # Удаляем сообщение (для обоих типов)
    try:
        await message.delete()
        print(f"Сообщение удалено по реакции от {payload.user_id}")
    except discord.NotFound:
        pass
    except discord.Forbidden:
        print("Нет прав на удаление сообщения")
    except Exception as e:
        print(f"Ошибка удаления сообщения: {e}")


# -----------------------------
# Фоновая задача для проверки устаревших монет
# -----------------------------

async def stale_check_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild_id in known_guild_ids():
            try:
                with use_guild(guild_id):
                    await check_stale_updates()
            except Exception as e:
                print(f"[stale_check_loop] Ошибка (сервер {guild_id}): {e}")
        await asyncio.sleep(300)  # проверка каждые 10 минут


# -----------------------------
# Запуск
# -----------------------------

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Собираем и обычный текст, и все части embed, включая fields.
    text_parts = []
    if message.content:
        text_parts.append(message.content)

    for embed in message.embeds:
        if embed.title:
            text_parts.append(embed.title)
        if embed.description:
            text_parts.append(embed.description)
        if embed.footer and embed.footer.text:
            text_parts.append(embed.footer.text)
        for field in embed.fields:
            text_parts.append(field.name)
            text_parts.append(field.value)

    all_text = "\n".join(text_parts).lower()

    # Обрабатываем и победу, и поражение. Баланс аккаунта обновляется
    # по выбранной в строке валюте (Coins или Gems).
    if "match won" in all_text or "match lost" in all_text:
        try:
            updated = await update_coins_from_match(message)

            if updated:
                print("Количество валюты успешно обновлено.")

        except Exception as error:
            print(f"Ошибка обработки Match Won/Lost: {error}")

    await bot.process_commands(message)


@bot.event
async def on_ready():
    global MAIN_GUILD_ID

    table_channel = bot.get_channel(TABLE_CHANNEL_ID)

    if table_channel is not None and MAIN_GUILD_ID != table_channel.guild.id:
        MAIN_GUILD_ID = table_channel.guild.id
        with use_guild(None):
            set_setting("guild_id", str(MAIN_GUILD_ID))
        print(f"Основной сервер таблицы: {table_channel.guild.name} ({MAIN_GUILD_ID})")

    print(f"Бот запущен: {bot.user}")
    print(f"TABLE_CHANNEL_ID: {TABLE_CHANNEL_ID}")
    print(f"STALE_CHANNEL_ID: {STALE_CHANNEL_ID}")

    channel = bot.get_channel(TABLE_CHANNEL_ID)

    if channel is None:
        print("Канал таблицы не найден через кеш Discord.")
    else:
        print(
            f"Канал таблицы найден: #{channel.name}, "
            f"сервер: {channel.guild.name}, "
            f"тип: {channel.type}"
        )

        permissions = channel.permissions_for(channel.guild.me)

        print("Права бота в канале таблицы:")
        print(f"  view_channel: {permissions.view_channel}")
        print(f"  send_messages: {permissions.send_messages}")
        print(f"  embed_links: {permissions.embed_links}")
        print(f"  read_history: {permissions.read_message_history}")
        print(f"  manage_messages: {permissions.manage_messages}")

    # Проверяем канал для просрочек
    if STALE_CHANNEL_ID:
        stale_channel = bot.get_channel(STALE_CHANNEL_ID)
        if stale_channel is None:
            print("Канал просрочек не найден через кеш Discord.")
        else:
            print(
                f"Канал просрочек найден: #{stale_channel.name}, "
                f"сервер: {stale_channel.guild.name}, "
                f"тип: {stale_channel.type}"
            )
    else:
        print("STALE_CHANNEL_ID не указан в .env - уведомления о просрочке не будут отправляться")

    try:
        await bot.tree.sync()
        print("Slash-команды синхронизированы.")
    except Exception as error:
        print(f"Ошибка синхронизации команд: {error}")

    for guild_id in known_guild_ids():
        try:
            with use_guild(guild_id):
                await update_table_messages()
        except Exception as error:
            print(f"Ошибка обновления таблицы (сервер {guild_id}): {error}")

    # Запускаем фоновую проверку устаревших монет
    if not getattr(bot, "background_started", False):
        bot.background_started = True
        bot.loop.create_task(stale_check_loop())
        bot.loop.create_task(sales_loop())


if not TOKEN:
    raise RuntimeError("В .env не указан DISCORD_TOKEN")

if not TABLE_CHANNEL_ID:
    raise RuntimeError("В .env не указан TABLE_CHANNEL_ID")


init_db()
bot.run(TOKEN)
