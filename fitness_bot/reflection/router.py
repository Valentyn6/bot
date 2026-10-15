from __future__ import annotations

from datetime import date, timedelta
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..db import Database
from ..time_utils import local_today
from .states import ReflectionFlow

router = Router(name="reflection")
_db: Database | None = None
MAX_ANSWER_LENGTH = 600
MOOD_LABELS = {1: "😞", 2: "🙁", 3: "😐", 4: "🙂", 5: "😊"}
ENERGY_LABELS = {1: "🪫", 2: "🔋", 3: "🔋🔋", 4: "🔋🔋🔋", 5: "🔋🔋🔋🔋"}
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
DAILY_STEPS = (
    ("mood", "Як би ти оцінив/оцінила свій настрій сьогодні?", "rating"),
    ("energy", "Скільки в тебе сьогодні було енергії?", "rating"),
    ("good", "Що сьогодні було хорошого?", "text"),
    ("challenge", "Що було найскладнішим або забрало сили?", "text"),
    ("self_care", "Що ти сьогодні зробив/зробила для себе?", "text"),
    ("tomorrow", "Що хотілося б узяти із собою в завтрашній день?", "text"),
)
WEEKLY_STEPS = (
    ("energy_sources", "Що давало тобі сили цього тижня?", "text"),
    ("patterns", "Яку повторювану тему або закономірність ти помітив/помітила?", "text"),
    ("proud_of", "Чим ти задоволений/задоволена, навіть якщо це дрібниця?", "text"),
    ("next_week", "Що одне хочеш спробувати або зробити собі простішим наступного тижня?", "text"),
)


def configure(db: Database) -> None:
    global _db
    _db = db


def _database() -> Database:
    if _db is None:
        raise RuntimeError("Reflection router is not configured")
    return _db


def buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


def main_markup(has_today: bool) -> InlineKeyboardMarkup:
    write_label = "✏️ Змінити запис за сьогодні" if has_today else "🪞 Записати про сьогодні"
    return buttons([
        [(write_label, "reflection:write")],
        [("📊 Огляд і рефлексія за тиждень", "reflection:week")],
        [("📚 Попередні записи", "reflection:history")],
        [("← Головне меню", "home")],
    ])


def entry_text(entry) -> str:
    try:
        day_label = date.fromisoformat(entry["entry_date"]).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        day_label = escape(str(entry["entry_date"]))
    lines = [f"🪞 <b>Рефлексія · {day_label}</b>"]
    if entry["mood"] is not None:
        mood = int(entry["mood"])
        lines.extend(("", f"Настрій: {MOOD_LABELS.get(mood, '•')} {mood}/5"))
    if entry["energy"] is not None:
        energy = int(entry["energy"])
        lines.extend(("", f"Енергія: {ENERGY_LABELS.get(energy, '•')} {energy}/5"))
    sections = (
        ("🌤 Що було хорошого", entry["good"]),
        ("🌧 Що було складним", entry["challenge"]),
        ("💛 Що зробив/зробила для себе", entry["self_care"]),
        ("🌱 Що взяти в завтра", entry["tomorrow"]),
    )
    for title, answer in sections:
        if answer:
            lines.extend(("", f"<b>{title}</b>", escape(answer)))
    if len(lines) == 1:
        lines.extend(("", "У цьому записі поки немає відповідей."))
    return "\n".join(lines)


def _week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())


def weekly_report_text(entries, week_start: date, period_end: date, review=None) -> str:
    mood_values = [int(row["mood"]) for row in entries if row["mood"] is not None]
    energy_values = [int(row["energy"]) for row in entries if row["energy"] is not None]
    mood_average = f"{sum(mood_values) / len(mood_values):.1f}/5 за {len(mood_values)} дн." if mood_values else "поки немає оцінок"
    energy_average = f"{sum(energy_values) / len(energy_values):.1f}/5 за {len(energy_values)} дн." if energy_values else "поки немає оцінок"
    by_date = {row["entry_date"]: row for row in entries}
    lines = [
        f"📊 <b>Огляд тижня · {week_start.strftime('%d.%m')}–{period_end.strftime('%d.%m.%Y')}</b>",
        "",
        f"Записів: {len(entries)} із {(period_end - week_start).days + 1} днів",
        f"Середній настрій: {mood_average}",
        f"Середня енергія: {energy_average}",
        "",
        "Це лише твої позначки за тиждень — вони не пояснюють причин самі по собі.",
        "",
        "<b>Дні з записами</b>",
    ]
    if entries:
        for offset in range((period_end - week_start).days + 1):
            day = week_start + timedelta(days=offset)
            row = by_date.get(day.isoformat())
            if not row:
                continue
            mood = f"{MOOD_LABELS.get(int(row['mood']), '•')} {row['mood']}/5" if row["mood"] is not None else "настрій —"
            energy = f"енергія {row['energy']}/5" if row["energy"] is not None else "енергія —"
            lines.append(f"• {WEEKDAYS[day.weekday()]} {day.strftime('%d.%m')} · {mood} · {energy}")
    else:
        lines.append("Поки немає записів за цей тиждень.")
    if review:
        sections = (
            ("Що давало сили", review["energy_sources"]),
            ("Помічені закономірності", review["patterns"]),
            ("Чим пишаюся", review["proud_of"]),
            ("Що спробую наступного тижня", review["next_week"]),
        )
        existing = [(title, answer) for title, answer in sections if answer]
        if existing:
            lines.extend(("", "🪞 <b>Твоя рефлексія за тиждень</b>"))
            for title, answer in existing:
                lines.extend((f"\n<b>{title}</b>", escape(answer)))
    return "\n".join(lines)


def week_markup(has_review: bool, week_start: str) -> InlineKeyboardMarkup:
    label = "✏️ Змінити рефлексію тижня" if has_review else "🪞 Підбити підсумки тижня"
    rows = [[(label, "reflection:week-write")]]
    if has_review:
        rows.append([("🗑 Видалити рефлексію тижня", f"reflection:week-delete:{week_start}")])
    rows.append([("📚 Попередні тижні", "reflection:week-history")])
    rows.extend([[("← До рефлексії", "reflection:menu")], [("← Головне меню", "home")]])
    return buttons(rows)


async def _allowed(event: Message | CallbackQuery) -> bool:
    account, _ = await _database().ensure_account(event.from_user.id)
    if account["access_status"] == "approved" or (account["is_admin"] and account["access_status"] != "banned"):
        return True
    text = "⛔ Доступ заблоковано." if account["access_status"] == "banned" else "⏳ Дочекайтеся підтвердження адміністратора."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


async def _render_menu(message: Message, telegram_id: int) -> None:
    today = local_today().isoformat()
    entry = await _database().reflection_entry(telegram_id, today)
    text = "🪞 <b>Рефлексія</b>\n\nКілька хвилин, щоб помітити, як минув день. Оцінки настрою й енергії та відповіді на запитання необов’язкові. Записи не публікуються й не надсилаються іншим користувачам."
    if entry:
        text += "\n\n" + entry_text(entry)
    await message.edit_text(text, reply_markup=main_markup(bool(entry)), parse_mode="HTML")


async def _render_week(message: Message, telegram_id: int) -> None:
    today = local_today()
    start = _week_start(today)
    entries = await _database().reflection_entries_between(telegram_id, start.isoformat(), today.isoformat())
    review = await _database().weekly_reflection(telegram_id, start.isoformat())
    await message.edit_text(
        weekly_report_text(entries, start, today, review),
        reply_markup=week_markup(bool(review), start.isoformat()),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "reflection:menu")
async def reflection_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    await _render_menu(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "reflection:week")
async def reflection_week(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    await _render_week(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "reflection:week-history")
async def reflection_week_history(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    reviews = await _database().weekly_reflections(callback.from_user.id)
    if not reviews:
        await callback.message.edit_text(
            "📚 Збережених тижневих підсумків поки немає.",
            reply_markup=buttons([[("← До огляду тижня", "reflection:week")], [("← До рефлексії", "reflection:menu")]]),
        )
    else:
        rows = []
        for review in reviews:
            start = date.fromisoformat(review["week_start"])
            end = min(start + timedelta(days=6), local_today())
            rows.append([(f"📊 {start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}", f"reflection:week-view:{start.isoformat()}")])
        rows.extend([[("← До огляду тижня", "reflection:week")], [("← До рефлексії", "reflection:menu")]])
        await callback.message.edit_text("📚 <b>Попередні тижневі підсумки</b>\n\nОберіть тиждень:", reply_markup=buttons(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("reflection:week-view:"))
async def reflection_week_view(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    try:
        start = date.fromisoformat(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некоректна дата.", show_alert=True)
        return
    today = local_today()
    if start.weekday() != 0 or start > today:
        await callback.answer("Тиждень не знайдено.", show_alert=True)
        return
    period_end = min(start + timedelta(days=6), today)
    review = await _database().weekly_reflection(callback.from_user.id, start.isoformat())
    if not review:
        await callback.answer("Підсумок не знайдено.", show_alert=True)
        return
    entries = await _database().reflection_entries_between(callback.from_user.id, start.isoformat(), period_end.isoformat())
    await callback.message.edit_text(
        weekly_report_text(entries, start, period_end, review),
        reply_markup=buttons([
            [("🗑 Видалити рефлексію тижня", f"reflection:week-delete:{start.isoformat()}")],
            [("← До попередніх тижнів", "reflection:week-history")],
            [("← До огляду тижня", "reflection:week")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


def _steps(kind: str):
    return DAILY_STEPS if kind == "daily" else WEEKLY_STEPS


async def _prompt(message: Message, state: FSMContext, index: int, edit: bool = False) -> None:
    data = await state.get_data()
    steps = _steps(data["kind"])
    key, question, step_type = steps[index]
    if data["kind"] == "daily":
        day_label = date.fromisoformat(data["period_key"]).strftime("%d.%m.%Y")
        header = f"🪞 <b>Рефлексія · {day_label}</b>"
        warning = ""
        if index == 0:
            warning = "\nПісля завершення пропущені поля буде очищено. Якщо скасувати, попередній запис залишиться." if data["had_existing"] else "\nНезаповнені поля залишаться порожніми."
    else:
        start = date.fromisoformat(data["period_key"])
        header = f"🪞 <b>Підсумок тижня · від {start.strftime('%d.%m.%Y')}</b>"
        warning = ""
        if index == 0:
            warning = "\nПісля завершення пропущені поля буде очищено. Якщо скасувати, попередня рефлексія залишиться." if data["had_existing"] else "\nНезавершений запис не збережеться."
    text = f"{header}\nКрок {index + 1} із {len(steps)}\n\n{question}\n\nВідповідь необов’язкова — можна пропустити.{warning}"
    if step_type == "rating":
        labels = MOOD_LABELS if key == "mood" else ENERGY_LABELS
        scale = "1 — дуже низький, 5 — дуже високий." if key == "energy" else "1 — дуже поганий, 5 — дуже хороший."
        text += f"\n{scale}"
        keyboard_rows = [[(f"{rating} {labels[rating]}", f"reflection:rate:{index}:{rating}") for rating in range(1, 6)]]
    else:
        keyboard_rows = []
    keyboard_rows.extend([
        [("Пропустити", f"reflection:skip:{index}")],
        [("✖️ Скасувати", "reflection:cancel")],
    ])
    keyboard = buttons(keyboard_rows)
    if edit:
        await message.bot.edit_message_text(
            text, chat_id=data["chat_id"], message_id=data["prompt_message_id"],
            reply_markup=keyboard, parse_mode="HTML",
        )
    else:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


async def _start_flow(callback: CallbackQuery, state: FSMContext, kind: str) -> None:
    await state.clear()
    today = local_today()
    period_key = today.isoformat() if kind == "daily" else _week_start(today).isoformat()
    existing = (
        await _database().reflection_entry(callback.from_user.id, period_key)
        if kind == "daily"
        else await _database().weekly_reflection(callback.from_user.id, period_key)
    )
    await state.set_state(ReflectionFlow.answer)
    await state.update_data(
        kind=kind,
        period_key=period_key,
        had_existing=bool(existing),
        answers={},
        index=0,
        chat_id=callback.message.chat.id,
        prompt_message_id=callback.message.message_id,
    )
    await _prompt(callback.message, state, 0)
    await callback.answer()


async def _complete_flow(message: Message, state: FSMContext, telegram_id: int, data: dict) -> None:
    answers = data["answers"]
    kind = data["kind"]
    if not any(value is not None and value != "" for value in answers.values()):
        result = "Запис не змінено — усі запитання пропущені."
        markup = buttons([[("← До рефлексії", "reflection:menu")], [("← Головне меню", "home")]])
    elif kind == "daily":
        await _database().save_reflection(telegram_id, data["period_key"], answers)
        entry = await _database().reflection_entry(telegram_id, data["period_key"])
        result = "✅ <b>Запис збережено</b>\n\n" + entry_text(entry)
        markup = buttons([[("← До рефлексії", "reflection:menu")], [("← Головне меню", "home")]])
    else:
        await _database().save_weekly_reflection(telegram_id, data["period_key"], answers)
        today = local_today()
        start = date.fromisoformat(data["period_key"])
        entries = await _database().reflection_entries_between(telegram_id, start.isoformat(), today.isoformat())
        review = await _database().weekly_reflection(telegram_id, start.isoformat())
        result = "✅ <b>Тижневу рефлексію збережено</b>\n\n" + weekly_report_text(entries, start, today, review)
        markup = week_markup(bool(review), start.isoformat())
    await message.bot.edit_message_text(
        result, chat_id=data["chat_id"], message_id=data["prompt_message_id"],
        reply_markup=markup, parse_mode="HTML",
    )
    await state.clear()


async def _advance(callback: CallbackQuery, state: FSMContext, answer) -> None:
    data = await state.get_data()
    step_index = int(data["index"])
    step_key = _steps(data["kind"])[step_index][0]
    answers = dict(data["answers"])
    answers[step_key] = answer
    index = step_index + 1
    await state.update_data(answers=answers, index=index)
    if index < len(_steps(data["kind"])):
        await _prompt(callback.message, state, index, edit=True)
    else:
        await _complete_flow(callback.message, state, callback.from_user.id, {**data, "answers": answers})
    await callback.answer()


@router.callback_query(F.data == "reflection:write")
async def reflection_write(callback: CallbackQuery, state: FSMContext) -> None:
    if await _allowed(callback):
        await _start_flow(callback, state, "daily")


@router.callback_query(F.data == "reflection:week-write")
async def reflection_week_write(callback: CallbackQuery, state: FSMContext) -> None:
    if await _allowed(callback):
        await _start_flow(callback, state, "weekly")


@router.callback_query(F.data.startswith("reflection:rate:"))
async def reflection_rate(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    parts = callback.data.split(":")
    data = await state.get_data()
    try:
        index, rating = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Оцінку не вдалося зчитати.", show_alert=True)
        return
    steps = _steps(data.get("kind", ""))
    if index != data.get("index") or not 1 <= rating <= 5 or index >= len(steps) or steps[index][2] != "rating":
        await callback.answer("Цей крок уже завершено.")
        return
    await _advance(callback, state, rating)


@router.callback_query(F.data.startswith("reflection:skip:"))
async def reflection_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    data = await state.get_data()
    try:
        index = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Цей крок уже завершено.")
        return
    if index != data.get("index"):
        await callback.answer("Цей крок уже завершено.")
        return
    await _advance(callback, state, None if _steps(data["kind"])[index][2] == "rating" else "")


@router.callback_query(F.data == "reflection:cancel")
async def reflection_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _render_menu(callback.message, callback.from_user.id)
    await callback.answer("Запис скасовано")


@router.message(ReflectionFlow.answer)
async def reflection_answer(message: Message, state: FSMContext) -> None:
    if not await _allowed(message):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("Надішли текстову відповідь або натисни «Пропустити».")
        return
    if len(value) > MAX_ANSWER_LENGTH:
        await message.answer(f"Відповідь задовга. Скороти її до {MAX_ANSWER_LENGTH} символів.")
        return
    data = await state.get_data()
    index = int(data["index"])
    steps = _steps(data["kind"])
    if index >= len(steps) or steps[index][2] != "text":
        await message.answer("Спочатку обери оцінку кнопкою або пропусти цей крок.")
        return
    answers = dict(data["answers"])
    answers[steps[index][0]] = value
    index += 1
    await state.update_data(answers=answers, index=index)
    if index < len(steps):
        await _prompt(message, state, index, edit=True)
    else:
        await _complete_flow(message, state, message.from_user.id, {**data, "answers": answers})


@router.callback_query(F.data == "reflection:history")
async def reflection_history(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    entries = await _database().reflection_entries(callback.from_user.id)
    if not entries:
        await callback.message.edit_text(
            "📚 Записів поки немає. Коли захочеш, занотуй кілька думок про сьогодні.",
            reply_markup=buttons([[("← До рефлексії", "reflection:menu")], [("← Головне меню", "home")]]),
        )
    else:
        rows = []
        for entry in entries:
            day = date.fromisoformat(entry["entry_date"])
            answer = next((entry[field] for field in ("good", "challenge", "self_care", "tomorrow") if entry[field]), "")
            if answer:
                preview = " ".join(answer.split())[:25]
            else:
                preview = f"настрій {entry['mood'] or '—'} · енергія {entry['energy'] or '—'}"
            rows.append([(f"📅 {day.strftime('%d.%m.%Y')} · {preview}", f"reflection:view:{entry['entry_date']}")])
        rows.extend([[("← До рефлексії", "reflection:menu")], [("← Головне меню", "home")]])
        await callback.message.edit_text("📚 <b>Попередні записи</b>\n\nОберіть день для перегляду:", reply_markup=buttons(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("reflection:view:"))
async def reflection_view(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    value = callback.data.rsplit(":", 1)[1]
    try:
        day = date.fromisoformat(value).isoformat()
    except ValueError:
        await callback.answer("Некоректна дата.", show_alert=True)
        return
    entry = await _database().reflection_entry(callback.from_user.id, day)
    if not entry:
        await callback.answer("Запис не знайдено.", show_alert=True)
        return
    await callback.message.edit_text(
        entry_text(entry),
        reply_markup=buttons([
            [("🗑 Видалити запис", f"reflection:delete:{day}")],
            [("← До списку", "reflection:history")],
            [("← До рефлексії", "reflection:menu")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reflection:delete-confirm:"))
async def reflection_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    day = callback.data.rsplit(":", 1)[1]
    try:
        day = date.fromisoformat(day).isoformat()
    except ValueError:
        await callback.answer("Некоректна дата.", show_alert=True)
        return
    deleted = await _database().delete_reflection_entry(callback.from_user.id, day)
    await callback.message.edit_text(
        "🗑 Запис видалено." if deleted else "Запис не знайдено.",
        reply_markup=buttons([[("← До списку записів", "reflection:history")], [("← До рефлексії", "reflection:menu")]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reflection:delete:"))
async def reflection_delete_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    day = callback.data.rsplit(":", 1)[1]
    if not await _database().reflection_entry(callback.from_user.id, day):
        await callback.answer("Запис не знайдено.", show_alert=True)
        return
    await callback.message.edit_text(
        "Видалити цей запис без можливості відновлення?",
        reply_markup=buttons([
            [("Так, видалити", f"reflection:delete-confirm:{day}")],
            [("← Назад до запису", f"reflection:view:{day}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reflection:week-delete:"))
async def reflection_week_delete_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    try:
        start = date.fromisoformat(callback.data.rsplit(":", 1)[1]).isoformat()
    except ValueError:
        await callback.answer("Некоректна дата.", show_alert=True)
        return
    if not await _database().weekly_reflection(callback.from_user.id, start):
        await callback.answer("Рефлексію не знайдено.", show_alert=True)
        return
    await callback.message.edit_text(
        "Видалити рефлексію за цей тиждень без можливості відновлення?",
        reply_markup=buttons([
            [("Так, видалити", f"reflection:week-delete-confirm:{start}")],
            [("← Назад до огляду", "reflection:week")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reflection:week-delete-confirm:"))
async def reflection_week_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed(callback):
        return
    await state.clear()
    try:
        start = date.fromisoformat(callback.data.rsplit(":", 1)[1]).isoformat()
    except ValueError:
        await callback.answer("Некоректна дата.", show_alert=True)
        return
    deleted = await _database().delete_weekly_reflection(callback.from_user.id, start)
    await callback.message.edit_text(
        "🗑 Тижневу рефлексію видалено." if deleted else "Рефлексію не знайдено.",
        reply_markup=buttons([[("← До огляду тижня", "reflection:week")], [("← До рефлексії", "reflection:menu")]]),
    )
    await callback.answer()
