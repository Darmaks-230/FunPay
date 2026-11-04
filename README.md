# FunPay farm orders

Боты для продавца на FunPay и Starvell:

| Файл | Что делает |
|---|---|
| `main.py` | Discord-бот с таблицей заказов, которую можно менять командами |
| `FunPay_commands.py` | Отвечает на команды `/money`, `/info`, `/help` в чатах FunPay и уведомляет о завершении фарма |
| `FunPay_messages.py` | Пересылает сообщения FunPay в отдельные каналы Discord |
| `auto_raise.py` | Автоматически поднимает свои лоты на FunPay |
| `Starvell_bot.py` | То же для Starvell: команды в чате, сообщения в Discord, продажи в общую таблицу, автоподнятие (клиент — `starvell_api.py`) |
| `lot_descriptions.py` | Меняет полное описание лотов монет, гемов или опыта на FunPay и Starvell по команде `/lot-description` в Discord; в описании опыта `{tower}` заменяется названием башни из названия лота |
| `run_all.py` | Запускает всех ботов сразу; FunPay-боты работают через одно подключение (`funpay_shared.py`) |

## Установка

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

Скопируйте `.env.example` в `.env` и заполните значения.

Для Starvell нужна cookie `session`: войдите на starvell.com, F12 → Application →
Cookies → `https://starvell.com`, скопируйте значение `session` в `STARVELL_SESSION`.
Без неё бот Starvell просто не запускается, остальные работают.

## Запуск

Двойной клик по `start.bat`. Логи пишутся в `logs/bots.log`.
