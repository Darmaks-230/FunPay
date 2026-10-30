"""
Новые продажи FunPay -> очередь для кнопки «В таблицу» в Discord.

FunPay-бот записывает каждую оплаченную продажу в таблицу
pending_sales базы orders.sqlite3. Бот таблицы (main.py) берёт
оттуда продажи и публикует карточку с кнопкой; по кнопке
открывается форма с уже заполненными ссылкой, количеством и валютой.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from funpaybotengine import Router
from funpaybotengine.dispatching.events import NewSaleEvent
from funpaybotengine.types import Message


# Без импорта funpay_shared: main.py берёт отсюда схему таблицы,
# и ему не нужен FunPay-клиент.
DATABASE_PATH = Path(__file__).resolve().parent / "orders.sqlite3"

router = Router(name="sales")


CREATE_PENDING_SALES = """
CREATE TABLE IF NOT EXISTS pending_sales (
    order_id TEXT PRIMARY KEY,
    buyer TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'coins',
    amount INTEGER NOT NULL DEFAULT 0,
    price TEXT NOT NULL DEFAULT '',
    player TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    card_channel_id INTEGER NOT NULL DEFAULT 0,
    card_message_id INTEGER NOT NULL DEFAULT 0,
    card_player TEXT NOT NULL DEFAULT '',
    done_by TEXT NOT NULL DEFAULT '',
    row_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    form_data TEXT NOT NULL DEFAULT ''
)
"""


# Закрытые заказы: подтверждены (покупателем или администратором)
# или по ним сделан возврат. main.py по ним убирает аккаунт из таблицы
# и удаляет сообщение «завершил фарм».
CREATE_CLOSED_ORDERS = """
CREATE TABLE IF NOT EXISTS closed_orders (
    order_id TEXT PRIMARY KEY,
    processed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT 'confirmed'
)
"""

# Причины закрытия заказа
CLOSE_REASONS = {
    "confirmed": "Заказ подтверждён покупателем",
    "closed_by_admin": "Заказ подтверждён администратором",
    "refunded": "Возврат по заказу",
}


def ensure_pending_sales_columns(connection) -> None:
    """
    form_data — данные формы заказа, поставленного в очередь
    (у того же аккаунта уже идёт фарм). В базе от прошлой версии
    этого столбца нет.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(pending_sales)")}

    if "form_data" not in columns:
        connection.execute(
            "ALTER TABLE pending_sales ADD COLUMN form_data TEXT NOT NULL DEFAULT ''"
        )


def ensure_closed_orders_columns(connection) -> None:
    """В базе от прошлой версии у closed_orders нет столбца reason."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(closed_orders)")}

    if "reason" not in columns:
        connection.execute(
            "ALTER TABLE closed_orders ADD COLUMN reason TEXT NOT NULL DEFAULT 'confirmed'"
        )

# Аккаунты, которые нафармили (их строка уже освобождена).
# Есть в базе каждого сервера: по ним удаляется сообщение «завершил фарм»
# и покупателю на FunPay уходит сообщение о конце фарма.
CREATE_FINISHED_ORDERS = """
CREATE TABLE IF NOT EXISTS finished_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL DEFAULT '',
    player TEXT NOT NULL DEFAULT '',
    row_name TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'coins',
    balance REAL NOT NULL DEFAULT 0,
    goal REAL NOT NULL DEFAULT 0,
    chat_id TEXT NOT NULL DEFAULT '',
    channel_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0,
    buyer_notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ''
)
"""


def order_id_from_link(link: str) -> str:
    """'https://funpay.com/orders/RFZLSDVU/' -> 'RFZLSDVU'"""
    match = re.search(r"/orders/([A-Za-z0-9]+)", link or "")
    return match.group(1) if match else ""


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.execute(CREATE_PENDING_SALES)
    ensure_pending_sales_columns(connection)
    connection.execute(CREATE_CLOSED_ORDERS)
    ensure_closed_orders_columns(connection)
    return connection


def parse_sale_title(title: str) -> tuple[str, int]:
    """
    Валюта и количество из названия лота, например:
    '💰ФАРМ МОНЕТ💰|...|⏳ФАРМ 24/7⏳, Услуги, 50 000 шт.' -> ('coins', 50000)
    """

    upper_title = (title or "").upper()

    if "ОПЫТ" in upper_title or re.search(r"\b(XP|EXP)\b", upper_title):
        currency = "xp"
    elif "ГЕМ" in upper_title:
        currency = "gems"
    else:
        currency = "coins"

    match = re.search(r"(\d[\d\s ]*)\s*шт\.?\s*$", title or "")
    amount = int(re.sub(r"\D", "", match.group(1))) if match else 0

    return currency, amount


def save_sale(
    order_id: str,
    buyer: str,
    chat_id: str,
    title: str,
    price: str,
) -> None:
    currency, amount = parse_sale_title(title)

    with connect() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO pending_sales(
                order_id, buyer, chat_id, title, currency,
                amount, price, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                buyer,
                chat_id,
                title,
                currency,
                amount,
                price,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def set_pending_player(chat_id: str, player: str) -> None:
    """
    Покупатель написал в чат /info ник или /money ник —
    подставляем ник в его ещё не записанные продажи.
    """

    with connect() as connection:
        connection.execute(
            """
            UPDATE pending_sales
            SET player = ?
            WHERE chat_id = ? AND status = 'new'
            """,
            (player, str(chat_id)),
        )


@router.on_new_sale(as_task=True)
async def handle_new_sale(event: NewSaleEvent):
    order_id = event.object.meta.order_id
    chat_id = event.object.chat_id

    try:
        order = await event.get_order_preview()
    except Exception:
        logging.exception("Не удалось получить заказ #%s", order_id)
        return

    price = f"{order.total.value:g} {order.total.character}" if order.total else ""

    try:
        save_sale(
            order_id=order.id,
            buyer=order.counterparty.username if order.counterparty else "",
            chat_id=str(chat_id or ""),
            title=order.title,
            price=price,
        )
    except sqlite3.Error:
        logging.exception("Не удалось сохранить продажу #%s", order_id)
        return

    logging.info(
        "Новая продажа #%s: %s (%s)",
        order.id,
        order.title,
        price,
    )


def classify_order_message(text: str) -> tuple[str, str] | None:
    """
    Системное сообщение FunPay -> (номер заказа, причина закрытия).

    Свой разбор вместо событий библиотеки: она не узнаёт возврат,
    сделанный администратором («Администратор … вернул деньги …»).
    Частичный возврат заказ не закрывает — его не трогаем.
    """

    lower = (text or "").strip().lower()
    match = re.search(r"#([A-Za-z0-9]+)", text or "")

    if not match:
        return None

    if (
        "подтвердил успешное выполнение заказа" in lower
        or "has confirmed that order" in lower
    ):
        by_admin = lower.startswith(("администратор", "the administrator"))
        return match.group(1), "closed_by_admin" if by_admin else "confirmed"

    if "вернул деньги покупателю" in lower or "has refunded the buyer" in lower:
        return match.group(1), "refunded"

    return None


@router.on_new_message(as_task=True)
async def handle_order_closed(message: Message):
    # Только системные сообщения FunPay (у них отправитель с ID 0),
    # чтобы покупатель не мог закрыть заказ, напечатав такой текст в чат.
    if message.sender_id != 0:
        return

    result = classify_order_message(message.text)

    if result is None:
        return

    order_id, reason = result

    try:
        with connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO closed_orders(order_id, created_at, reason)
                VALUES (?, ?, ?)
                """,
                (order_id, datetime.now(timezone.utc).isoformat(), reason),
            )
    except sqlite3.Error:
        logging.exception("Не удалось сохранить закрытие заказа #%s", order_id)
        return

    logging.info("%s: #%s", CLOSE_REASONS[reason], order_id)
