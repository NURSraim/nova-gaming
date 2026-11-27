"""
Nova Gaming — Обработчики команд бота
"""
import html
from aiogram import Router, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)
from bot.config import WEBAPP_URL
from bot.notifier import (
    is_user_authorized_for_booking,
    notify_player_by_cached_id,
    update_booking_status,
)

router = Router()


@router.message(CommandStart())
async def cmd_start(message: types.Message):
    """
    Команда /start — приветствие и запуск Telegram Mini App
    """
    raw_name = (message.from_user.first_name if message.from_user else None) or "Игрок"
    name = html.escape(raw_name)

    text = (
        f"🎮 <b>Привет, {name}!</b>\n\n"
        f"Добро пожаловать в <b>Nova Gaming</b> — "
        f"сервис мгновенного бронирования ПК в компьютерных клубах!\n\n"
        f"🖥 Выбирай киберарену рядом с собой\n"
        f"🪑 Бронируй свободное место (Standart / VIP)\n"
        f"🍕 Заказывай напитки и снеки к столу\n\n"
        f"Нажми кнопку ниже, чтобы открыть приложение 👇"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🚀 Открыть Nova Gaming",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )],
            [InlineKeyboardButton(
                text="📖 Как это работает?",
                callback_data="how_it_works",
            )],
            [InlineKeyboardButton(
                text="💬 Поддержка",
                callback_data="support",
            )],
        ]
    )

    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📖 <b>Как пользоваться Nova Gaming:</b>\n\n"
        "1️⃣ Нажми кнопку <b>«🚀 Открыть Nova Gaming»</b>\n"
        "2️⃣ Выбери клуб в каталоге\n"
        "3️⃣ Ткни на свободный ПК на интерактивной карте зала\n"
        "4️⃣ Выбери время и тариф, оплати по номеру карты\n"
        "5️⃣ Приходи и садись за свой ПК — он ждёт тебя!\n"
    )
    await message.answer(text, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "how_it_works")
async def cb_how_it_works(callback: types.CallbackQuery):
    await callback.answer()
    if callback.message:
        await cmd_help(callback.message)


@router.message(Command("id"))
async def cmd_id(message: types.Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(
        f"🆔 Твой неизменяемый Telegram ID: <code>{uid}</code>",
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "support")
async def cb_support(callback: types.CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "💬 <b>Поддержка Nova Gaming</b>\n\n"
            "Если у тебя есть вопросы или предложения, напиши нам: @FGH_ccc\n"
            "Мы всегда рады помочь! 🤝",
            parse_mode="HTML",
        )


@router.callback_query(lambda c: c.data and (c.data.startswith("bk_ok:") or c.data.startswith("bk_no:")))
async def cb_booking_decision(callback: types.CallbackQuery):
    """Обрабатывает нажатие админом кнопок Одобрить / Отклонить прямо в Telegram."""
    data = callback.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Некорректные данные заявки", show_alert=True)
        return

    action, env, booking_id = parts
    uid = callback.from_user.id if callback.from_user else 0
    if not await is_user_authorized_for_booking(env, booking_id, uid):
        await callback.answer("⛔ Только администратор этого клуба может менять статус брони!", show_alert=True)
        return

    new_st = "ok" if action == "bk_ok" else "no"
    saved = await update_booking_status(env, booking_id, new_st)
    if not saved:
        await callback.answer("❌ Ошибка обновления статуса в базе", show_alert=True)
        return

    stamp = "\n\n✅ <b>СТАТУС: ОДОБРЕНО АДМИНИСТРАТОРОМ</b>" if new_st == "ok" else "\n\n❌ <b>СТАТУС: ОТКЛОНЕНО АДМИНИСТРАТОРОМ</b>"
    await callback.answer("✅ Бронь подтверждена!" if new_st == "ok" else "❌ Бронь отклонена")
    if callback.bot:
        await notify_player_by_cached_id(callback.bot, booking_id, new_st)

    if callback.message:
        try:
            if callback.message.photo:
                base_cap = callback.message.caption or ""
                await callback.message.edit_caption(
                    caption=base_cap + stamp,
                    reply_markup=None,
                    parse_mode="HTML",
                )
            else:
                base_txt = callback.message.text or ""
                await callback.message.edit_text(
                    text=base_txt + stamp,
                    reply_markup=None,
                    parse_mode="HTML",
                )
        except Exception:
            pass
