"""
Общий FunPay-клиент для всех ботов.

Раньше каждый бот создавал свой Bot и сам опрашивал FunPay,
из-за чего с одним golden_key шло 2-3 параллельных потока
запросов и FunPay начинал тормозить. Теперь у всех модулей
одна сессия и один поток событий (listen_events), а каждый
модуль подключает к нему свой Router.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from funpaybotengine import Bot, Dispatcher
from funpaybotengine.storage.inmemory import InMemoryStorage


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

GOLDEN_KEY = os.getenv("FUNPAY_GOLDEN_KEY", "").strip()

if not GOLDEN_KEY:
    raise RuntimeError("Не найден FUNPAY_GOLDEN_KEY в файле .env")


bot = Bot(golden_key=GOLDEN_KEY)
dispatcher = Dispatcher()

# Превью чатов из опроса FunPay: по chat_id даёт ник собеседника.
# Передаётся в listen_events(..., session_storage=chat_storage).
chat_storage = InMemoryStorage()
