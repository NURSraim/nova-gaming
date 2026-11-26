"""
Nova Gaming — Точка входа 🎮
Запуск: py bot/main.py
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path, чтобы импорты работали
sys.path.insert(0, str(Path(__file__).parent.parent))

from aiohttp import web
from aiogram import Bot, Dispatcher
from bot.config import BOT_TOKEN
from bot.handlers import router
from bot.notifier import run_booking_watcher


async def _start_health_server(port: int, logger: logging.Logger):
    """Мини-HTTP сервер для Render / Railway / Koyeb (чтобы бесплатный хостинг держал бота 24/7)."""
    async def handle_health(_request):
        return web.Response(text="Nova Gaming Bot OK")

    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("🌐 Health-check сервер слушает порт %d (/health)", port)
    return runner


async def main():
    """Запуск бота."""

    # Настраиваем логирование (чтобы видеть, что происходит)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger(__name__)

    # Если запущены на облаке (Render / Koyeb / Railway), поднимаем health-check порт
    health_runner = None
    port_env = os.environ.get("PORT", "").strip()
    if port_env.isdigit():
        health_runner = await _start_health_server(int(port_env), logger)

    # Создаём бота и диспетчер
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # Подключаем наши обработчики команд
    dp.include_router(router)

    # Выводим информацию о боте
    bot_info = await bot.get_me()
    logger.info("=" * 50)
    logger.info(f"🎮 Nova Gaming Bot запущен!")
    logger.info(f"🤖 Имя: {bot_info.full_name}")
    logger.info(f"📎 Username: @{bot_info.username}")
    logger.info("=" * 50)
    logger.info("Бот слушает сообщения... (Ctrl+C для остановки)")

    # Удаляем старые апдейты и запускаем polling + фоновый наблюдатель уведомлений
    await bot.delete_webhook(drop_pending_updates=True)
    watcher_task = asyncio.create_task(run_booking_watcher(bot))
    try:
        await dp.start_polling(bot)
    finally:
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass
        if health_runner is not None:
            await health_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен. До встречи!")
