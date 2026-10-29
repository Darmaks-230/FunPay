# FunPay farm orders

Боты для продавца на FunPay:

| Файл | Что делает |
|---|---|
| `main.py` | Discord-бот с таблицей заказов, которую можно менять командами |
| `FunPay_commands.py` | Отвечает на команды `/money`, `/info`, `/help` в чатах FunPay и уведомляет о завершении фарма |
| `FunPay_messages.py` | Пересылает сообщения FunPay в отдельные каналы Discord |
| `auto_raise.py` | Автоматически поднимает свои лоты на FunPay |
| `run_all.py` | Запускает всех ботов сразу; FunPay-боты работают через одно подключение (`funpay_shared.py`) |

## Установка

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

Скопируйте `.env.example` в `.env` и заполните значения.

## Запуск

Двойной клик по `start.bat`. Логи пишутся в `logs/bots.log`.
