"""
Несколько аккаунтов FunPay и Starvell с одной Discord-таблицей.

Первый аккаунт — обычные настройки .env (FUNPAY_GOLDEN_KEY,
STARVELL_SESSION, ...). Следующие — те же настройки с номером:

    FUNPAY_GOLDEN_KEY_2=...
    STARVELL_SESSION_2=...

run_all.py запускает каждый дополнительный аккаунт отдельным процессом
(run_all.py --account 2) с подменёнными настройками. Таблица, база
orders.sqlite3 и Discord у всех аккаунтов общие, а свои файлы
(каналы покупателей, состояние Starvell) у каждого аккаунта отдельные.

Чтобы о конце фарма писал покупателю тот аккаунт, с которым он
переписывался, бот запоминает, через какой аккаунт пришёл чат
(таблица chat_accounts). Чаты, которых там нет, — первого аккаунта.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "orders.sqlite3"

ACCOUNT_ENV = "BOT_ACCOUNT"
MAIN_ACCOUNT = "1"

# Настройки, которые у каждого аккаунта свои (с номером: NAME_2)
ACCOUNT_SETTINGS = (
    "FUNPAY_GOLDEN_KEY",
    "FUNPAY_OWNER_ID",
    "STARVELL_SESSION",
    "DISCORD_WEBHOOK_NAME",
    "STARVELL_WEBHOOK_NAME",
)

# Без своего значения у дополнительного аккаунта эти настройки пустые,
# а не как у первого: иначе он зашёл бы в чужой аккаунт
OWN_ONLY_SETTINGS = ("FUNPAY_GOLDEN_KEY", "FUNPAY_OWNER_ID", "STARVELL_SESSION")

CREATE_CHAT_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS chat_accounts (
    chat_id TEXT PRIMARY KEY,
    account TEXT NOT NULL DEFAULT '1',
    updated_at TEXT NOT NULL DEFAULT ''
)
"""


def current() -> str:
    return os.getenv(ACCOUNT_ENV, MAIN_ACCOUNT)


def is_main() -> bool:
    return current() == MAIN_ACCOUNT


def extra_accounts() -> list[str]:
    """Номера дополнительных аккаунтов из .env: ['2', '3', ...]."""
    numbers = set()

    for name, value in os.environ.items():
        match = re.fullmatch(r"(FUNPAY_GOLDEN_KEY|STARVELL_SESSION)_(\d+)", name)
        if match and value.strip() and match.group(2) != MAIN_ACCOUNT:
            numbers.add(match.group(2))

    return sorted(numbers, key=int)


def use_account(number: str) -> None:
    """Подменяет настройки на настройки аккаунта. Вызывать до импорта ботов."""
    os.environ[ACCOUNT_ENV] = number

    if number == MAIN_ACCOUNT:
        return

    for name in ACCOUNT_SETTINGS:
        value = os.getenv(f"{name}_{number}")
        if value is not None:
            os.environ[name] = value.strip()
        elif name in OWN_ONLY_SETTINGS:
            os.environ[name] = ""


def label() -> str:
    """Для логов и Discord: '' у первого аккаунта, ' (аккаунт 2)' у остальных."""
    return "" if is_main() else f" (аккаунт {current()})"


def state_file(name: str) -> Path:
    """funpay_channels.json -> funpay_channels_2.json у второго аккаунта."""
    path = BASE_DIR / name
    if is_main():
        return path
    return path.with_name(f"{path.stem}_{current()}{path.suffix}")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.execute(CREATE_CHAT_ACCOUNTS)
    return connection


def remember_chat(chat_id) -> None:
    """Чат пришёл через этот аккаунт — ему и писать в этот чат."""
    if not chat_id:
        return

    try:
        with connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO chat_accounts(chat_id, account, updated_at) VALUES (?, ?, ?)",
                (str(chat_id), current(), datetime.now(timezone.utc).isoformat()),
            )
    except sqlite3.Error:
        # Не мешаем пересылке сообщения: чат просто останется за первым аккаунтом
        pass


def owns_chat(chat_id) -> bool:
    try:
        with connect() as connection:
            row = connection.execute(
                "SELECT account FROM chat_accounts WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
    except sqlite3.Error:
        return is_main()

    return (row[0] if row else MAIN_ACCOUNT) == current()
