"""
Nova Gaming — Живые Telegram-уведомления админам и игрокам (Firestore -> Telegram)
Следит за dev_bookings (полигон) и bookings (прод) каждые 3 секунды.
"""
import asyncio
import base64
import html
import json
import logging
from typing import Any

from aiohttp import ClientSession, ClientTimeout
from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

logger = logging.getLogger("nova.notifier")

FIREBASE_PROJECT_ID = "nova-gaming-6c610"
FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
    f"/databases/(default)/documents"
)
HTTP_TIMEOUT = ClientTimeout(total=8)

# Резервный Root Telegram ID (если у клуба в dev только тестовые ID < 10000)
ROOT_FALLBACK_TG_ID = 5619771997

DEFAULT_CLUB_NAMES: dict[int, str] = {
    1: "Nova Cyber Arena",
    2: "Next Level Esports",
    3: "Pixel Zone Lounge",
    4: "Level Up Bootcamp",
}

DAYS_RU = ["Сегодня", "Завтра", "Послезавтра"]


def _from_fs_value(v: dict[str, Any]) -> Any:
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "booleanValue" in v:
        return bool(v["booleanValue"])
    if "stringValue" in v:
        return str(v["stringValue"])
    if "arrayValue" in v:
        vals = v["arrayValue"].get("values") or []
        return [_from_fs_value(item) for item in vals]
    if "mapValue" in v:
        flds = v["mapValue"].get("fields") or {}
        return {k: _from_fs_value(val) for k, val in flds.items()}
    return None


def _from_fs_doc(doc: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (doc.get("fields") or {}).items():
        out[k] = _from_fs_value(v)
    return out


async def _fs_list(session: ClientSession, collection: str) -> list[dict[str, Any]] | None:
    url = f"{FIRESTORE_BASE}/{collection}?pageSize=200"
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()
            docs = payload.get("documents") or []
            return [_from_fs_doc(d) for d in docs]
    except Exception as exc:
        logger.debug("Firestore list error (%s): %s", collection, exc)
        return None


async def is_user_authorized_for_booking(env: str, booking_id: str, user_id: int) -> bool:
    """Проверяет, что user_id — Root или реальный админ клуба, к которому относится бронь."""
    if not user_id:
        return False
    if int(user_id) == ROOT_FALLBACK_TG_ID:
        return True
    bk_col = "dev_bookings" if env == "dev" else "bookings"
    club_col = "dev_clubs_status" if env == "dev" else "clubs_status"
    try:
        async with ClientSession() as session:
            async with session.get(f"{FIRESTORE_BASE}/{bk_col}/{booking_id}", timeout=HTTP_TIMEOUT) as r_bk:
                if r_bk.status != 200:
                    return False
                b_doc = _from_fs_doc(await r_bk.json())
            club_id = int(b_doc.get("club") or 0)
            if not club_id:
                return False
            async with session.get(f"{FIRESTORE_BASE}/{club_col}/{club_id}", timeout=HTTP_TIMEOUT) as r_cl:
                club_doc = _from_fs_doc(await r_cl.json()) if r_cl.status == 200 else None
            return int(user_id) in _extract_admin_ids(club_doc)
    except Exception as exc:
        logger.error("Ошибка проверки прав админа: %s", exc)
        return False


async def update_booking_status(env: str, booking_id: str, new_status: str) -> bool:
    """Обновляет только поле `st` брони в Firestore (не затирая чек и остальные поля)."""
    col = "dev_bookings" if env == "dev" else "bookings"
    url = f"{FIRESTORE_BASE}/{col}/{booking_id}?updateMask.fieldPaths=st"
    body = {"fields": {"st": {"stringValue": new_status}}}
    try:
        async with ClientSession() as session:
            async with session.patch(url, json=body, timeout=HTTP_TIMEOUT) as resp:
                if resp.status in (200, 201):
                    logger.info("✅ Firestore обновлён: %s/%s -> st=%s", col, booking_id, new_status)
                    return True
                err = await resp.text()
                logger.error("Ошибка обновления брони %s/%s: %s", col, booking_id, err)
                return False
    except Exception as exc:
        logger.error("Сетевая ошибка обновления брони: %s", exc)
        return False


def _extract_admin_ids(club_doc: dict[str, Any] | None) -> list[int]:
    """Достаёт список реальных Telegram ID админов клуба (игнорируя тестовые < 10000)."""
    raw_list: list[Any] = []
    if club_doc:
        if isinstance(club_doc.get("adminIds"), list):
            raw_list = club_doc["adminIds"]
        elif isinstance(club_doc.get("adminIdsJson"), str):
            try:
                parsed = json.loads(club_doc["adminIdsJson"])
                if isinstance(parsed, list):
                    raw_list = parsed
            except Exception:
                pass

    real_ids: list[int] = []
    for item in raw_list:
        try:
            uid = int(str(item).strip())
            if uid >= 10000 and uid not in real_ids:
                real_ids.append(uid)
        except ValueError:
            continue

    if not real_ids:
        real_ids.append(ROOT_FALLBACK_TG_ID)
    return real_ids


def _get_club_name(club_id: int, club_doc: dict[str, Any] | None) -> str:
    if club_doc:
        if club_doc.get("name"):
            return str(club_doc["name"])
        if isinstance(club_doc.get("customClubJson"), str):
            try:
                meta = json.loads(club_doc["customClubJson"])
                if meta.get("name"):
                    return str(meta["name"])
            except Exception:
                pass
    return DEFAULT_CLUB_NAMES.get(club_id, f"Клуб #{club_id}")


def _format_booking_time(b: dict[str, Any]) -> str:
    day_idx = int(b.get("day") or 0)
    day_lbl = DAYS_RU[day_idx] if 0 <= day_idx < len(DAYS_RU) else "Сегодня"
    start_h = int(b.get("start") or 0)
    hrs = int(b.get("hours") or 1)
    end_h = (start_h + hrs) % 24
    return f"{day_lbl}, {start_h:02d}:00–{end_h:02d}:00 ({hrs} ч)"


def _decode_shot_bytes(shot_img: str | None) -> bytes | None:
    if not shot_img or not isinstance(shot_img, str):
        return None
    if "," in shot_img and shot_img.startswith("data:image"):
        try:
            b64_part = shot_img.split(",", 1)[1]
            return base64.b64decode(b64_part)
        except Exception:
            return None
    return None


async def _notify_admins_new_booking(
    bot: Bot,
    env: str,
    b: dict[str, Any],
    club_doc: dict[str, Any] | None,
) -> None:
    booking_id = str(b.get("id") or "")
    club_id = int(b.get("club") or 1)
    club_name = html.escape(_get_club_name(club_id, club_doc))
    seat = html.escape(str(b.get("seat") or "?"))
    time_str = html.escape(_format_booking_time(b))
    price = int(b.get("price") or 0)
    price_str = f"{price:,}".replace(",", " ") + " сум"
    shot_name = html.escape(str(b.get("shot") or "прикреплён"))
    env_tag = " [DEV]" if env == "dev" else ""

    text = (
        f"🔔 <b>НОВАЯ ЗАЯВКА НА БРОНЬ{env_tag}</b>\n\n"
        f"🏢 <b>Клуб:</b> {club_name}\n"
        f"🖥️ <b>Место:</b> #{seat}\n"
        f"🕒 <b>Время:</b> {time_str}\n"
        f"💰 <b>Сумма:</b> <code>{price_str}</code>\n"
        f"📎 <b>Чек:</b> {shot_name}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"bk_ok:{env}:{booking_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"bk_no:{env}:{booking_id}",
                ),
            ]
        ]
    )

    img_bytes = _decode_shot_bytes(b.get("shotImg"))
    recipients = _extract_admin_ids(club_doc)

    for admin_uid in recipients:
        try:
            if img_bytes:
                photo = BufferedInputFile(img_bytes, filename="receipt.jpg")
                await bot.send_photo(
                    chat_id=admin_uid,
                    photo=photo,
                    caption=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=admin_uid,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            logger.info("📨 Уведомление о брони %s отправлено админу TG ID=%s", booking_id, admin_uid)
        except Exception as exc:
            logger.warning("⚠️ Не удалось отправить пуш админу %s: %s", admin_uid, exc)


async def _notify_player_status(
    bot: Bot,
    b: dict[str, Any],
    club_doc: dict[str, Any] | None,
    new_st: str,
) -> None:
    owner_raw = str(b.get("ownerId") or "").strip()
    if not owner_raw.isdigit():
        return
    owner_uid = int(owner_raw)
    if owner_uid < 10000:
        return  # Пропускаем тестовые mock-ID (1001 и т.д.)

    club_id = int(b.get("club") or 1)
    club_name = html.escape(_get_club_name(club_id, club_doc))
    seat = html.escape(str(b.get("seat") or "?"))
    time_str = html.escape(_format_booking_time(b))

    if new_st == "ok":
        msg = (
            f"🎉 <b>Ваша бронь подтверждена!</b>\n\n"
            f"🏢 <b>Клуб:</b> {club_name}\n"
            f"🖥️ <b>Место:</b> #{seat}\n"
            f"🕒 <b>Время:</b> {time_str}\n\n"
            f"Ждём вас в клубе! Приятной игры 🎮"
        )
    elif new_st == "no":
        msg = (
            f"❌ <b>Заявка на бронь отклонена</b>\n\n"
            f"🏢 <b>Клуб:</b> {club_name}\n"
            f"🖥️ <b>Место:</b> #{seat}\n"
            f"🕒 <b>Время:</b> {time_str}\n\n"
            f"Если перевод был выполнен — свяжитесь с администратором клуба."
        )
    else:
        return

    try:
        await bot.send_message(chat_id=owner_uid, text=msg, parse_mode="HTML")
        logger.info("📨 Статус '%s' отправлен игроку TG ID=%s", new_st, owner_uid)
    except Exception as exc:
        logger.warning("⚠️ Не удалось уведомить игрока %s: %s", owner_uid, exc)


async def run_booking_watcher(bot: Bot) -> None:
    """Фоновый цикл слежения за новыми бронями в Firestore (dev + prod)."""
    logger.info("🔔 Фоновый наблюдатель уведомлений Nova Gaming запущен (dev + prod)")
    seen: dict[str, str] = {}
    initialized: dict[str, bool] = {"dev": False, "prod": False}

    envs = [
        ("dev", "dev_bookings", "dev_clubs_status"),
        ("prod", "bookings", "clubs_status"),
    ]

    async with ClientSession() as session:
        while True:
            try:
                for env, bk_col, club_col in envs:
                    bk_list = await _fs_list(session, bk_col)
                    if bk_list is None:
                        continue

                    club_list = await _fs_list(session, club_col) or []
                    clubs_by_id: dict[int, dict[str, Any]] = {}
                    for cd in club_list:
                        if cd.get("id") is not None:
                            clubs_by_id[int(cd["id"])] = cd

                    # Первый проход после запуска бота: запоминаем старые брони, чтобы не спамить
                    if not initialized[env]:
                        for b in bk_list:
                            bid = str(b.get("id") or "")
                            if bid:
                                seen[f"{env}:{bid}"] = str(b.get("st") or "")
                        initialized[env] = True
                        logger.info("📦 Загружен снимок %s (%d броней)", bk_col, len(bk_list))
                        continue

                    for b in bk_list:
                        bid = str(b.get("id") or "")
                        if not bid:
                            continue
                        skey = f"{env}:{bid}"
                        cur_st = str(b.get("st") or "")
                        prev_st = seen.get(skey)

                        if prev_st is None:
                            seen[skey] = cur_st
                            if cur_st == "new" and b.get("by") == "user":
                                club_doc = clubs_by_id.get(int(b.get("club") or 1))
                                await _notify_admins_new_booking(bot, env, b, club_doc)
                        elif prev_st != cur_st:
                            seen[skey] = cur_st
                            if prev_st == "new" and cur_st in ("ok", "no"):
                                club_doc = clubs_by_id.get(int(b.get("club") or 1))
                                await _notify_player_status(bot, b, club_doc, cur_st)
            except asyncio.CancelledError:
                logger.info("🛑 Наблюдатель уведомлений остановлен.")
                raise
            except Exception as exc:
                logger.error("Ошибка в цикле уведомлений: %s", exc)

            await asyncio.sleep(3)
