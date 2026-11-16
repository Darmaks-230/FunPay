"""
Копирование лотов FunPay с основного аккаунта на дополнительные.

Работает в run_all.py основного аккаунта. Раз в COPY_INTERVAL_SECONDS
смотрит лоты основного аккаунта и создаёт на каждом дополнительном
(FUNPAY_GOLDEN_KEY_2, ...) те, которых там ещё нет: то же название,
описание, цену, количество и раздел.

Каждый лот копируется один раз (funpay_copied_<номер>.json): если лот
удалить на дополнительном аккаунте, он не появится снова. Лот с таким же
названием, который уже есть на дополнительном аккаунте, не дублируется.
Выключить: FUNPAY_COPY_OFFERS=false в .env.
"""

import asyncio
import json
import logging
import os

from funpaybotengine.types.enums import SubcategoryType

import accounts
from lot_descriptions import funpay_extra_bot, funpay_lots


log = logging.getLogger("offer_copy")

COPY_OFFERS = os.getenv("FUNPAY_COPY_OFFERS", "true").lower() in {"1", "true", "yes", "on"}
COPY_INTERVAL_SECONDS = 30 * 60
# Пауза между созданием лотов, чтобы FunPay не ограничил запросы
SAVE_DELAY_SECONDS = 3

# Поля, которые у нового лота свои. Товары автовыдачи (secrets) не
# копируем: один и тот же товар продался бы дважды.
SKIP_FIELDS = {
    "offer_id",
    "form_created_at",
    "csrf_token",
    "node_id",
    "location",
    "deleted",
    "secrets",
    "auto_delivery",
}


def copied_file(number: str):
    return accounts.BASE_DIR / f"funpay_copied_{number}.json"


def load_copied(number: str) -> set[int]:
    try:
        return set(json.loads(copied_file(number).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def save_copied(number: str, copied: set[int]) -> None:
    copied_file(number).write_text(json.dumps(sorted(copied)), encoding="utf-8")


async def copy_offer(source, target, offer_id: int) -> None:
    fields = await source.get_offer_fields(offer_id=offer_id)
    node_id = int(fields.fields_dict["node_id"])

    # Пустая форма нового лота в том же разделе
    new_fields = await target.get_offer_fields(
        subcategory_type=SubcategoryType.OFFERS,
        subcategory_id=node_id,
    )

    for key, value in fields.fields_dict.items():
        if key not in SKIP_FIELDS:
            new_fields.set_field(key, value)

    await target.save_offer_fields(new_fields)


async def copy_to_account(source, number: str) -> None:
    target = funpay_extra_bot(number)
    if target is None:
        return

    copied = load_copied(number)
    target_titles = {title for _, title in await funpay_lots(target)}
    created = 0

    for offer_id, title in await funpay_lots(source):
        if offer_id in copied:
            continue

        if title not in target_titles:
            try:
                await copy_offer(source, target, offer_id)
            except Exception as error:
                # Попробуем в следующий раз
                log.error("FunPay %s: не удалось скопировать лот «%s»: %s", number, title[:60], error)
                await asyncio.sleep(SAVE_DELAY_SECONDS)
                continue

            created += 1
            target_titles.add(title)
            log.info("FunPay %s: скопирован лот «%s»", number, title[:60])
            await asyncio.sleep(SAVE_DELAY_SECONDS)

        copied.add(offer_id)
        save_copied(number, copied)

    if created:
        log.info("FunPay %s: скопировано лотов с основного аккаунта: %s", number, created)


async def copy_loop() -> None:
    from funpay_shared import FUNPAY_ENABLED, bot

    if not COPY_OFFERS or not FUNPAY_ENABLED:
        await asyncio.Event().wait()

    while True:
        for number in accounts.extra_accounts():
            try:
                await copy_to_account(bot, number)
            except Exception:
                log.exception("FunPay %s: ошибка копирования лотов", number)

        await asyncio.sleep(COPY_INTERVAL_SECONDS)
