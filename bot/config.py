"""
Nova Gaming — Конфигурация бота
Загружаем токен из .env файла, чтобы он не светился в коде
"""
import os
from pathlib import Path

# Читаем .env файл вручную (без лишних зависимостей)
env_path = Path(__file__).parent.parent / ".env"

if env_path.exists():
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

# Токен бота из BotFather
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")

if not BOT_TOKEN or BOT_TOKEN == "СЮДА_ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER":
    raise ValueError(
        "❌ Токен не найден! Открой файл .env и вставь токен от @BotFather"
    )

# URL твоего Mini App (пока заглушка, потом заменим на реальный)
WEBAPP_URL: str = os.environ.get("WEBAPP_URL", "")
