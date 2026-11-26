"""
Nova Gaming — Защищённый HTTP API Сервер (aiohttp + Telegram HMAC-SHA256 + Firestore)
Запуск: py bot/server.py
Адрес полигона: http://127.0.0.1:8080/dev
"""

import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

# Добавляем корень проекта в sys.path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

from aiohttp import ClientSession, ClientTimeout, web
from bot.config import BOT_TOKEN
from bot.security import (
    create_mock_init_data,
    get_user_role,
    validate_init_data,
)

logger = logging.getLogger("nova.server")

FIREBASE_PROJECT_ID = "nova-gaming-6c610"
FIRESTORE_BASE_URL = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
    f"/databases/(default)/documents"
)

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
HTTP_TIMEOUT = ClientTimeout(total=8)

# Тестовые персоны для полигона dev.html (доступны только в DEV_MODE)
DEV_MODE = True
DEV_PERSONAS: dict[str, dict[str, Any]] = {
    "user": {"id": 1001, "username": "test_player", "first_name": "Нурик (Игрок)"},
    "admin": {"id": 2001, "username": "nova_admin_1", "first_name": "Админ Арены #1"},
    "admin2": {"id": 2002, "username": "next_admin_2", "first_name": "Админ Арены #2"},
    "sys": {"id": 9999, "username": "nova_root", "first_name": "Главный Сис-Админ"},
}


def _safe_int(val: Any, default: int = -1) -> int:
    if isinstance(val, bool):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _to_firestore_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Конвертирует обычный Python dict в формат полей Firestore REST API."""
    fields: dict[str, Any] = {}
    for k, v in data.items():
        if v is None:
            fields[k] = {"nullValue": None}
        elif isinstance(v, bool):
            fields[k] = {"booleanValue": v}
        elif isinstance(v, int):
            fields[k] = {"integerValue": str(v)}
        elif isinstance(v, float):
            fields[k] = {"doubleValue": v}
        elif isinstance(v, str):
            fields[k] = {"stringValue": v}
    return {"fields": fields}


def _from_firestore_fields(doc: dict[str, Any]) -> dict[str, Any]:
    """Конвертирует документ Firestore REST API обратно в обычный Python dict."""
    out: dict[str, Any] = {}
    for k, v in (doc.get("fields") or {}).items():
        if "integerValue" in v:
            out[k] = int(v["integerValue"])
        elif "doubleValue" in v:
            out[k] = float(v["doubleValue"])
        elif "booleanValue" in v:
            out[k] = bool(v["booleanValue"])
        elif "stringValue" in v:
            out[k] = str(v["stringValue"])
        elif "nullValue" in v:
            out[k] = None
    return out


async def firestore_get(
    session: ClientSession,
    collection: str,
    doc_id: str,
) -> dict[str, Any] | None:
    """Читает документ из Cloud Firestore для серверной проверки прав."""
    if not SAFE_ID_RE.match(collection) or not SAFE_ID_RE.match(doc_id):
        return None
    url = f"{FIRESTORE_BASE_URL}/{collection}/{doc_id}"
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status == 200:
                raw = await resp.json()
                return _from_firestore_fields(raw)
            return None
    except Exception as exc:
        logger.error("Ошибка чтения из Firestore: %s", exc)
        return None


async def firestore_upsert(
    session: ClientSession,
    collection: str,
    doc_id: str,
    data: dict[str, Any],
) -> bool:
    """Записывает или обновляет документ в Cloud Firestore от имени сервера."""
    if not SAFE_ID_RE.match(collection) or not SAFE_ID_RE.match(doc_id):
        return False
    url = f"{FIRESTORE_BASE_URL}/{collection}/{doc_id}"
    payload = _to_firestore_fields(data)
    try:
        async with session.patch(url, json=payload, timeout=HTTP_TIMEOUT) as resp:
            if resp.status in (200, 201):
                return True
            err_text = await resp.text()
            logger.error("Ошибка записи в Firestore (%s): %s", resp.status, err_text)
            return False
    except Exception as exc:
        logger.error("Сетевая ошибка Firestore: %s", exc)
        return False


def authenticate_request(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """
    Охранник на входе каждого защищённого эндпоинта.
    Достаёт заголовок X-Telegram-Init-Data, проверяет HMAC-SHA256 через security.py
    и возвращает проверенного пользователя и его роль из RBAC.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not init_data:
        logger.warning("⛔ Отклонён запрос без заголовка X-Telegram-Init-Data")
        return None, web.json_response(
            {"ok": False, "error": "Отсутствует криптографическая подпись Telegram (initData)"},
            status=401,
        )

    verified = validate_init_data(init_data, BOT_TOKEN)
    if not verified:
        logger.warning("⛔ Отклонён запрос с фальшивой или устаревшей подписью initData!")
        return None, web.json_response(
            {"ok": False, "error": "Подпись Telegram недействительна или подделана (HMAC mismatch)!"},
            status=403,
        )

    user = verified["user"]
    role_info = get_user_role(user["id"])
    auth_ctx = {
        "user": user,
        "role": role_info["role"],
        "club_id": role_info["club_id"],
        "title": role_info["title"],
        "auth_date": verified["auth_date"],
    }
    return auth_ctx, None


async def _parse_json_dict(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Разрешает запросы с локального dev.html / index.html."""
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
        except Exception as exc:
            logger.exception("Внутренняя ошибка сервера: %s", exc)
            resp = web.json_response({"ok": False, "error": "Внутренняя ошибка сервера"}, status=500)

    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    return resp


async def handle_index(request: web.Request) -> web.Response:
    path = ROOT_DIR / "webapp" / "index.html"
    return web.FileResponse(path)


async def handle_dev(request: web.Request) -> web.Response:
    path = ROOT_DIR / "webapp" / "dev.html"
    return web.FileResponse(path)


async def handle_dev_token(request: web.Request) -> web.Response:
    """
    Выдаёт подписанный HMAC-SHA256 тестовый initData для выбранной персоны на полигоне.
    Работает только когда DEV_MODE = True.
    """
    if not DEV_MODE:
        return web.json_response({"ok": False, "error": "Dev mode отключён"}, status=403)

    persona_key = request.query.get("persona", "user")
    persona = DEV_PERSONAS.get(persona_key, DEV_PERSONAS["user"])

    signed_init_data = create_mock_init_data(
        user_id=persona["id"],
        username=persona["username"],
        first_name=persona["first_name"],
        bot_token=BOT_TOKEN,
    )
    role_info = get_user_role(persona["id"])
    logger.info(
        "🔑 Выдан подписанный паспорт персоны '%s' (ID=%s, роль=%s)",
        persona_key,
        persona["id"],
        role_info["role"],
    )
    return web.json_response({
        "ok": True,
        "persona": persona_key,
        "initData": signed_init_data,
        "user": persona,
        "role": role_info["role"],
        "club_id": role_info["club_id"],
        "title": role_info["title"],
    })


async def handle_auth_verify(request: web.Request) -> web.Response:
    """Проверяет подпись initData и возвращает подтверждённую роль пользователя."""
    auth_ctx, err_resp = authenticate_request(request)
    if err_resp:
        return err_resp
    assert auth_ctx is not None
    return web.json_response({"ok": True, **auth_ctx})


async def handle_create_booking(request: web.Request) -> web.Response:
    """
    Защищённое создание брони игроком.
    ownerId берётся СТРОГО из проверенного Telegram паспорта, а не из тела запроса!
    """
    auth_ctx, err_resp = authenticate_request(request)
    if err_resp:
        return err_resp
    assert auth_ctx is not None

    body = await _parse_json_dict(request)
    if body is None:
        return web.json_response({"ok": False, "error": "Некорректный JSON-объект"}, status=400)

    collection = "dev_bookings" if body.get("env") == "dev" else "bookings"
    club_id = _safe_int(body.get("club"), 0)
    seat = str(body.get("seat", "")).strip()
    day = _safe_int(body.get("day"), -1)
    start = _safe_int(body.get("start"), -1)
    hours = _safe_int(body.get("hours"), 0)
    price = _safe_int(body.get("price"), 0)
    shot = str(body.get("shot", "")).strip()

    if (
        club_id <= 0
        or not SAFE_ID_RE.match(seat)
        or day not in (0, 1, 2)
        or not (0 <= start <= 23)
        or not (1 <= hours <= 12)
        or price <= 0
    ):
        return web.json_response({"ok": False, "error": "Некорректные параметры бронирования"}, status=400)
    if not shot:
        return web.json_response({"ok": False, "error": "Скриншот чека обязателен"}, status=400)

    now_ms = int(time.time() * 1000)
    verified_uid = str(auth_ctx["user"]["id"])
    raw_id = str(body.get("id") or f"bk_{now_ms}_{verified_uid}")
    booking_id = raw_id if SAFE_ID_RE.match(raw_id) else f"bk_{now_ms}_{verified_uid}"

    doc_data = {
        "id": booking_id,
        "club": club_id,
        "seat": seat,
        "day": day,
        "start": start,
        "hours": hours,
        "price": price,
        "st": "new",
        "by": "user",
        "ownerId": verified_uid,
        "ownerName": str(auth_ctx["user"].get("first_name", "Игрок")),
        "shot": shot[:120],
        "created": now_ms,
    }

    session: ClientSession = request.app["http_session"]
    saved = await firestore_upsert(session, collection, booking_id, doc_data)
    if not saved:
        return web.json_response({"ok": False, "error": "Ошибка сохранения в Firestore"}, status=502)

    logger.info(
        "✅ Создана бронь %s: Игрок ID=%s -> Клуб #%s, ПК %s (%s ч.)",
        booking_id,
        verified_uid,
        club_id,
        seat,
        hours,
    )
    return web.json_response({"ok": True, "booking": doc_data})


async def handle_decide_booking(request: web.Request) -> web.Response:
    """
    Защищённое подтверждение или отклонение брони.
    Разрешено ТОЛЬКО админу соответствующего клуба (проверяется по реальному документу в БД!) или sys.
    """
    auth_ctx, err_resp = authenticate_request(request)
    if err_resp:
        return err_resp
    assert auth_ctx is not None

    if auth_ctx["role"] not in ("admin", "sys"):
        logger.warning(
            "⛔ Игрок ID=%s попытался вызвать админский метод decide_booking!",
            auth_ctx["user"]["id"],
        )
        return web.json_response(
            {"ok": False, "error": "Доступ запрещён! Требуются права администратора клуба."},
            status=403,
        )

    body = await _parse_json_dict(request)
    if body is None:
        return web.json_response({"ok": False, "error": "Некорректный JSON-объект"}, status=400)

    collection = "dev_bookings" if body.get("env") == "dev" else "bookings"
    booking_id = str(body.get("id", "")).strip()
    new_status = str(body.get("st", "")).strip()

    if not SAFE_ID_RE.match(booking_id) or new_status not in ("ok", "no"):
        return web.json_response({"ok": False, "error": "Некорректный ID или статус"}, status=400)

    session: ClientSession = request.app["http_session"]

    # Читаем реальный документ брони из Firestore, чтобы админ Клуба #1 не мог подделать club_id в запросе!
    existing = await firestore_get(session, collection, booking_id)
    if not existing:
        return web.json_response({"ok": False, "error": "Бронь не найдена в базе"}, status=404)

    real_club_id = _safe_int(existing.get("club"), 0)
    if auth_ctx["role"] == "admin" and auth_ctx["club_id"] != real_club_id:
        logger.warning(
            "⛔ Админ клуба #%s (ID=%s) попытался изменить бронь чужого клуба #%s!",
            auth_ctx["club_id"],
            auth_ctx["user"]["id"],
            real_club_id,
        )
        return web.json_response(
            {
                "ok": False,
                "error": f"Вы администратор Клуба #{auth_ctx['club_id']} и не можете управлять Клубом #{real_club_id}!",
            },
            status=403,
        )

    existing["st"] = new_status
    saved = await firestore_upsert(session, collection, booking_id, existing)
    if not saved:
        return web.json_response({"ok": False, "error": "Ошибка обновления статуса в Firestore"}, status=502)

    logger.info(
        "🛡️ Админ ID=%s (%s) изменил статус брони %s -> '%s'",
        auth_ctx["user"]["id"],
        auth_ctx["title"],
        booking_id,
        new_status,
    )
    return web.json_response({"ok": True, "id": booking_id, "st": new_status})


async def handle_manual_booking(request: web.Request) -> web.Response:
    """Защищённая ручная посадка от стойки (только админ этого клуба или sys)."""
    auth_ctx, err_resp = authenticate_request(request)
    if err_resp:
        return err_resp
    assert auth_ctx is not None

    if auth_ctx["role"] not in ("admin", "sys"):
        return web.json_response(
            {"ok": False, "error": "Доступ запрещён! Только админ клуба может сажать гостей вручную."},
            status=403,
        )

    body = await _parse_json_dict(request)
    if body is None:
        return web.json_response({"ok": False, "error": "Некорректный JSON-объект"}, status=400)

    collection = "dev_bookings" if body.get("env") == "dev" else "bookings"
    club_id = _safe_int(body.get("club"), 0)
    seat = str(body.get("seat", "")).strip()
    day = _safe_int(body.get("day"), -1)
    start = _safe_int(body.get("start"), -1)
    hours = _safe_int(body.get("hours"), 0)

    if auth_ctx["role"] == "admin" and auth_ctx["club_id"] != club_id:
        return web.json_response(
            {
                "ok": False,
                "error": f"Вы администратор Клуба #{auth_ctx['club_id']} и не можете сажать гостей в Клуб #{club_id}!",
            },
            status=403,
        )

    if (
        club_id <= 0
        or not SAFE_ID_RE.match(seat)
        or day not in (0, 1, 2)
        or not (0 <= start <= 23)
        or not (1 <= hours <= 12)
    ):
        return web.json_response({"ok": False, "error": "Некорректные параметры посадки"}, status=400)

    now_ms = int(time.time() * 1000)
    raw_id = str(body.get("id") or f"bk_{now_ms}_manual")
    booking_id = raw_id if SAFE_ID_RE.match(raw_id) else f"bk_{now_ms}_manual"
    doc_data = {
        "id": booking_id,
        "club": club_id,
        "seat": seat,
        "day": day,
        "start": start,
        "hours": hours,
        "st": "ok",
        "by": "manual",
        "adminId": str(auth_ctx["user"]["id"]),
        "created": now_ms,
    }

    session: ClientSession = request.app["http_session"]
    saved = await firestore_upsert(session, collection, booking_id, doc_data)
    if not saved:
        return web.json_response({"ok": False, "error": "Ошибка записи в Firestore"}, status=502)

    logger.info("⚡ Ручная посадка: Админ ID=%s -> Клуб #%s, ПК %s", auth_ctx["user"]["id"], club_id, seat)
    return web.json_response({"ok": True, "booking": doc_data})


async def handle_club_action(request: web.Request) -> web.Response:
    """Одобрение/отклонение подключения клуба к платформе. Разрешено ТОЛЬКО Сис-Админу (role == 'sys')."""
    auth_ctx, err_resp = authenticate_request(request)
    if err_resp:
        return err_resp
    assert auth_ctx is not None

    if auth_ctx["role"] != "sys":
        logger.warning(
            "⛔ Пользователь ID=%s (роль=%s) попытался вызвать ROOT-метод club_action!",
            auth_ctx["user"]["id"],
            auth_ctx["role"],
        )
        return web.json_response(
            {"ok": False, "error": "Доступ запрещён! Требуется уровень допуска ROOT (Главный Сис-Админ)."},
            status=403,
        )

    body = await _parse_json_dict(request)
    if body is None:
        return web.json_response({"ok": False, "error": "Некорректный JSON-объект"}, status=400)

    collection = "dev_clubs_status" if body.get("env") == "dev" else "clubs_status"
    club_id = _safe_int(body.get("id"), 0)
    approve = bool(body.get("ok", False))
    sub_paid = bool(body.get("subPaid", True))
    suspended = bool(body.get("suspended", False))
    if club_id <= 0:
        return web.json_response({"ok": False, "error": "Некорректный ID клуба"}, status=400)

    doc_data = {
        "id": club_id,
        "ok": approve,
        "gone": not approve and bool(body.get("gone", True)),
        "subPaid": sub_paid,
        "suspended": suspended,
    }
    session: ClientSession = request.app["http_session"]
    saved = await firestore_upsert(session, collection, str(club_id), doc_data)
    if not saved:
        return web.json_response({"ok": False, "error": "Ошибка обновления клуба в Firestore"}, status=502)

    logger.info(
        "👑 Сис-Админ ID=%s изменил статус Клуба #%s -> ok=%s, subPaid=%s, suspended=%s",
        auth_ctx["user"]["id"],
        club_id,
        approve,
        sub_paid,
        suspended,
    )
    return web.json_response({"ok": True, "club": doc_data})


async def on_startup(app: web.Application):
    app["http_session"] = ClientSession()


async def on_cleanup(app: web.Application):
    session: ClientSession | None = app.get("http_session")
    if session:
        await session.close()


def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_get("/dev", handle_dev)
    app.router.add_get("/api/dev-token", handle_dev_token)
    app.router.add_post("/api/auth/verify", handle_auth_verify)
    app.router.add_post("/api/bookings/create", handle_create_booking)
    app.router.add_post("/api/bookings/decide", handle_decide_booking)
    app.router.add_post("/api/bookings/manual", handle_manual_booking)
    app.router.add_post("/api/clubs/action", handle_club_action)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    PORT = 8080
    logger.info("=" * 60)
    logger.info("🛡️ NOVA GAMING SECURE API SERVER ЗАПУЩЕН")
    logger.info("👉 Тестовый полигон (3 роли): http://127.0.0.1:%s/dev", PORT)
    logger.info("👉 Боевой интерфейс:          http://127.0.0.1:%s/", PORT)
    logger.info("=" * 60)
    web.run_app(create_app(), host="127.0.0.1", port=PORT, print=None)
