"""
Полное описание лотов FunPay и Starvell по команде из Discord.

main.py (/menu lot-description) кладёт задание в таблицу
lot_description_jobs базы orders.sqlite3, этот модуль (работает в
run_all.py рядом с FunPay- и Starvell-клиентами) меняет описания и
записывает итог, а main.py отправляет его в Discord.

Описание задаётся отдельно для лотов монет, гемов и опыта. В описании
опыта {tower} заменяется названием башни из названия лота
(«ФАРМ УРОВНЯ НА KINGPIN» -> KINGPIN).
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from FunPay_sales import DATABASE_PATH


log = logging.getLogger("lot_descriptions")


CREATE_JOBS = """
CREATE TABLE IF NOT EXISTS lot_description_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'coins',
    platforms TEXT NOT NULL DEFAULT 'funpay,starvell',
    text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    result TEXT NOT NULL DEFAULT '',
    channel_id INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ''
)
"""

# Текущие описания (для подстановки в форму команды)
CREATE_CURRENT = """
CREATE TABLE IF NOT EXISTS lot_descriptions (
    kind TEXT PRIMARY KEY,
    text TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
)
"""

KIND_NAMES = {
    "coins": "монет",
    "gems": "гемов",
    "xp": "опыта",
}

TOWER_PLACEHOLDER = "{tower}"
TOWER_PLACEHOLDERS = (TOWER_PLACEHOLDER, "{TOWER}", "{башня}")

JOBS_CHECK_SECONDS = 5
CURRENT_REFRESH_SECONDS = 30 * 60
# Пауза между сохранениями лотов, чтобы площадки не ограничили запросы
SAVE_DELAY_SECONDS = 2


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(CREATE_JOBS)
    connection.execute(CREATE_CURRENT)
    return connection


def lot_kind(title: str) -> tuple[str | None, str]:
    """Вид лота по названию: ('xp', 'KINGPIN'), ('gems', ''), ('coins', '') или (None, '')."""
    # Импорт здесь: FunPay_commands подключает FunPay-клиент, а
    # main.py берёт из этого модуля только схему таблиц
    from FunPay_commands import tower_name_from_title

    upper = (title or "").upper()
    tower = tower_name_from_title(title)

    if tower or "ОПЫТ" in upper or "ФАРМ УРОВНЯ" in upper:
        return "xp", tower
    if "ГЕМ" in upper:
        return "gems", ""
    if "МОНЕТ" in upper:
        return "coins", ""
    return None, ""


def tower_display(tower: str) -> str:
    """KINGPIN -> Kingpin, CROOK BOSS -> Crook Boss."""
    return tower.title()


def render(text: str, tower: str) -> str:
    tower = tower_display(tower)
    for placeholder in TOWER_PLACEHOLDERS:
        text = text.replace(placeholder, tower)
    return text.strip()


def to_template(text: str, tower: str) -> str:
    """Описание лота опыта -> шаблон с {tower} для формы."""
    return text.replace(tower_display(tower), TOWER_PLACEHOLDER) if tower else text


# =========================
# FunPay
# =========================

async def funpay_user_id() -> int:
    from funpaybotengine.exceptions.bot_exceptions import BotNotInitializedError
    from funpay_shared import bot

    try:
        return bot.userid
    except BotNotInitializedError:
        await bot.update()
        return bot.userid


async def funpay_lots() -> list[tuple[int, str]]:
    from funpay_shared import bot

    profile = await bot.get_profile_page(await funpay_user_id())
    return [
        (offer.id, offer.title or "")
        for groups in (profile.offers or {}).values()
        for offers in groups.values()
        for offer in offers
    ]


def funpay_error_text(error: Exception, text: str = "") -> str:
    """Текст ошибки FunPay вместе с полями, на которые он указал."""
    fields = getattr(error, "fields", None) or {}

    if any("много строк" in str(reason) for reason in fields.values()):
        return (
            f"для FunPay слишком много строк ({len(text.splitlines())}) — "
            "уберите несколько пустых строк"
        )
    details = "; ".join(f"{field}: {reason}" for field, reason in fields.items())
    return f"{error} ({details})" if details else str(error)


async def funpay_set(kind: str, text: str) -> str:
    from funpay_shared import bot

    lots = [(lot_id, title) for lot_id, title in await funpay_lots() if lot_kind(title)[0] == kind]
    if not lots:
        return f"FunPay: лотов {KIND_NAMES[kind]} не найдено"

    changed, errors = 0, []

    for lot_id, title in lots:
        try:
            fields = await bot.get_offer_fields(offer_id=lot_id)
            fields.desc_ru = render(text, lot_kind(title)[1])
            await bot.save_offer_fields(fields)
            changed += 1
        except Exception as error:
            log.exception("FunPay: не удалось сохранить лот %s", lot_id)
            errors.append(f"{title.split('|')[0].strip()}: {funpay_error_text(error, text)}")
        await asyncio.sleep(SAVE_DELAY_SECONDS)

    return summary("FunPay", changed, len(lots), errors)


# =========================
# Starvell
# =========================

def starvell_offer_title(offer: dict) -> str:
    return ((offer.get("descriptions") or {}).get("rus") or {}).get("briefDescription") or ""


async def starvell_lots() -> list[tuple[str, str]]:
    from starvell_api import starvell

    if not starvell.user:
        await starvell.login()

    return [
        (offer.get("publicId"), starvell_offer_title(offer))
        for category in await starvell.profile_offers()
        for offer in category.get("offers") or []
        if offer.get("publicId")
    ]


async def starvell_set(kind: str, text: str) -> str:
    from starvell_api import starvell

    if not starvell.enabled:
        return "Starvell: не задан STARVELL_SESSION"

    lots = [(lot_id, title) for lot_id, title in await starvell_lots() if lot_kind(title)[0] == kind]
    if not lots:
        return f"Starvell: лотов {KIND_NAMES[kind]} не найдено"

    changed, errors = 0, []

    for lot_id, title in lots:
        try:
            await starvell.set_offer_description(lot_id, render(text, lot_kind(title)[1]))
            changed += 1
        except Exception as error:
            log.warning("Starvell: не удалось сохранить лот %s: %s", lot_id, error)
            errors.append(f"{title.split('|')[0].strip()}: {error}")
        await asyncio.sleep(SAVE_DELAY_SECONDS)

    return summary("Starvell", changed, len(lots), errors)


def summary(platform: str, changed: int, total: int, errors: list[str]) -> str:
    icon = "✅" if not errors else ("⚠️" if changed else "❌")
    lines = [f"{icon} {platform}: изменено {changed} из {total}"]

    # Одинаковые ошибки (например, у всех лотов) показываем один раз
    unique = []
    for error in errors:
        reason = error.split(": ", 1)[-1]
        if reason not in (item.split(": ", 1)[-1] for item in unique):
            unique.append(error)

    lines += [f"   • {error[:300]}" for error in unique[:3]]
    return "\n".join(lines)


# =========================
# Задания из Discord
# =========================

PLATFORM_SETTERS = {
    "funpay": funpay_set,
    "starvell": starvell_set,
}


async def run_job(job) -> str:
    results = []

    for platform in job["platforms"].split(","):
        setter = PLATFORM_SETTERS.get(platform.strip())
        if setter is None:
            continue
        try:
            results.append(await setter(job["kind"], job["text"]))
        except Exception as error:
            log.exception("Ошибка смены описания (%s)", platform)
            results.append(f"❌ {platform}: {error}")

    return "\n".join(results)


async def refresh_current():
    """Запоминает текущие описания (FunPay) для подстановки в форму."""
    from funpay_shared import bot

    found = {}

    for lot_id, title in await funpay_lots():
        kind, tower = lot_kind(title)
        if kind is None or kind in found:
            continue
        fields = await bot.get_offer_fields(offer_id=lot_id)
        found[kind] = to_template(fields.desc_ru or "", tower)

    now = datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        for kind, text in found.items():
            connection.execute(
                "INSERT OR REPLACE INTO lot_descriptions(kind, text, updated_at) VALUES (?, ?, ?)",
                (kind, text, now)
            )


async def jobs_loop():
    last_refresh = 0.0
    loop = asyncio.get_running_loop()

    # Задание, прерванное перезапуском, выполняем заново
    with connect() as connection:
        connection.execute("UPDATE lot_description_jobs SET status = 'new' WHERE status = 'running'")

    while True:
        try:
            if loop.time() - last_refresh > CURRENT_REFRESH_SECONDS:
                last_refresh = loop.time()
                await refresh_current()
        except Exception:
            log.exception("Не удалось прочитать текущие описания лотов")

        try:
            with connect() as connection:
                jobs = connection.execute(
                    "SELECT * FROM lot_description_jobs WHERE status = 'new' ORDER BY id"
                ).fetchall()

            for job in jobs:
                with connect() as connection:
                    connection.execute(
                        "UPDATE lot_description_jobs SET status = 'running' WHERE id = ?",
                        (job["id"],)
                    )

                log.info("Смена описания лотов %s: %s", KIND_NAMES.get(job["kind"]), job["platforms"])
                result = await run_job(job)

                with connect() as connection:
                    connection.execute(
                        "UPDATE lot_description_jobs SET status = 'done', result = ? WHERE id = ?",
                        (result, job["id"])
                    )

                # Форма в следующий раз покажет новое описание
                last_refresh = 0.0

        except Exception:
            log.exception("Ошибка заданий смены описания")

        await asyncio.sleep(JOBS_CHECK_SECONDS)
