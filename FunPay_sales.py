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
from datetime import datetime, timedelta, timezone
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
    form_data TEXT NOT NULL DEFAULT '',
    short_id TEXT NOT NULL DEFAULT ''
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
    (у того же аккаунта уже идёт фарм); short_id — короткий номер
    заказа Starvell для показа. В базе от прошлых версий этих столбцов нет.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(pending_sales)")}

    for column in ("form_data", "short_id"):
        if column not in columns:
            connection.execute(
                f"ALTER TABLE pending_sales ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
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


# Заказы и чаты Starvell — UUID (с дефисами), FunPay — буквы и цифры
STARVELL_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def is_starvell_id(value) -> bool:
    return bool(re.fullmatch(STARVELL_ID, str(value or "").strip().lower()))


def order_id_from_link(link: str) -> str:
    """
    'https://funpay.com/orders/RFZLSDVU/' -> 'RFZLSDVU'
    'https://starvell.com/order/01a1970a-...' -> '01a1970a-...'
    """
    match = re.search(rf"starvell\.com/order/({STARVELL_ID})", (link or "").lower())
    if match:
        return match.group(1)

    match = re.search(r"/orders/([A-Za-z0-9]+)", link or "")
    return match.group(1) if match else ""


def find_order_for_chat(connection, chat_id, player: str, currency: str = "") -> str:
    """
    Номер заказа по чату покупателя (для строк со ссылкой на чат).

    У покупателя может быть несколько заказов на один аккаунт (например,
    монеты и гемы), поэтому подбираем по нику и валюте строки:
    1. ник и валюта совпали;
    2. единственный заказ в этой валюте;
    3. ник совпал;
    4. единственная свежая продажа в чате.
    Если однозначно не выходит — не угадываем.
    """

    if not chat_id:
        return ""

    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    sales = connection.execute(
        """
        SELECT order_id, player, currency
        FROM pending_sales
        WHERE chat_id = ?
          AND status IN ('new', 'added', 'queued')
          AND created_at >= ?
        ORDER BY created_at DESC
        """,
        (str(chat_id), since),
    ).fetchall()

    def same_player(sale) -> bool:
        return bool(player) and (sale[1] or "").lower() == player.lower()

    def same_currency(sale) -> bool:
        return bool(currency) and sale[2] == currency

    for sale in sales:
        if same_player(sale) and same_currency(sale):
            return sale[0]

    by_currency = [sale for sale in sales if same_currency(sale)]
    if len(by_currency) == 1:
        return by_currency[0][0]

    for sale in sales:
        if same_player(sale):
            return sale[0]

    return sales[0][0] if len(sales) == 1 else ""


def order_link(order_id: str) -> str:
    if is_starvell_id(order_id):
        return f"https://starvell.com/order/{order_id}"
    return f"https://funpay.com/orders/{order_id}/"


def chat_link(chat_id) -> str:
    if is_starvell_id(chat_id):
        return f"https://starvell.com/chat/{chat_id}"
    return f"https://funpay.com/chat/?node={chat_id}"


def order_link_patterns(order_id: str) -> tuple[str, str]:
    """Шаблоны LIKE для поиска строк таблицы со ссылкой на заказ."""
    if is_starvell_id(order_id):
        return f"%/order/{order_id}%", f"%/order/{order_id.upper()}%"
    return f"%/orders/{order_id}/%", f"%/orders/{order_id}"


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
    short_id: str = "",
) -> None:
    currency, amount = parse_sale_title(title)

    with connect() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO pending_sales(
                order_id, buyer, chat_id, title, currency,
                amount, price, created_at, short_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                short_id,
            ),
        )


def close_order(order_id: str, reason: str) -> bool:
    """Заказ закрыт — main.py уберёт его из таблицы. True, если запись новая."""
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO closed_orders(order_id, created_at, reason)
            VALUES (?, ?, ?)
            """,
            (order_id, datetime.now(timezone.utc).isoformat(), reason),
        )
        return cursor.rowcount > 0


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
        close_order(order_id, reason)
    except sqlite3.Error:
        logging.exception("Не удалось сохранить закрытие заказа #%s", order_id)
        return

    logging.info("%s: #%s", CLOSE_REASONS[reason], order_id)
