"""Middleware для Telegram бота."""

import logging
from typing import Any, Callable

from aiogram import BaseMiddleware, types
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)


class WhitelistMiddleware(BaseMiddleware):
    """Middleware для перевірки доступу користувача.

    Перевіряє:
    - Статус доступу (approved/pending/banned)
    - Права адміністратора
    - Статус для команди /start (дозволена всім)
    """

    def __init__(self, db):
        """Ініціалізація middleware.

        Args:
            db: Об'єкт Database
        """
        self.db = db
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[types.Update, dict[str, Any]], Any],
        event: types.Update | Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        """Обробити подію до передачі handler'у."""
        try:
            user_id = event.from_user.id if hasattr(event, "from_user") else None
            if not user_id:
                return await handler(event, data)

            account, _ = await self.db.ensure_account(user_id)

            # Визначити команду для /start пропуску
            text_parts = (
                (event.text or "").split(maxsplit=1) if isinstance(event, Message) else []
            )
            command = text_parts[0] if text_parts else ""
            is_start = command == "/start" or command.startswith("/start@")

            # Перевірити доступ
            if self._has_access(account, is_start):
                return await handler(event, data)

            # Відправити повідомлення про заблокування/очікування
            text = self._get_access_denied_message(account)
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)

            logger.warning(f"Доступ заблоковано для користувача {user_id}, статус: {account['access_status']}")
            return None

        except Exception as e:
            logger.error(f"Помилка в WhitelistMiddleware: {e}")
            return await handler(event, data)

    @staticmethod
    def _has_access(account: dict, is_start: bool) -> bool:
        """Перевірити, чи користувач має доступ."""
        if account["access_status"] == "approved":
            return True
        if account["is_admin"] and account["access_status"] != "banned":
            return True
        if is_start:
            return True
        return False

    @staticmethod
    def _get_access_denied_message(account: dict) -> str:
        """Отримати повідомлення про блокування доступу."""
        if account["access_status"] == "banned":
            return "⛔ Ваш доступ заблоковано адміністратором."
        return "⏳ Ви не додані до білого списку. Очікуйте підтвердження адміністратора."
