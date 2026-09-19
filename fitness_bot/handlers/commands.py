"""Основні команди та обробники для бота."""

import logging
from datetime import date

from aiogram import Bot, Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from .utils import account_access, account_profile, home_text, home_markup, nav
from calculations import calculate_targets
from constants import DEFAULT_PROFILE_WEIGHT, DEFAULT_ACTIVITY_KCAL

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, db, bot: Bot) -> None:
    """Команда /start для початку роботи з ботом."""
    await state.clear()

    try:
        account, created = await db.ensure_account(message.from_user.id)

        # Налаштувати адміна при першому запуску
        admin_id = await db.configure_admin(message.from_user.id)
        if admin_id:
            account = await db.account(message.from_user.id)

        # Перевірити статус доступу
        if account["access_status"] == "banned":
            await message.answer("⛔ Ваш доступ заблоковано адміністратором.")
            logger.warning(f"Заблокований користувач спробував запустити бот: {message.from_user.id}")
            return

        # Послати адміну повідомлення про нову заявку
        if not account["is_admin"] and account["access_status"] != "approved":
            if account["access_status"] == "rejected":
                await db.set_access(message.from_user.id, "pending")
                account = await db.account(message.from_user.id)

            if (created or account["access_status"] == "pending") and admin_id:
                try:
                    await bot.send_message(
                        admin_id,
                        f"🔔 Нова заявка на доступ\n"
                        f"Telegram ID: {message.from_user.id}\n"
                        f"Ім'я: {message.from_user.full_name}",
                    )
                except Exception as e:
                    logger.error(f"Не вдалося відправити повідомлення адміну: {e}")

        # Відправити привіт
        status_msg = "✅ Твій доступ схвалено!" if account["access_status"] == "approved" else f"⏳ Твоя заявка: {account['access_status']}"
        await message.answer(
            f"👋 Привіт, {message.from_user.first_name}!\n\n"
            f"Я допоможу тобі вести дневник фітнесу:\n"
            f"• 🍽 Записувати раціон\n"
            f"• ⚖️ Відслідковувати вагу\n"
            f"• 📊 Аналізувати активність\n"
            f"• 📈 Переглядати графіки\n\n"
            f"{status_msg}"
        )

        # Показати головне меню
        text = await home_text(db, message.from_user.id)
        markup = await home_markup(db, message.from_user.id)
        await message.answer(text, reply_markup=markup)

        logger.info(
            f"Користувач запустив бота: {message.from_user.id}, "
            f"новий={'так' if created else 'ні'}"
        )

    except Exception as e:
        logger.error(f"Помилка у /start: {e}")
        await message.answer(
            "❌ Виникла помилка при ініціалізації. Спробуйте пізніше."
        )


@router.message(F.text == "/help")
async def help_command(message: Message) -> None:
    """Команда /help для допомоги."""
    await message.answer(
        "ℹ️ **Допомога:**\n\n"
        "**Основне меню:**\n"
        "• 🍽 Раціон — додавай продукти та керуй раціоном\n"
        "• ⚖️ Вага — записуй та аналізуй вагу\n"
        "• 📈 Графіки — переглядай тренди в графіках\n"
        "• 🩺 Здоровʼя — дані про активність та сон\n"
        "• 👤 Профіль — налаштування профіля\n\n"
        "**Імпорт даних:**\n"
        "📥 Імпорт — прими JSON Health Export Kit\n\n"
        "**Адміністратор:**\n"
        "🛡 Адмін-панель — управління користувачами та базою продуктів",
        parse_mode="Markdown"
    )
