import asyncio
import logging
import os
import time
from typing import Any

from funpaybotengine import Bot
from funpaybotengine.exceptions.bot_exceptions import BotNotInitializedError
from funpaybotengine.methods.raise_offers import RaiseOffers
from funpaybotengine.types.enums import SubcategoryType

from funpay_shared import bot as shared_bot


logger = logging.getLogger("funpay_raise")


DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Пауза между поднятием разных игр
DELAY_SECONDS = int(os.getenv("DELAY_SECONDS", "10"))

# Максимальная пауза между проверками
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "30"))

# Как часто перечитывать список своих лотов (1 запрос профиля)
LOTS_REFRESH_SECONDS = 30 * 60

# Если FunPay не сказал, сколько ждать
DEFAULT_WAIT_SECONDS = 4 * 60 * 60

# Повтор после сетевой ошибки
ERROR_RETRY_SECONDS = 10 * 60


class SafeRaiseOffers(RaiseOffers):
    """
    При успешном поднятии FunPay может не прислать
    wait / unlock_at, а библиотека требует эти поля и
    падает с ошибкой валидации, хотя лоты уже подняты.
    """

    async def parse_result(self, response: Any) -> dict[str, Any]:
        data = await super().parse_result(response)

        defaults = {
            "error": False,
            "msg": "",
            "wait": 0,
            "unlock_at": "",
        }

        for name, value in defaults.items():
            if data.get(name) is None:
                data[name] = value

        return data


async def get_my_categories(bot: Bot) -> dict[int, tuple[int, ...]]:
    """
    Возвращает {category_id: (subcategory_id, ...)} только для
    тех разделов, где у продавца есть лоты.

    Раньше бот брал все 4280 категорий с главной страницы и
    пытался поднять чужие игры, а на первом же «Подождите 4 часа»
    останавливался целиком.
    """

    # bot.userid бросает исключение, пока бот не инициализирован
    try:
        user_id = bot.userid
    except BotNotInitializedError:
        await bot.update()
        user_id = bot.userid

    profile = await bot.get_profile_page(user_id)

    my_lots = (profile.offers or {}).get(SubcategoryType.OFFERS, {})
    my_subcategory_ids = set(my_lots)

    if not my_subcategory_ids:
        return {}

    categories = await bot.storage.get_categories()

    if not categories:
        await bot.update()
        categories = await bot.storage.get_categories()

    result: dict[int, tuple[int, ...]] = {}

    for category in categories:
        if category is None:
            continue

        subcategory_ids = tuple(
            sorted(
                subcategory.id
                for subcategory in category.subcategories
                if subcategory.type is SubcategoryType.OFFERS
                and subcategory.id in my_subcategory_ids
            )
        )

        if subcategory_ids:
            result[category.id] = subcategory_ids

    return result


async def raise_category(
    bot: Bot,
    category_id: int,
    subcategory_ids: tuple[int, ...],
) -> int:
    """Поднимает одну игру. Возвращает, через сколько секунд повторить."""

    if DRY_RUN:
        logger.info(
            "DRY_RUN: category_id=%s, subcategory_ids=%s",
            category_id,
            subcategory_ids,
        )
        return INTERVAL_MINUTES * 60

    response = (
        await SafeRaiseOffers(
            category_id=category_id,
            subcategory_ids=subcategory_ids,
        ).execute(bot)
    ).response_obj

    wait = response.wait if response.wait > 0 else DEFAULT_WAIT_SECONDS

    if response.error:
        logger.info(
            "Игра %s: %s (повтор через %s мин.)",
            category_id,
            response.msg or "поднятие недоступно",
            wait // 60,
        )
    else:
        logger.info(
            "Игра %s: лоты подняты. %s",
            category_id,
            response.msg,
        )

    # Небольшой запас, чтобы не попасть раньше разблокировки
    return wait + 30


async def raise_loop(bot: Bot = shared_bot) -> None:
    logger.info(
        "Автоподнятие запущено: dry_run=%s, delay=%s сек., interval=%s мин.",
        DRY_RUN,
        DELAY_SECONDS,
        INTERVAL_MINUTES,
    )

    categories: dict[int, tuple[int, ...]] = {}
    categories_loaded_at = 0.0
    next_raise_at: dict[int, float] = {}

    while True:
        now = time.monotonic()

        if not categories or now - categories_loaded_at > LOTS_REFRESH_SECONDS:
            try:
                categories = await get_my_categories(bot)
                categories_loaded_at = now

                logger.info(
                    "Игр с лотами для поднятия: %s %s",
                    len(categories),
                    list(categories),
                )

            except Exception:
                logger.exception("Не удалось получить список своих лотов")

        for category_id, subcategory_ids in categories.items():
            if next_raise_at.get(category_id, 0) > time.monotonic():
                continue

            try:
                retry_after = await raise_category(
                    bot,
                    category_id,
                    subcategory_ids,
                )
            except Exception:
                logger.exception("Ошибка поднятия игры %s", category_id)
                retry_after = ERROR_RETRY_SECONDS

            next_raise_at[category_id] = time.monotonic() + retry_after

            await asyncio.sleep(DELAY_SECONDS)

        # Спим до ближайшего поднятия, но не дольше интервала
        upcoming = [
            next_raise_at[category_id] - time.monotonic()
            for category_id in categories
            if category_id in next_raise_at
        ]

        sleep_seconds = min(upcoming, default=INTERVAL_MINUTES * 60)
        sleep_seconds = max(60, min(sleep_seconds, INTERVAL_MINUTES * 60))

        await asyncio.sleep(sleep_seconds)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        asyncio.run(raise_loop())
    except KeyboardInterrupt:
        print("Бот остановлен пользователем.")
