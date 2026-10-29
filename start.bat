@echo off
chcp 65001 >nul
title FunPay bots
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist "venv\Scripts\python.exe" (
    echo Не найдено виртуальное окружение venv.
    echo Создайте его: python -m venv venv
    echo и установите зависимости: venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

:loop
echo [%date% %time%] Запуск ботов...
"venv\Scripts\python.exe" run_all.py
echo [%date% %time%] Боты остановлены. Перезапуск через 10 секунд (Ctrl+C - выход)...
timeout /t 10
goto loop
