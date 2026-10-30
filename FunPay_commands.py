import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from funpaybotengine import Router
from funpaybotengine.types import Message

from funpay_shared import bot, dispatcher
from FunPay_sales import (
    CREATE_FINISHED_ORDERS,
    connect as connect_sales,
    find_order_for_chat,
    order_link,
    set_pending_player,
)


# =================================================
# Настройки
# =================================================

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "orders.sqlite3"

# ID администраторов из .env
ADMIN_CHAT_IDS = {
    item.strip()
    for item in os.getenv("ADMIN_CHAT_IDS", "").split(",")
    if item.strip()
}

if not DATABASE_PATH.exists():
    raise FileNotFoundError(
        f"База данных не найдена:\n{DATABASE_PATH}"
    )


# =================================================
# Бот
# =================================================

# Общий FunPay-клиент (funpay_shared.py), здесь только свои обработчики.
# as_task=True — обработчик не задерживает опрос FunPay.
router = Router(name="commands")


# =================================================
# Подготовка базы данных
# =================================================

# Базы, в которых уже добавлены нужные столбцы
_prepared_paths: set[Path] = set()


def database_paths() -> list[Path]:
    """
    Базы таблиц всех серверов Discord: orders.sqlite3 основного
    сервера и orders_<id сервера>.sqlite3 остальных (см. main.py).
    """

    paths = [DATABASE_PATH] + sorted(BASE_DIR.glob("orders_*.sqlite3"))

    for path in paths:
        if path in _prepared_paths:
            continue

        try:
            prepare_database(path)
            _prepared_paths.add(path)
        except sqlite3.Error as error:
            # Базу нового сервера main.py может ещё создавать
            logging.warning("База %s пока не готова: %s", path.name, error)

    return [path for path in paths if path in _prepared_paths]


def prepare_database(database_path: Path = DATABASE_PATH):
    """
    Добавляет дополнительные столбцы в таблицу rows.
    """

    with sqlite3.connect(database_path) as connection:
        cursor = connection.cursor()

        # Записи о нафармивших аккаунтах (их создаёт main.py)
        cursor.execute(CREATE_FINISHED_ORDERS)

        columns = {
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(rows)"
            ).fetchall()
        }

        if "chat_id" not in columns:
            cursor.execute(
                """
                ALTER TABLE rows
                ADD COLUMN chat_id TEXT
                """
            )
            logging.info("Добавлен столбец chat_id")

        if "farm_finished" not in columns:
            cursor.execute(
                """
                ALTER TABLE rows
                ADD COLUMN farm_finished INTEGER DEFAULT 0
                """
            )
            logging.info("Добавлен столбец farm_finished")

        for column, definition in (
            ("before_gems", "INTEGER DEFAULT 0"),
            ("paid_gems", "INTEGER DEFAULT 0"),
            ("account_gems", "INTEGER DEFAULT 0"),
            ("adjust_gems", "INTEGER DEFAULT 0"),
            ("before_xp", "INTEGER DEFAULT 0"),
            ("paid_xp", "INTEGER DEFAULT 0"),
            ("account_xp", "INTEGER DEFAULT 0"),
            ("adjust_xp", "INTEGER DEFAULT 0"),
            ("currency", "TEXT DEFAULT 'coins'"),
        ):
            if column not in columns:
                cursor.execute(f"ALTER TABLE rows ADD COLUMN {column} {definition}")
                logging.info("Добавлен столбец %s", column)

        connection.commit()


# =================================================
# Сохранение ID чата пользователя
# =================================================

def get_chat_id_from_message(message: Message):
    """
    Получает ID чата из сообщения.
    """

    possible_fields = (
        "chat_id",
        "conversation_id",
        "user_id",
        "sender_id",
        "author_id",
    )

    for field_name in possible_fields:
        value = getattr(message, field_name, None)

        if value is not None:
            return str(value)

    return None


def is_admin(message: Message) -> bool:
    """
    Проверяет, является ли отправитель администратором.
    """

    chat_id = get_chat_id_from_message(message)

    if chat_id is None:
        return False

    return chat_id in ADMIN_CHAT_IDS



def save_chat_id(player_name: str, chat_id: str) -> bool:
    """
    Привязывает ID чата к игровому нику. Пустую ссылку или ссылку
    на чат в строке заменяет ссылкой на заказ (если он известен
    по продажам — с учётом валюты строки) или на чат с покупателем.
    """

    try:
        updated = 0
        chat_link = f"https://funpay.com/chat/?node={chat_id}"

        with connect_sales() as sales_connection:
            for database_path in database_paths():
                with sqlite3.connect(database_path) as connection:
                    rows = connection.execute(
                        """
                        SELECT id, currency, link
                        FROM rows
                        WHERE LOWER(TRIM(player)) = LOWER(TRIM(?))
                        """,
                        (player_name,)
                    ).fetchall()

                    for row_id, currency, link in rows:
                        link = link or ""

                        if not link.strip() or "funpay.com/chat/" in link:
                            order_id = find_order_for_chat(
                                sales_connection,
                                chat_id,
                                player_name,
                                currency or "coins"
                            )
                            link = order_link(order_id) if order_id else chat_link

                        connection.execute(
                            """
                            UPDATE rows
                            SET chat_id = ?,
                                farm_finished = 0,
                                link = ?
                            WHERE id = ?
                            """,
                            (str(chat_id), link, row_id)
                        )
                        updated += 1

                    connection.commit()

        return updated > 0

    except sqlite3.Error as error:
        logging.error(
            "Ошибка сохранения ID чата: %s",
            error
        )
        return False


# =================================================
# Получение игрока из базы
# =================================================

def get_account_data(player_name: str) -> dict | None:
    """Ищет игрока в таблицах всех серверов."""

    for database_path in database_paths():
        try:
            with sqlite3.connect(database_path) as connection:
                connection.row_factory = sqlite3.Row

                result = connection.execute(
                    """
                    SELECT
                        id,
                        player,
                        account_coins,
                        adjust_coins,
                        before_coins,
                        paid_coins,
                        account_gems,
                        adjust_gems,
                        before_gems,
                        paid_gems,
                        before_xp,
                        paid_xp,
                        account_xp,
                        adjust_xp,
                        currency,
                        chat_id,
                        farm_finished
                    FROM rows
                    WHERE LOWER(TRIM(player)) = LOWER(TRIM(?))
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (player_name,)
                ).fetchone()

                if result is not None:
                    return dict(result)

        except sqlite3.Error as error:
            logging.error(
                "Ошибка чтения базы %s: %s",
                database_path.name,
                error
            )

    return None


# =================================================
# Расчёт баланса и прогресса
# =================================================

# Названия валют: (значок, «Монеты», «монет»)
CURRENCY_NAMES = {
    "coins": ("💰", "Монеты", "монет"),
    "gems": ("💎", "Гемы", "гемов"),
    "xp": ("💠", "Опыт", "опыта"),
}


def currency_names(currency: str):
    return CURRENCY_NAMES.get(currency, CURRENCY_NAMES["coins"])


def calculate_progress(data: dict):
    currency = (data.get("currency") or "coins").lower()
    if currency in ("gems", "xp"):
        account = data.get(f"account_{currency}") or 0
        adjust = data.get(f"adjust_{currency}") or 0
        # Опыт считается с нуля: цель — только оплаченный опыт
        before = 0 if currency == "xp" else data.get("before_gems") or 0
        paid = data.get(f"paid_{currency}") or 0
    else:
        currency = "coins"
        account = data.get("account_coins") or 0
        adjust = data.get("adjust_coins") or 0
        before = (data.get("before_coins") or 0) * 1000
        paid = (data.get("paid_coins") or 0) * 1000
    total = account + adjust
    goal = (before + paid)
    progress = int((total / goal) * 100) if goal > 0 else 0
    return total, goal, min(progress, 100), currency


# =================================================
# Формирование статуса фарма
# =================================================

def get_farm_status(total: int, goal: int, currency: str) -> str:
    if goal > 0 and total >= goal:
        name = currency_names(currency)[2]
        return f"\n\n✅ Фарм закончен!\n🎉 Цель по {name} достигнута."
    return ""


# =================================================
# Команда /money
# =================================================

@router.on_new_message(as_task=True)
async def money_command(message: Message):
    text = (message.text or "").strip()

    if not text.lower().startswith("/money"):
        return

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await message.reply(
            text="Использование: /money ник"
        )
        return

    player_name = parts[1].strip()

    if not player_name:
        await message.reply(
            text="Укажите игровой ник."
        )
        return

    data = get_account_data(player_name)

    if data is None:
        await message.reply(
            text=f"Ник «{player_name}» не найден в базе данных."
        )
        return

    # Сохраняем пользователя, который запросил баланс
    # Администратор может проверять любого игрока,
    # но его ID не сохраняется в базе
    if not is_admin(message):
        chat_id = get_chat_id_from_message(message)

        if chat_id is not None:
            # Ник появится в карточке продажи в Discord
            set_pending_player(chat_id, player_name)

            saved = save_chat_id(player_name, chat_id)

            if saved:
                logging.info(
                    "Игрок %s привязан к чату %s через /money",
                    player_name,
                    chat_id
                )
    else:
        logging.info(
            "Администратор проверил игрока %s через /money. "
            "ID не сохранён.",
            player_name
        )


    total, goal, progress, currency = calculate_progress(data)
    icon, title, _ = currency_names(currency)
    currency_name = f"{icon} {title}"
    await message.reply(text=f"{currency_name} игрока {player_name}: {total:,}")


# =================================================
# Команда /info
# =================================================

@router.on_new_message(as_task=True)
async def info_command(message: Message):
    text = (message.text or "").strip()

    if not text.lower().startswith("/info"):
        return

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await message.reply(
            text=(
                "Использование: /info ник\n\n"
                "Команда также привязывает ваш чат "
                "к игровому нику."
            )
        )
        return

    player_name = parts[1].strip()

    if not player_name:
        await message.reply(
            text="Укажите игровой ник."
        )
        return

    data = get_account_data(player_name)

    if data is None:
        await message.reply(
            text=f"Ник «{player_name}» не найден в базе данных."
        )
        return

    # Сохраняем пользователя, который запросил информацию
    # Администратор проверяет игрока без привязки своего чата
    if not is_admin(message):
        chat_id = get_chat_id_from_message(message)

        if chat_id is not None:
            # Ник появится в карточке продажи в Discord
            set_pending_player(chat_id, player_name)

            saved = save_chat_id(player_name, chat_id)

            if saved:
                logging.info(
                    "Игрок %s привязан к чату %s через /info",
                    player_name,
                    chat_id
                )
    else:
        logging.info(
            "Администратор проверил игрока %s через /info. "
            "ID не сохранён.",
            player_name
        )


    total, goal, progress, currency = calculate_progress(data)

    icon, name, _ = currency_names(currency)

    if currency == "gems":
        before = data.get("before_gems") or 0
    else:
        before = (data.get("before_coins") or 0) * 1000

    # Опыт считается с нуля — «до заказа» у него нет
    before_line = "" if currency == "xp" else f"{icon} Баланс до заказа: {before:,}\n"

    status = get_farm_status(total, goal, currency)

    await message.reply(
        text=(
            f"📊 Информация об игроке: {player_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{icon} Баланс игрока: {total:,} {name.lower()}\n"
            f"{before_line}"
            f"🎯 Цель фарма: {goal:,} {name.lower()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Прогресс: {progress}%"
            f"{status}"
        )
    )


# =================================================
# Админская команда /myid
# =================================================

@router.on_new_message(as_task=True)
async def my_id_command(message: Message):
    text = (message.text or "").strip()

    if text.lower() != "/myid":
        return

    chat_id = get_chat_id_from_message(message)

    print(
        f"[MYID] Пользователь отправил /myid. "
        f"ID чата: {chat_id}"
    )

    logging.info(
        "Команда /myid. ID чата: %s",
        chat_id
    )


# =================================================
# Команда /help
# =================================================

@router.on_new_message(as_task=True)
async def help_command(message: Message):
    text = (message.text or "").strip()

    if not text.lower().startswith("/help"):
        return

    await message.reply(
        text=(
            "📖 Доступные команды:\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "/money <ник> — баланс и привязка чата\n"
            "/info <ник> — подробная информация\n"
            "/help — справка\n"
        )
    )


# =================================================
# Получение активных заказов
# =================================================

def get_active_orders() -> list[dict]:
    orders = []

    for database_path in database_paths():
        try:
            with sqlite3.connect(database_path) as connection:
                connection.row_factory = sqlite3.Row

                rows = connection.execute(
                    """
                    SELECT
                        id,
                        player,
                        account_coins,
                        adjust_coins,
                        before_coins,
                        paid_coins,
                        account_gems,
                        adjust_gems,
                        before_gems,
                        paid_gems,
                        before_xp,
                        paid_xp,
                        account_xp,
                        adjust_xp,
                        currency,
                        chat_id,
                        farm_finished
                    FROM rows
                    WHERE chat_id IS NOT NULL
                      AND TRIM(chat_id) != ''
                      AND COALESCE(farm_finished, 0) = 0
                    """
                ).fetchall()

            for row in rows:
                order = dict(row)
                order["database_path"] = database_path
                orders.append(order)

        except sqlite3.Error as error:
            logging.error(
                "Ошибка получения активных заказов из %s: %s",
                database_path.name,
                error
            )

    return orders


def mark_farm_finished(row_id: int, database_path: Path = DATABASE_PATH):
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE rows
            SET farm_finished = 1
            WHERE id = ?
            """,
            (row_id,)
        )

        connection.commit()


# =================================================
# Отправка сообщения о завершении
# =================================================

def completion_text(player: str, total, goal, progress: int, currency: str) -> str:
    icon, _, name = currency_names(currency)

    return (
        f"✅ Фарм игрока {player} закончен!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} Получено {name}: {total:,}\n"
        f"🎯 Цель: {goal:,}\n"
        f"📊 Прогресс: {progress}%"
    )


async def send_completion_message(order: dict):
    total, goal, progress, currency = calculate_progress(order)

    await bot.send_message(
        chat_id=order["chat_id"],
        text=completion_text(order["player"], total, goal, progress, currency)
    )


def get_unnotified_finished_orders() -> list[dict]:
    """
    Аккаунты, которые нафармили: main.py сразу убирает их из таблицы,
    поэтому сообщение покупателю берём из finished_orders.
    """

    notices = []

    for database_path in database_paths():
        try:
            with sqlite3.connect(database_path) as connection:
                connection.row_factory = sqlite3.Row

                rows = connection.execute(
                    """
                    SELECT *
                    FROM finished_orders
                    WHERE buyer_notified = 0
                      AND TRIM(chat_id) != ''
                    """
                ).fetchall()

            for row in rows:
                notice = dict(row)
                notice["database_path"] = database_path
                notices.append(notice)

        except sqlite3.Error as error:
            logging.error(
                "Ошибка чтения finished_orders из %s: %s",
                database_path.name,
                error
            )

    return notices


async def notify_finished_orders():
    for notice in get_unnotified_finished_orders():
        try:
            await bot.send_message(
                chat_id=notice["chat_id"],
                text=completion_text(
                    notice["player"],
                    int(round(notice["balance"])),
                    int(round(notice["goal"])),
                    100,
                    notice["currency"]
                )
            )
        except Exception:
            logging.exception(
                "Не удалось отправить уведомление игроку %s",
                notice["player"]
            )
            continue

        with sqlite3.connect(notice["database_path"]) as connection:
            connection.execute(
                "UPDATE finished_orders SET buyer_notified = 1 WHERE id = ?",
                (notice["id"],)
            )

        logging.info(
            "Уведомление о конце фарма отправлено игроку %s",
            notice["player"]
        )


# =================================================
# Фоновая проверка фарма
# =================================================

async def farm_checker():
    while True:
        try:
            orders = get_active_orders()

            for order in orders:
                total, goal, progress, currency = calculate_progress(order)

                if goal <= 0:
                    continue

                if total >= goal:
                    try:
                        await send_completion_message(order)

                        mark_farm_finished(
                            order["id"],
                            order["database_path"]
                        )

                        logging.info(
                            "Уведомление отправлено игроку %s",
                            order["player"]
                        )

                    except Exception:
                        logging.exception(
                            "Не удалось отправить уведомление "
                            "игроку %s",
                            order["player"]
                        )

            await notify_finished_orders()

        except Exception:
            logging.exception(
                "Ошибка фоновой проверки фарма"
            )

        await asyncio.sleep(30)


# =================================================
# Запуск
# =================================================

def setup():
    """
    Подготовка базы. Вызывается из run_all.py и при
    отдельном запуске этого файла.
    """

    logging.info("🚀 Команды FunPay запускаются")
    logging.info("📁 База данных: %s", DATABASE_PATH)

    logging.info(
        "📁 Таблиц серверов: %s",
        len(database_paths())
    )

    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM rows")
        count = cursor.fetchone()[0]

        logging.info(
            "📊 В базе данных записей: %s",
            count
        )


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    setup()

    dispatcher.connect_router(router)

    asyncio.create_task(farm_checker())

    logging.info("🔄 Подключение к FunPay")

    await bot.listen_events(dispatcher)


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logging.info("⏹️ Бот остановлен")

    except Exception as error:
        logging.exception(
            "❌ Критическая ошибка: %s",
            error
        )
