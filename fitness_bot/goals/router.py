from __future__ import annotations

from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from ..calculations import parse_number
from ..db import Database
from ..time_utils import local_today
from .service import GoalsService
from .states import GoalContributionFlow, GoalFlow, HabitFlow

router = Router(name="goals")
_db: Database | None = None
_reminder_wakeup = None
STREAK_ACHIEVEMENTS = (
    (100, "👑", "Легенда"),
    (30, "💎", "Платиновий місяць"),
    (21, "🥇", "Золота звичка"),
    (7, "🥈", "Срібний тиждень"),
    (3, "🥉", "Бронзовий старт"),
)


def configure(db: Database, reminder_wakeup=None) -> None:
    global _db, _reminder_wakeup
    _db = db
    _reminder_wakeup = reminder_wakeup


def service() -> GoalsService:
    if _db is None:
        raise RuntimeError("Goals router is not configured")
    return GoalsService(_db)


def buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return buttons([[('✖️ Скасувати', 'cancel')]])


def menu() -> InlineKeyboardMarkup:
    return buttons([
        [("🎯 Відкрити цілі та звички", "goals:menu")],
        [("← Головне меню", "home")],
    ])


def progress_bar(current: float, target: float, width: int = 10) -> str:
    percent = min(100, max(0, round(current / target * 100))) if target > 0 else 0
    filled = round(percent * width / 100)
    return f"[{'█' * filled}{'░' * (width - filled)}] {percent}%"


def compact_name(value: str, max_length: int = 25) -> str:
    value = " ".join(value.split())
    return value if len(value) <= max_length else value[: max_length - 3].rstrip() + "..."


def streak_achievement(streak: int) -> tuple[str, str] | None:
    for threshold, icon, title in STREAK_ACHIEVEMENTS:
        if streak >= threshold:
            return icon, title
    return None


async def dashboard_data(telegram_id: int) -> tuple[list, list, list]:
    habits = await _db.habits(telegram_id)  # type: ignore[union-attr]
    today = local_today()
    week_start = today.fromordinal(today.toordinal() - today.weekday())
    dashboard_habits = []
    for habit in habits:
        completed_today = await _db.habit_completed(telegram_id, habit["id"], today.isoformat())  # type: ignore[union-attr]
        streak_date = today if completed_today else today.fromordinal(today.toordinal() - 1)
        dashboard_habits.append({
            **dict(habit),
            "completed_today": completed_today,
            "streak": await _db.habit_streak(habit["id"], streak_date.isoformat()),  # type: ignore[union-attr]
            "week_status": await _db.habit_week_status(telegram_id, habit["id"], week_start.isoformat()),  # type: ignore[union-attr]
        })
    habits = dashboard_habits
    return (
        await _db.goals(telegram_id),  # type: ignore[union-attr]
        await _db.finance_jars(telegram_id),  # type: ignore[union-attr]
        habits,
    )


def dashboard_markup(goals: list, habits: list) -> InlineKeyboardMarkup:
    rows = [[("➕ Нова ціль", "goals:create"), ("➕ Нова звичка", "goals:habit:create")]]
    for goal in goals:
        action = "➕ +1" if goal["goal_type"] != "life" else "💰 Внесок"
        rows.append([(f"{action} · {goal['title']}", f"goals:contribute:{goal['id']}"), ("🗑", f"goals:delete:{goal['id']}")])
    for index, habit in enumerate(habits, start=1):
        status = "✅" if habit["completed_today"] else "⬜"
        rows.append([
            (f"{status} Звичка {index}", f"goals:habit:toggle:{habit['id']}"),
            (f"⏰ {habit['reminder_time']}" if habit["reminder_time"] else "⏰ Час", f"goals:habit:remind:{habit['id']}"),
            ("🗑", f"goals:habit:delete:{habit['id']}"),
        ])
    rows.append([("← Головне меню", "home")])
    return buttons(rows)


def dashboard_text(goals: list, jars: list, habits: list) -> str:
    lines = ["🎯 <b>Цілі та звички</b>"]
    if jars:
        lines.append("\n🐷 <b>Банки Monobank</b>")
        for jar in jars:
            balance = int(jar["balance_minor"]) / 100
            target = int(jar["goal_minor"]) / 100
            lines.append(f"• {escape(jar['title'])} {progress_bar(balance, target)} {balance:,.2f}/{target:,.2f} ₴")
    if goals:
        lines.append("\n🎯 <b>Ручні цілі</b>")
        for goal in goals:
            current = float(goal["calculated_current_value"])
            target = float(goal["target_value"])
            status = " ✅" if goal["calculated_status"] == "completed" else ""
            if goal["goal_type"] != "life":
                lines.append(f"• 📊 {escape(goal['title'])}{status}: {current:g} / {target:g} шт. — {progress_bar(current, target)}")
            else:
                lines.append(f"• 💰 {escape(goal['title'])}{status} {progress_bar(current, target)} {current:,.2f}/{target:,.2f} ₴")
            if goal.get("target_date"):
                lines.append(f"  Дедлайн: {goal['target_date']}")
    elif not jars:
        lines.append("\nЦілей ще немає.")
    if habits:
        lines.append("\n🔁 <b>Звички</b>")
        for habit in habits:
            status = "✅" if habit["completed_today"] else "⬜"
            week_status = "".join("✅" if done else "⬜" for done in habit["week_status"])
            achievement = streak_achievement(int(habit["streak"]))
            badge = f" {achievement[0]}" if achievement else ""
            lines.append(f"• {status} {escape(compact_name(habit['name']))} — 🔥 {habit['streak']} дн. поспіль{badge}\n  {week_status} <i>Пн–Нд</i>")
    else:
        lines.append("\nЗвичок ще немає.")
    return "\n".join(lines)


async def render_dashboard(message: Message, telegram_id: int) -> None:
    goals, jars, habits = await dashboard_data(telegram_id)
    await message.edit_text(dashboard_text(goals, jars, habits), reply_markup=dashboard_markup(goals, habits), parse_mode="HTML")


async def allowed(event: Message | CallbackQuery) -> bool:
    if _db is None:
        return False
    account, _ = await _db.ensure_account(event.from_user.id)
    if account["access_status"] == "approved" or (account["is_admin"] and account["access_status"] != "banned"):
        return True
    text = "⛔ Доступ заблоковано." if account["access_status"] == "banned" else "⏳ Дочекайтеся підтвердження адміністратора."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


@router.callback_query(F.data == "goals:menu")
async def goals_menu(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    await render_dashboard(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "goals:list")
async def goal_list(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    await render_dashboard(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "goals:create")
async def goal_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    await state.clear()
    await state.set_state(GoalFlow.title)
    await callback.message.edit_text("Введіть назву цілі (до 80 символів). Для скасування натисніть кнопку або /cancel.", reply_markup=cancel_keyboard())
    await callback.answer()


@router.message(GoalFlow.title)
async def goal_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 80:
        await message.answer("Назва має містити від 1 до 80 символів.", reply_markup=cancel_keyboard())
        return
    await state.update_data(title=title)
    await state.set_state(GoalFlow.goal_type)
    await message.answer("Оберіть тип цілі:", reply_markup=buttons([
        [("💰 Фінансова ціль", "goals:type:life")],
        [("📊 Кількісна ціль", "goals:type:count")],
        [("✖️ Скасувати", "cancel")],
    ]))


@router.callback_query(F.data.startswith("goals:type:"))
async def goal_type_selected(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    goal_type = callback.data.rsplit(":", 1)[1]
    if goal_type not in {"life", "count"}:
        await callback.answer("Невідомий тип цілі.", show_alert=True)
        return
    await state.update_data(goal_type=goal_type)
    await state.set_state(GoalFlow.target_value)
    prompt = "Введіть суму цілі у гривнях, наприклад 25000." if goal_type == "life" else "Введіть цільову кількість, наприклад 12 книг."
    await callback.message.edit_text(prompt, reply_markup=cancel_keyboard())
    await callback.answer()


@router.message(GoalFlow.target_value)
async def goal_value(message: Message, state: FSMContext) -> None:
    try:
        value = parse_number(message.text)
        if not 0 < value <= 1_000_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Введіть додатне число до 1 000 000 000.", reply_markup=cancel_keyboard())
        return
    await state.update_data(target_value=value)
    await state.set_state(GoalFlow.target_date)
    await message.answer("Введіть майбутній дедлайн у форматі YYYY-MM-DD або «-», якщо дедлайну немає.", reply_markup=cancel_keyboard())


@router.message(GoalFlow.target_date)
async def goal_date(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    deadline_text = (message.text or "").strip()
    target_date = None if deadline_text == "-" else deadline_text
    try:
        goal_id = await service().create_goal(message.from_user.id, data["title"], data["target_value"], target_date, data["goal_type"])
    except ValueError as exc:
        await message.answer(f"{escape(str(exc))} Перевірте дату й спробуйте ще раз.", reply_markup=cancel_keyboard(), parse_mode="HTML")
        return
    await state.clear()
    await message.answer(f"✅ Ціль створено (№{goal_id}).", reply_markup=menu())


@router.callback_query(F.data == "goals:habits")
async def habit_list(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    await render_dashboard(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "goals:habit:create")
async def habit_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    await state.clear()
    await state.set_state(HabitFlow.name)
    await callback.message.edit_text("Введіть назву щоденної звички (до 60 символів).", reply_markup=cancel_keyboard())
    await callback.answer()


@router.message(HabitFlow.name)
async def habit_name(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip() or len(message.text.strip()) > 60:
        await message.answer("Назва звички має містити від 1 до 60 символів.", reply_markup=cancel_keyboard())
        return
    habit_id = await service().create_habit(message.from_user.id, message.text.strip())
    await state.clear()
    await message.answer(f"✅ Звичку створено (№{habit_id}).", reply_markup=menu())


@router.callback_query(F.data.startswith("goals:habit:remind:"))
async def habit_reminder_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    habit_id = int(callback.data.rsplit(":", 1)[1])
    habit = next((h for h in await _db.habits(callback.from_user.id) if h["id"] == habit_id), None)  # type: ignore[union-attr]
    if not habit:
        await callback.answer("Звичку не знайдено.", show_alert=True)
        return
    await state.clear()
    if habit["reminder_time"]:
        choices = [[("🔕 Вимкнути нагадування", f"goals:habit:remind-off:{habit_id}")]]
    else:
        choices = []
    choices.append([("✖️ Скасувати", "goals:menu")])
    await callback.message.edit_text(
        f"⏰ <b>Нагадування про звичку</b>\n\n«{escape(habit['name'])}»\n"
        f"Поточний час: {habit['reminder_time'] or 'вимкнено'}\n"
        "Введіть новий час за Києвом у форматі <code>ГГ:ХХ</code>.",
        reply_markup=buttons(choices), parse_mode="HTML",
    )
    await state.update_data(reminder_habit_id=habit_id)
    await state.set_state(HabitFlow.reminder_time)
    await callback.answer()


@router.message(HabitFlow.reminder_time)
async def habit_reminder_time(message: Message, state: FSMContext) -> None:
    if not await allowed(message):
        await state.clear()
        return
    try:
        value = __import__("datetime").datetime.strptime((message.text or "").strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        await message.answer("Введіть час у форматі ГГ:ХХ, наприклад 20:00.", reply_markup=cancel_keyboard())
        return
    data = await state.get_data()
    updated = await _db.set_habit_reminder(message.from_user.id, int(data["reminder_habit_id"]), value)  # type: ignore[union-attr]
    await state.clear()
    if _reminder_wakeup is not None:
        _reminder_wakeup.set()
    if updated:
        await message.answer(f"✅ Нагадування про звичку надходитиме щодня о {value} за Києвом.", reply_markup=menu())
    else:
        await message.answer("Звичку не знайдено. Оновіть список звичок.", reply_markup=menu())


@router.callback_query(F.data.startswith("goals:habit:remind-off:"))
async def habit_reminder_off(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    habit_id = int(callback.data.rsplit(":", 1)[1])
    updated = await _db.set_habit_reminder(callback.from_user.id, habit_id, None)  # type: ignore[union-attr]
    if _reminder_wakeup is not None:
        _reminder_wakeup.set()
    await render_dashboard(callback.message, callback.from_user.id)
    await callback.answer("Нагадування вимкнено." if updated else "Звичку не знайдено.")


@router.callback_query(F.data.startswith("goals:habit:toggle:"))
async def habit_toggle(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    habit_id = int(callback.data.rsplit(":", 1)[1])
    today = local_today().isoformat()
    was_completed = await _db.habit_completed(callback.from_user.id, habit_id, today)  # type: ignore[union-attr]
    previous_streak_date = today if was_completed else date.fromordinal(date.fromisoformat(today).toordinal() - 1).isoformat()
    previous_streak = await _db.habit_streak(habit_id, previous_streak_date)  # type: ignore[union-attr]
    completed, streak = await _db.toggle_habit(callback.from_user.id, habit_id, today)  # type: ignore[union-attr]
    await render_dashboard(callback.message, callback.from_user.id)
    achievement = streak_achievement(streak)
    previous_achievement = streak_achievement(previous_streak)
    if completed and achievement and achievement != previous_achievement:
        await callback.answer(f"🎉 Нове досягнення: {achievement[0]} {achievement[1]}! Стрік: {streak} дн.", show_alert=True)
    else:
        await callback.answer(("✅ Виконано" if completed else "↩️ Відмітку знято") + f". Серія: {streak} дн.")


@router.callback_query(F.data.startswith("goals:habit:delete:"))
async def habit_delete_confirm(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    habit_id = int(callback.data.rsplit(":", 1)[1])
    habit = next((row for row in await _db.habits(callback.from_user.id) if row["id"] == habit_id), None)  # type: ignore[union-attr]
    if not habit:
        await callback.answer("Звичку не знайдено.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Видалити звичку «{escape(habit['name'])}»?\n\nІсторію її виконання також буде видалено.",
        reply_markup=buttons([
            [("✅ Так, видалити", f"goals:habit:delete-confirm:{habit_id}"), ("← Скасувати", "goals:menu")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("goals:habit:delete-confirm:"))
async def habit_delete(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    habit_id = int(callback.data.rsplit(":", 1)[1])
    deleted = await _db.delete_habit(callback.from_user.id, habit_id)  # type: ignore[union-attr]
    if not deleted:
        await callback.answer("Звичку не знайдено.", show_alert=True)
        return
    await render_dashboard(callback.message, callback.from_user.id)
    await callback.answer("🗑 Звичку видалено")


@router.callback_query(F.data.startswith("goals:contribute:"))
async def goal_contribute_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    goal_id = int(callback.data.rsplit(":", 1)[1])
    goal = next((row for row in await _db.goals(callback.from_user.id) if row["id"] == goal_id), None)  # type: ignore[union-attr]
    if not goal:
        await callback.answer("Ціль не знайдено.", show_alert=True)
        return
    if goal["goal_type"] != "life":
        await _db.add_goal_contribution(callback.from_user.id, goal_id, 1)  # type: ignore[union-attr]
        await render_dashboard(callback.message, callback.from_user.id)
        await callback.answer("✅ Додано 1")
        return
    await state.clear()
    await state.update_data(goal_id=goal_id, dashboard_message_id=callback.message.message_id)
    await state.set_state(GoalContributionFlow.amount)
    await callback.message.edit_text(f"Введіть суму внеску для цілі «{escape(goal['title'])}».", reply_markup=cancel_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.message(GoalContributionFlow.amount)
async def goal_contribution_received(message: Message, state: FSMContext) -> None:
    try:
        amount = parse_number(message.text)
        if not 0 < amount <= 1_000_000_000:
            raise ValueError
        data = await state.get_data()
        updated = await _db.add_goal_contribution(message.from_user.id, int(data["goal_id"]), amount)  # type: ignore[union-attr]
        if not updated:
            raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введіть додатне число до 1 000 000 000, наприклад 500.", reply_markup=cancel_keyboard())
        return
    data = await state.get_data()
    await state.clear()
    dashboard_message = await message.bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=int(data["dashboard_message_id"]),
        text="🎯 <b>Цілі та звички</b>",
        reply_markup=buttons([[('🎯 Оновити dashboard', 'goals:menu')]]),
        parse_mode="HTML",
    )
    await render_dashboard(dashboard_message, message.from_user.id)


@router.callback_query(F.data.startswith("goals:delete:"))
async def goal_delete_confirm(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    goal_id = int(callback.data.rsplit(":", 1)[1])
    goal = next((row for row in await _db.goals(callback.from_user.id) if row["id"] == goal_id), None)  # type: ignore[union-attr]
    if not goal:
        await callback.answer("Ціль не знайдено.", show_alert=True)
        return
    await callback.message.edit_text(f"Видалити ціль «{escape(goal['title'])}»?", reply_markup=buttons([[('✅ Так, видалити', f'goals:delete-confirm:{goal_id}'), ('← Скасувати', 'goals:menu')]]), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("goals:delete-confirm:"))
async def goal_delete(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    goal_id = int(callback.data.rsplit(":", 1)[1])
    deleted = await _db.delete_goal(callback.from_user.id, goal_id)  # type: ignore[union-attr]
    await render_dashboard(callback.message, callback.from_user.id)
    await callback.answer("🗑 Ціль видалено" if deleted else "Ціль не знайдено")


@router.callback_query(F.data.startswith("goals:habit:done:"))
async def habit_done(callback: CallbackQuery) -> None:
    await habit_toggle(callback)
