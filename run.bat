@echo off
chcp 65001 > nul
title Tanbox News AI - Казахстан (ИС МПТ / Маркировка)
echo ========================================================
echo    Tanbox News AI - Казахстан (ИС МПТ / Маркировка)
echo ========================================================

if exist ".venv\Scripts\python.exe" (
    echo [OK] Используется виртуальное окружение .venv...
    .venv\Scripts\python.exe bot.py
) else if exist "venv\Scripts\python.exe" (
    echo [OK] Используется виртуальное окружение venv...
    venv\Scripts\python.exe bot.py
) else (
    echo [OK] Запуск через системный Python...
    python bot.py
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Бот завершил работу с ошибкой. Проверьте настройки в .env
)
pause
