from __future__ import annotations

import logging
from pathlib import Path

from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery


logger = logging.getLogger(__name__)
LAST_GRAPH_CHAT_KEY = "_last_graph_chat_id"
LAST_GRAPH_MESSAGE_KEY = "_last_graph_message_id"


async def send_graph(
    callback: CallbackQuery,
    state: FSMContext,
    path: Path,
    filename: str,
    caption: str,
    *,
    as_photo: bool = False,
) -> None:
    """Send a chart and remove the user's previously sent chart, if any."""
    previous = await state.get_data()
    file = BufferedInputFile(path.read_bytes(), filename=filename)
    if as_photo:
        sent = await callback.message.answer_photo(file, caption=caption)
    else:
        sent = await callback.message.answer_document(file, caption=caption)

    await state.update_data(
        **{
            LAST_GRAPH_CHAT_KEY: sent.chat.id,
            LAST_GRAPH_MESSAGE_KEY: sent.message_id,
        }
    )

    old_chat_id = previous.get(LAST_GRAPH_CHAT_KEY)
    old_message_id = previous.get(LAST_GRAPH_MESSAGE_KEY)
    if old_chat_id and old_message_id:
        try:
            await callback.bot.delete_message(chat_id=old_chat_id, message_id=old_message_id)
        except Exception:
            # The new chart has already been delivered; cleanup should not fail the action.
            logger.info("Could not remove previous chart message %s", old_message_id, exc_info=True)
