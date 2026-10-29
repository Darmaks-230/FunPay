import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from funpaybotengine import Router
from funpaybotengine.types import Message

from funpay_shared import bot, dispatcher


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

def prepare_database():
    """
    Добавляет дополнительные столбцы в таблицу rows.
    """

    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.cursor()

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
    Привязывает ID чата к игровому нику.
    """

    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            cursor = connection.cursor()

            cursor.execute(
                """
                UPDATE rows
                SET chat_id = ?,
                    farm_finished = 0
                WHERE LOWER(TRIM(player)) = LOWER(TRIM(?))
                """,
                (str(chat_id), player_name)
            )

            connection.commit()

            return cursor.rowcount > 0

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
    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.row_factory = sqlite3.Row

            cursor = connection.cursor()

            cursor.execute(
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
                    currency,
                    chat_id,
                    farm_finished
                FROM rows
                WHERE LOWER(TRIM(player)) = LOWER(TRIM(?))
                ORDER BY id DESC
                LIMIT 1
                """,
                (player_name,)
            )

            result = cursor.fetchone()

            if result is None:
                return None

            return dict(result)

    except sqlite3.Error as error:
        logging.error(
            "Ошибка чтения базы данных: %s",
            error
        )
        return None


# =================================================
# Расчёт баланса и прогресса
# =================================================

def calculate_progress(data: dict):
    currency = (data.get("currency") or "coins").lower()
    if currency == "gems":
        account = data.get("account_gems") or 0
        adjust = data.get("adjust_gems") or 0
        before = data.get("before_gems") or 0
        paid = data.get("paid_gems") or 0
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
        name = "гемов" if currency == "gems" else "монет"
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
    currency_name = "💎 Гемы" if currency == "gems" else "💰 Монеты"
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

    if currency == "gems":
        icon = "💎"
        name = "Гемы"
        before = data.get("before_gems") or 0
    else:
        icon = "💰"
        name = "Монеты"
        before = (data.get("before_coins") or 0) * 1000

    status = get_farm_status(total, goal, currency)

    await message.reply(
        text=(
            f"📊 Информация об игроке: {player_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{icon} Баланс игрока: {total:,} {name.lower()}\n"
            f"{icon} Баланс до заказа: {before:,}\n"
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
    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.row_factory = sqlite3.Row

            cursor = connection.cursor()

            cursor.execute(
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
                    currency,
                    chat_id,
                    farm_finished
                FROM rows
                WHERE chat_id IS NOT NULL
                  AND TRIM(chat_id) != ''
                  AND COALESCE(farm_finished, 0) = 0
                """
            )

            return [
                dict(row)
                for row in cursor.fetchall()
            ]

    except sqlite3.Error as error:
        logging.error(
            "Ошибка получения активных заказов: %s",
            error
        )
        return []


def mark_farm_finished(row_id: int):
    with sqlite3.connect(DATABASE_PATH) as connection:
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

async def send_completion_message(order: dict):
    total, goal, progress, currency = calculate_progress(order)
    chat_id = order["chat_id"]
    icon = "💎" if currency == "gems" else "💰"
    name = "гемов" if currency == "gems" else "монет"

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Фарм игрока {order['player']} закончен!\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{icon} Получено {name}: {total:,}\n"
            f"🎯 Цель: {goal:,}\n"
            f"📊 Прогресс: {progress}%"
        )
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

                        mark_farm_finished(order["id"])

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

    prepare_database()

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
