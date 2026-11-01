import asyncio
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from funpaybotengine import Router
from funpaybotengine.types import Message

from funpay_shared import bot, dispatcher
from FunPay_sales import (
    CREATE_FINISHED_ORDERS,
    chat_link,
    connect as connect_sales,
    find_order_for_chat,
    is_starvell_id,
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
        buyer_chat_link = chat_link(chat_id)

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

                        if not link.strip() or "funpay.com/chat/" in link or "starvell.com/chat/" in link:
                            order_id = find_order_for_chat(
                                sales_connection,
                                chat_id,
                                player_name,
                                currency or "coins"
                            )
                            link = order_link(order_id) if order_id else buyer_chat_link

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
# Команды /money и /info
# =================================================
#
# Ответы собираются отдельно от FunPay: те же команды
# обрабатывает и бот Starvell (Starvell_bot.py).

def bind_player_chat(player_name: str, chat_id, admin: bool, command: str):
    """
    Покупатель написал команду со своим ником — привязываем его чат
    к строке таблицы. Администратор проверяет игрока без привязки.
    """

    if admin:
        logging.info(
            "Администратор проверил игрока %s через %s. ID не сохранён.",
            player_name,
            command
        )
        return

    if chat_id is None:
        return

    # Ник появится в карточке продажи в Discord
    set_pending_player(chat_id, player_name)

    if save_chat_id(player_name, chat_id):
        logging.info(
            "Игрок %s привязан к чату %s через %s",
            player_name,
            chat_id,
            command
        )


def money_reply(text: str, chat_id, admin: bool) -> str | None:
    """Ответ на /money ник или None, если это не команда /money."""

    if not text.lower().startswith("/money"):
        return None

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        return "Использование: /money ник"

    player_name = parts[1].strip()

    if not player_name:
        return "Укажите игровой ник."

    data = get_account_data(player_name)

    if data is None:
        return f"Ник «{player_name}» не найден в базе данных."

    bind_player_chat(player_name, chat_id, admin, "/money")

    total, goal, progress, currency = calculate_progress(data)
    icon, title, _ = currency_names(currency)
    return f"{icon} {title} игрока {player_name}: {total:,}"


def info_reply(text: str, chat_id, admin: bool) -> str | None:
    """Ответ на /info ник или None, если это не команда /info."""

    if not text.lower().startswith("/info"):
        return None

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        return (
            "Использование: /info ник\n\n"
            "Команда также привязывает ваш чат "
            "к игровому нику."
        )

    player_name = parts[1].strip()

    if not player_name:
        return "Укажите игровой ник."

    data = get_account_data(player_name)

    if data is None:
        return f"Ник «{player_name}» не найден в базе данных."

    bind_player_chat(player_name, chat_id, admin, "/info")

    total, goal, progress, currency = calculate_progress(data)

    icon, name, _ = currency_names(currency)

    if currency == "gems":
        before = data.get("before_gems") or 0
    else:
        before = (data.get("before_coins") or 0) * 1000

    # Опыт считается с нуля — «до заказа» у него нет
    before_line = "" if currency == "xp" else f"{icon} Баланс до заказа: {before:,}\n"

    status = get_farm_status(total, goal, currency)

    return (
        f"📊 Информация об игроке: {player_name}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} Баланс игрока: {total:,} {name.lower()}\n"
        f"{before_line}"
        f"🎯 Цель фарма: {goal:,} {name.lower()}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Прогресс: {progress}%"
        f"{status}"
    )


@router.on_new_message(as_task=True)
async def money_command(message: Message):
    reply = money_reply(
        (message.text or "").strip(),
        get_chat_id_from_message(message),
        is_admin(message)
    )

    if reply:
        await message.reply(text=reply)


@router.on_new_message(as_task=True)
async def info_command(message: Message):
    reply = info_reply(
        (message.text or "").strip(),
        get_chat_id_from_message(message),
        is_admin(message)
    )

    if reply:
        await message.reply(text=reply)


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

# =================================================
# Команда /exp — расчёт опыта до 20 уровня башни
# =================================================

MAX_TOWER_LEVEL = 20

# Опыт, нужный на каждый уровень: индекс — уровень (1..20)
EVOLUTION_LEVEL_EXP = [
    0, 75, 80, 86, 93, 100, 107, 115, 124, 133, 143,
    154, 166, 178, 192, 206, 221, 238, 256, 275, 296,
]
REGULAR_LEVEL_EXP = [
    0, 50, 54, 59, 64, 70, 76, 83, 91, 99, 108,
    118, 129, 140, 153, 167, 182, 198, 216, 235, 257,
]

TOWER_TYPES = {
    "evolution": EVOLUTION_LEVEL_EXP,
    "regular": REGULAR_LEVEL_EXP,
}

# Названия башен (в нижнем регистре, без пробелов) -> (название, тип)
TOWER_ALIASES = {
    "juggernaut": ("JUGGERNAUT", "evolution"),
    "jaggernaut": ("JUGGERNAUT", "evolution"),
    "operator": ("OPERATOR", "evolution"),
    "enforcer": ("ENFORCER", "evolution"),
    "kingpin": ("KINGPIN", "evolution"),
    "shotgunner": ("SHOTGUNNER", "regular"),
    "crookboss": ("CROOK BOSS", "regular"),
    "scout": ("SCOUT", "regular"),
    "minigunner": ("MINIGUNNER", "regular"),
}

EXP_USAGE = (
    "❗Использование: /exp <башня> <текущий уровень> <кол-во опыта текущего уровня>\n"
    "✅Пример: /exp scout 5 30\n"
    "⏫Эволюции: Juggernaut, Operator, Enforcer, Kingpin\n"
    "⏹️Обычные: Shotgunner, Crook Boss, Scout, Minigunner"
)

# Лоты «ФАРМ УРОВНЯ НА <БАШНЯ>» берутся из профиля продавца:
# если лот пересоздать, ссылка обновится сама. Кэш — на час.
TOWER_LOTS_CACHE_SECONDS = 60 * 60
_tower_lots: dict[str, str] = {}
_tower_lots_loaded_at = 0.0


async def get_tower_lot_link(tower_name: str) -> str:
    global _tower_lots, _tower_lots_loaded_at

    if not _tower_lots or time.monotonic() - _tower_lots_loaded_at > TOWER_LOTS_CACHE_SECONDS:
        try:
            profile = await bot.get_profile_page(bot.userid)
            lots = {}

            for groups in (profile.offers or {}).values():
                for offers in groups.values():
                    for offer in offers:
                        name = tower_name_from_title(offer.title)
                        if name:
                            lots[name] = f"https://funpay.com/lots/offer?id={offer.id}"

            _tower_lots = lots
            _tower_lots_loaded_at = time.monotonic()
        except Exception:
            logging.exception("Не удалось получить лоты башен для /exp")

    return _tower_lots.get(tower_name, "")


def calculate_exp_to_max(level_exp: list[int], level: int, exp_on_level: int) -> int:
    """Сколько опыта осталось до 20 уровня."""
    return sum(level_exp[level + 1:MAX_TOWER_LEVEL + 1]) - exp_on_level


async def exp_reply(text: str, get_lot_link=None) -> str | None:
    """
    Ответ на /exp или None, если это не команда /exp.
    get_lot_link(башня) -> ссылка на лот; по умолчанию — лоты FunPay.
    """

    if not re.match(r"^/exp(?:\s|$)", text, flags=re.IGNORECASE):
        return None

    parts = text.split()[1:]

    if len(parts) < 3:
        return EXP_USAGE

    tower_key = "".join(parts[:-2]).lower()
    tower = TOWER_ALIASES.get(tower_key)

    if tower is None:
        return f"Башня «{' '.join(parts[:-2])}» не найдена.\n\n{EXP_USAGE}"

    try:
        level = int(parts[-2])
        exp_on_level = int(parts[-1])
    except ValueError:
        return f"Уровень и опыт должны быть числами.\n\n{EXP_USAGE}"

    tower_name, tower_type = tower
    level_exp = TOWER_TYPES[tower_type]
    title = tower_name

    if level >= MAX_TOWER_LEVEL:
        return f"💠 {title}: уже {MAX_TOWER_LEVEL} уровень, опыт не нужен."

    if level < 0:
        return f"Уровень должен быть от 0 до {MAX_TOWER_LEVEL}."

    next_level_exp = level_exp[level + 1]

    if exp_on_level < 0 or exp_on_level >= next_level_exp:
        return (
            f"На {level} уровне может быть от 0 до {next_level_exp - 1} опыта "
            f"(на {level + 1} уровень нужно {next_level_exp})."
        )

    need = calculate_exp_to_max(level_exp, level, exp_on_level)
    need_text = f"{need:,}".replace(",", " ")
    lot_link = await (get_lot_link or get_tower_lot_link)(tower_name)
    pay_line = (
        f"💳 Оплатите {need} шт. в лоте: {lot_link}"
        if lot_link
        else f"💳 Оплатите {need} шт. в лоте «ФАРМ УРОВНЯ НА {tower_name}»"
    )

    return (
        f"💠 Расчёт опыта: {title}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Сейчас: {level} уровень, {exp_on_level} опыта\n"
        f"🎯 До {MAX_TOWER_LEVEL} уровня нужно: {need_text} опыта\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{pay_line}"
    )


def tower_name_from_title(title: str) -> str:
    """'...ФАРМ УРОВНЯ НА KINGPIN...' -> 'KINGPIN' (или '')."""
    title = (title or "").upper()

    if "ФАРМ УРОВНЯ" not in title:
        return ""

    for name, _ in TOWER_ALIASES.values():
        if f"НА {name}" in title:
            return name

    return ""


@router.on_new_message(as_task=True)
async def exp_command(message: Message):
    reply = await exp_reply((message.text or "").strip())

    if reply:
        await message.reply(text=reply)


HELP_TEXT = (
    "📖 Доступные команды:\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
    "/money <ник> — баланс и привязка чата\n"
    "/info <ник> — подробная информация\n"
    "/exp <башня> <уровень> <опыт> — сколько опыта оплатить до 20 уровня\n"
    "/help — справка\n"
)


@router.on_new_message(as_task=True)
async def help_command(message: Message):
    text = (message.text or "").strip()

    if not text.lower().startswith("/help"):
        return

    await message.reply(text=HELP_TEXT)


# =================================================
# Получение активных заказов
# =================================================

def is_platform_chat(chat_id, starvell: bool) -> bool:
    """Чат Starvell — UUID, чат FunPay — число."""
    return is_starvell_id(chat_id) == starvell


def get_active_orders(starvell: bool = False) -> list[dict]:
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
                if not is_platform_chat(row["chat_id"], starvell):
                    continue
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


def get_unnotified_finished_orders(starvell: bool = False) -> list[dict]:
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
                if not is_platform_chat(row["chat_id"], starvell):
                    continue
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


async def send_funpay_message(chat_id, text: str):
    await bot.send_message(chat_id=chat_id, text=text)


async def notify_finished_orders(send=send_funpay_message, starvell: bool = False):
    for notice in get_unnotified_finished_orders(starvell):
        try:
            await send(
                notice["chat_id"],
                completion_text(
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


async def check_farm(send=send_funpay_message, starvell: bool = False):
    """
    Пишет покупателям, чей аккаунт нафармил. send(chat_id, текст) —
    отправка в чат площадки; starvell — какие чаты проверять.
    """

    for order in get_active_orders(starvell):
        total, goal, progress, currency = calculate_progress(order)

        if goal <= 0 or total < goal:
            continue

        try:
            await send(
                order["chat_id"],
                completion_text(order["player"], total, goal, progress, currency)
            )

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

    await notify_finished_orders(send, starvell)


# =================================================
# Фоновая проверка фарма
# =================================================

async def farm_checker():
    while True:
        try:
            await check_farm()

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
