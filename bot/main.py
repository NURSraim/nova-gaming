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
from bot.notifier import notify_new_booking_direct, notify_player_status_direct


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


async def _start_http_server(port: int, bot: Bot, logger: logging.Logger):
    """HTTP сервер для health-check (Render/UptimeRobot) + мгновенных push-уведомлений без опроса Firestore."""
    async def handle_health(_request):
        return web.Response(text="Nova Gaming Bot OK", headers=CORS_HEADERS)

    async def handle_options(_request):
        return web.Response(status=204, headers=CORS_HEADERS)

    async def handle_notify(request: web.Request):
        try:
            payload = await request.json()
            env = str(payload.get("env") or "prod")
            booking = payload.get("booking") or {}
            club = payload.get("club") or None
            if not booking.get("id"):
                return web.json_response({"ok": False, "error": "missing booking.id"}, status=400, headers=CORS_HEADERS)
            asyncio.create_task(notify_new_booking_direct(bot, env, booking, club))
            return web.json_response({"ok": True}, headers=CORS_HEADERS)
        except Exception as exc:
            logger.error("Ошибка /api/notify: %s", exc)
            return web.json_response({"ok": False, "error": "bad payload"}, status=400, headers=CORS_HEADERS)

    async def handle_notify_status(request: web.Request):
        try:
            payload = await request.json()
            booking = payload.get("booking") or {}
            club = payload.get("club") or None
            new_st = str(payload.get("status") or "")
            if new_st in ("ok", "no"):
                asyncio.create_task(notify_player_status_direct(bot, booking, club, new_st))
            return web.json_response({"ok": True}, headers=CORS_HEADERS)
        except Exception as exc:
            logger.error("Ошибка /api/notify-status: %s", exc)
            return web.json_response({"ok": False}, status=400, headers=CORS_HEADERS)

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_route("OPTIONS", "/api/notify", handle_options)
    app.router.add_post("/api/notify", handle_notify)
    app.router.add_route("OPTIONS", "/api/notify-status", handle_options)
    app.router.add_post("/api/notify-status", handle_notify_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("🌐 HTTP сервер слушает порт %d (/health, /api/notify)", port)
    return runner


async def main():
    """Запуск бота."""

    # Настраиваем логирование (чтобы видеть, что происходит)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger(__name__)

    # Создаём бота и диспетчер
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # Подключаем наши обработчики команд
    dp.include_router(router)

    # Если запущены на облаке (Render / Koyeb / Railway), поднимаем HTTP-сервер (/health + /api/notify)
    http_runner = None
    port_env = os.environ.get("PORT", "").strip()
    if port_env.isdigit():
        http_runner = await _start_http_server(int(port_env), bot, logger)

    # Выводим информацию о боте
    bot_info = await bot.get_me()
    logger.info("=" * 50)
    logger.info("🎮 Nova Gaming Bot запущен! (режим прямых push-уведомлений, 0 опросов Firestore)")
    logger.info(f"🤖 Имя: {bot_info.full_name}")
    logger.info(f"📎 Username: @{bot_info.username}")
    logger.info("=" * 50)
    logger.info("Бот слушает сообщения... (Ctrl+C для остановки)")

    # Удаляем старые апдейты и запускаем polling (без фонового 3-сек опроса Firestore!)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        if http_runner is not None:
            await http_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен. До встречи!")
