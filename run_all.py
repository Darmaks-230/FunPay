"""
Запуск всех ботов одной командой (см. start.bat).

- main.py              — Discord-таблица, отдельный процесс
- FunPay_commands.py   — команды /money, /info, /help + проверка фарма
- FunPay_messages.py   — пересылка сообщений FunPay в Discord
- auto_raise.py        — автоподнятие лотов
- Starvell_bot.py      — то же для Starvell: команды, сообщения в Discord,
                         продажи в общую таблицу, автоподнятие

Все FunPay-модули работают через ОДНО подключение к FunPay
(funpay_shared.py): один опрос событий вместо двух и одна сессия
для поднятия. Если какой-то бот упадёт, он перезапустится сам,
остальные продолжат работать.
"""

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from funpay_shared import BASE_DIR, bot, chat_storage, dispatcher

import FunPay_commands
import FunPay_messages
import FunPay_sales
import Starvell_bot
import auto_raise


RESTART_DELAY_SECONDS = 15

# Даём основному опросу FunPay подключиться, потом поднимаем лоты
RAISE_START_DELAY_SECONDS = 20


def setup_logging() -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    file_handler = RotatingFileHandler(
        log_dir / "bots.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            file_handler,
            logging.StreamHandler(),
        ],
    )

    # discord.py очень разговорчив на INFO
    logging.getLogger("discord").setLevel(logging.WARNING)


async def supervise(name, start_coroutine, start_delay: float = 0) -> None:
    """Запускает задачу и перезапускает её, если она упала или завершилась."""

    logger = logging.getLogger("run_all")

    if start_delay:
        await asyncio.sleep(start_delay)

    while True:
        try:
            logger.info("▶ %s запущен", name)
            await start_coroutine()
            logger.warning("%s завершился", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s упал", name)

        logger.info(
            "%s будет перезапущен через %s сек.",
            name,
            RESTART_DELAY_SECONDS,
        )
        await asyncio.sleep(RESTART_DELAY_SECONDS)


async def run_main_table_bot() -> None:
    """main.py вызывает bot.run() при импорте, поэтому идёт отдельным процессом."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        str(BASE_DIR / "main.py"),
        cwd=str(BASE_DIR),
    )

    try:
        return_code = await process.wait()
        logging.getLogger("run_all").warning(
            "main.py завершился с кодом %s",
            return_code,
        )
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


async def run_funpay_messages_discord() -> None:
    client = FunPay_messages.discord_client

    if client.is_closed():
        client.clear()

    await client.start(FunPay_messages.DISCORD_TOKEN)


async def main() -> None:
    setup_logging()

    FunPay_commands.setup()

    dispatcher.connect_routers(
        FunPay_commands.router,
        FunPay_messages.router,
        FunPay_sales.router,
    )

    await asyncio.gather(
        supervise("Discord-таблица (main.py)", run_main_table_bot),
        supervise(
            "Опрос FunPay",
            lambda: bot.listen_events(dispatcher, session_storage=chat_storage),
        ),
        supervise("Discord для сообщений FunPay", run_funpay_messages_discord),
        supervise("Очистка старых каналов", FunPay_messages.cleanup_inactive_channels),
        supervise("Проверка фарма", FunPay_commands.farm_checker),
        supervise(
            "Автоподнятие",
            auto_raise.raise_loop,
            start_delay=RAISE_START_DELAY_SECONDS,
        ),
        supervise("Starvell", Starvell_bot.run),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Все боты остановлены.")
