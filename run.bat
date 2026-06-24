@echo off
REM Запуск бота из его папки через venv. Используется и вручную, и службой.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" bot.py
) else (
    python bot.py
)
