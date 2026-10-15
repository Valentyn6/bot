from __future__ import annotations

import io
import json
import logging
import sqlite3
import asyncio
import csv
import zipfile
import calendar
from contextlib import suppress
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Document, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .calculations import calculate_targets, food_totals, is_profile_training_day, is_training_day, normalize_date, parse_number, weekly_activity, weekly_activity_by_day_type, weight_stats
from .charts import activity_chart, weight_chart
from .config import Settings
from .db import Database
from .import_health import ImportErrorMessage, parse_food_database, parse_health_export
from .logger_config import setup_logging
from .finance import router as finance_router
from .finance.router import configure as configure_finance, money
from .goals import router as goals_router
from .goals.router import configure as configure_goals
from .reflection import router as reflection_router
from .reflection.router import configure as configure_reflection
from .time_utils import APP_TIMEZONE, local_today


router = Router()
db: Database
admin_id = 0
reminder_wakeup: asyncio.Event | None = None
DIET_TRAINING = "training"
DIET_REST = "rest"
WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")


class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        user_id = user.id
        text_parts = (event.text or "").split(maxsplit=1) if isinstance(event, Message) else []
        command = text_parts[0] if text_parts else ""
        is_start = command == "/start" or command.startswith("/start@")
        try:
            account, _ = await db.ensure_account(user_id)
            identity = (user.username, user.first_name, user.last_name)
            saved_identity = (account["username"], account["first_name"], account["last_name"])
            if identity != saved_identity:
                await db.update_telegram_identity(user_id, *identity)
        except Exception:
            logging.exception("Access check failed for Telegram user %s", user_id)
            if isinstance(event, CallbackQuery):
                await event.answer("Тимчасова помилка перевірки доступу. Спробуйте ще раз.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Тимчасова помилка перевірки доступу. Спробуйте ще раз.")
            return None
        if account["access_status"] == "approved" or (account["is_admin"] and account["access_status"] != "banned") or is_start:
            return await handler(event, data)
        text = "⛔ Ваш доступ заблоковано адміністратором." if account["access_status"] == "banned" else "⏳ Ви не додані до білого списку. Очікуйте підтвердження адміністратора."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return None


class InputFlow(StatesGroup):
    profile_value = State()
    food_meal = State()
    food_product = State()
    food_quantity = State()
    food_edit_quantity = State()
    weight = State()
    food_name = State()
    food_macros = State()
    food_unit = State()
    import_workout = State()
    manual_activity = State()
    daily_summary_time = State()


def buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*(InlineKeyboardButton(text=text, callback_data=data) for text, data in row))
    return builder.as_markup()


def nav() -> InlineKeyboardMarkup:
    return buttons([[('← До спорту і раціону', 'sport:menu')]])


def more_nav() -> InlineKeyboardMarkup:
    return buttons([[('← До спорту і раціону', 'sport:menu')]])


async def account_profile(user_id: int):
    account, _ = await db.ensure_account(user_id)
    profile = await db.active_profile(user_id)
    if not profile:
        await db.create_profile(user_id)
        profile = await db.active_profile(user_id)
    return profile


async def account_access(user_id: int):
    account, _ = await db.ensure_account(user_id)
    return account


async def is_admin(user_id: int) -> bool:
    account = await account_access(user_id)
    return bool(account["is_admin"])


async def may_manage_diet(user_id: int) -> bool:
    account = await account_access(user_id)
    return bool(account["is_admin"] or account["can_manage_diet"])


async def admin_target_profile(user_id: int, profiles):
    account = await db.account(user_id)
    return next((profile for profile in profiles if profile["id"] == account["active_profile_id"]), profiles[0])


async def access_denied(message: Message | CallbackQuery) -> bool:
    user_id = message.from_user.id
    account = await account_access(user_id)
    if account["access_status"] == "banned":
        text = "⛔ Ваш доступ заблоковано адміністратором."
    elif account["access_status"] != "approved":
        text = "⏳ Заявка ще очікує розгляду адміністратором."
    else:
        return False
    if isinstance(message, CallbackQuery):
        await message.answer(text, show_alert=True)
    else:
        await message.answer(text)
    return True


def admin_markup(accounts) -> InlineKeyboardMarkup:
    rows = []
    for account in accounts:
        status = {"approved": "✅", "pending": "⏳", "rejected": "❌", "banned": "⛔"}.get(account["access_status"], "❔")
        username = account["username"]
        display_name = " ".join(part for part in (account["first_name"], account["last_name"]) if part)
        identity = f"@{username}" if username else (display_name or "без ніку")
        identity = identity[:38]
        rows.append([(f"{status} {account['telegram_id']} · {identity}", f"adm:user:{account['telegram_id']}")])
    rows += [[("← Головне меню", "home")]]
    return buttons(rows)


async def source_values(profile, diet_type: str | None = None, as_of_date: date | None = None):
    if profile["manual_mode"]:
        return profile["manual_weight"] or 71, profile["manual_activity"] or 0, ""
    weights = await db.weights(profile["id"])
    end_date = as_of_date or local_today()
    start_date = end_date - timedelta(days=6)
    activities = await db.activities_between(profile["id"], start_date.isoformat(), end_date.isoformat())
    weight = weights[-1]["weight"] if weights else 71
    if profile["diet_mode"] == "single":
        activity = weekly_activity(activities)
        warning = ""
        if not weights and not activities:
            warning = "⚠️ Немає ні ваги, ні активності — використано дефолти: 71 кг, 0 ккал."
        elif not weights:
            warning = f"⚠️ Немає даних ваги — використано дефолт 71 кг. Активність: {round(activity)} ккал."
        elif not activities:
            warning = f"⚠️ Немає даних активності — використано 0 ккал. Вага: {weight} кг."
        return weight, activity, warning
    today = end_date.isoformat()
    today_activity = next((row for row in activities if row["log_date"] == today), None)
    training = is_profile_training_day(profile, today_activity, end_date)
    selected_type = diet_type or ("training" if training else "rest")
    configured_schedule = profile["training_weekdays"]
    if profile["diet_mode"] == "split" and configured_schedule is not None:
        try:
            scheduled_weekdays = {int(value) for value in json.loads(configured_schedule)}
            matching_days = [
                row for row in activities
                if (date.fromisoformat(row["log_date"]).weekday() in scheduled_weekdays)
                == (selected_type == DIET_TRAINING)
            ]
        except (TypeError, ValueError):
            matching_days = [row for row in activities if is_training_day(row) == (selected_type == DIET_TRAINING)]
    else:
        matching_days = [row for row in activities if is_training_day(row) == (selected_type == DIET_TRAINING)]
    activity = weekly_activity(matching_days) if matching_days else weekly_activity(activities)
    warning = ""
    if not weights and not activities:
        warning = "⚠️ Немає ні ваги, ні активності — використано дефолти: 71 кг, 0 ккал."
    elif not weights:
        warning = f"⚠️ Немає даних ваги — використано дефолт 71 кг. Активність: {round(activity)} ккал."
    elif not activities:
        warning = f"⚠️ Немає даних активності — використано 0 ккал. Вага: {weight} кг."
    return weight, activity, warning


async def current_day_is_training(profile, as_of_date: date | None = None) -> bool:
    day = as_of_date or local_today()
    today = await db.activity_on_date(profile["id"], day.isoformat())
    return is_profile_training_day(profile, today, day)


def total_activity(row) -> float:
    return row["active_calories"]


async def home_text(user_id: int) -> str:
    profile = await account_profile(user_id)
    weight, activity, warning = await source_values(profile)
    targets = calculate_targets(profile, weight, activity)
    mode = "🖐 ручні дані" if profile["manual_mode"] else "📡 автоматичні дані"
    day_type = "тренувальний" if await current_day_is_training(profile) else "звичайний"
    return (
        f"🏋️ <b>Фітнес-трекер</b> · {profile['name']}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎯 <b>Ціль на сьогодні ({day_type} день)</b>\n"
        f"🔥 {targets['total']} ккал\n"
        f"🥩 Білки {targets['protein']} г  ·  🥑 Жири {targets['fat']} г  ·  🍚 Вуглеводи {targets['carbs']} г\n\n"
        f"⚖️ Вага <b>{weight:g} кг</b>  ·  ІМТ {targets['bmi']}\n"
        f"🎯 Режим: {profile['goal_mode']}  ·  {mode}\n"
        f"{warning}\n\n"
        "Оберіть розділ нижче, щоб продовжити."
    )


def main_text() -> str:
    return "🏠 <b>Головне меню</b>\n\nОберіть розділ:"


async def home_markup(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [('🥗 Спорт і Раціон', 'sport:menu')],
        [('💳 Фінанси', 'finance:menu')],
        [('🎯 Цілі та звички', 'goals:menu')],
        [('🪞 Рефлексія', 'reflection:menu')],
    ]
    if await is_admin(user_id):
        rows.append([('🛡 Адмін-панель', 'admin')])
    rows.append([('⚙️ Налаштування', 'settings:menu')])
    return buttons(rows)


async def sport_markup(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [('🍽 Раціон', 'food'), ('⚖️ Вага', 'weight')],
        [('📊 Підсумки', 'reports:menu')],
        [('📈 Графіки', 'charts'), ('🩺 Здоровʼя', 'health')],
        [('👤 Профіль', 'profile'), ('⋯ Ще', 'more')],
        [('← Головне меню', 'home')],
    ]
    return buttons(rows)


def reports_markup() -> InlineKeyboardMarkup:
    return buttons([
        [('📅 Підсумок дня', 'reports:today')],
        [('🗓 Огляд за 7 днів', 'reports:week')],
        [('📆 Підсумок місяця', 'reports:month')],
        [('← До спорту і раціону', 'sport:menu')],
    ])


def settings_markup(daily_summary_time: str | None) -> InlineKeyboardMarkup:
    rows = [[('⏰ Змінити час підсумку дня', 'settings:report-time')]]
    if daily_summary_time:
        rows.insert(0, [(f'🔔 Підсумок щодня о {daily_summary_time}', 'settings:report-time')])
        rows.append([('🔕 Вимкнути нагадування', 'settings:report-off')])
    else:
        rows.insert(0, [('🔔 Увімкнути нагадування', 'settings:report-time')])
    rows.append([('← Головне меню', 'home')])
    return buttons(rows)


def report_food_rows(profile, food_rows, activities):
    """Keep only the diet bucket used on each day in reports."""
    activity_by_date = {row['log_date']: row for row in activities}
    selected_rows = []
    for row in food_rows:
        activity = activity_by_date.get(row['log_date'])
        day = date.fromisoformat(row['log_date'])
        diet_type = DIET_TRAINING if is_profile_training_day(profile, activity, day) else DIET_REST
        if row['diet_type'] == diet_type:
            selected_rows.append(row)
    return selected_rows


async def daily_report(user_id: int, report_day: date | None = None) -> str:
    today = report_day or local_today()
    today_key = today.isoformat()
    profile = await account_profile(user_id)
    food_rows = await db.food_period(profile['id'], today_key, today_key)
    activity = await db.activity_on_date(profile['id'], today_key)
    food_rows = report_food_rows(profile, food_rows, [activity] if activity else [])
    totals = food_totals(food_rows)
    training = is_profile_training_day(profile, activity, today)
    diet_type = DIET_TRAINING if profile['diet_mode'] == 'split' and training else DIET_REST
    weight, activity_avg, _ = await source_values(profile, diet_type, today)
    targets = calculate_targets(profile, weight, activity_avg)
    habits = await db.habits(user_id)
    completed_flags = [await db.habit_completed(user_id, habit['id'], today_key) for habit in habits]
    completed_habits = sum(completed_flags)
    finance = await db.finance_report_summary(user_id, today_key, today_key)

    lines = [f"📊 <b>Підсумок дня · {today.strftime('%d.%m.%Y')}</b>", ""]
    if food_rows:
        lines.extend([
            f"🥗 <b>Раціон:</b> {totals['calories']:g} / {targets['total']} ккал",
            f"Б {totals['protein']:g} г · Ж {totals['fat']:g} г · В {totals['carbs']:g} г",
        ])
    else:
        lines.append("🥗 <b>Раціон:</b> записів за сьогодні ще немає.")
    if activity:
        lines.append(
            f"🏃 <b>Активність:</b> {activity['steps']:,.0f} кроків · "
            f"{activity['active_calories']:,.0f} активних ккал"
        )
        if activity['workout_calories']:
            lines.append(f"Тренування: {activity['workout_calories']:,.0f} ккал")
    else:
        lines.append("🏃 <b>Активність:</b> даних за сьогодні ще немає.")
    if habits:
        lines.append(f"🔁 <b>Звички:</b> виконано {completed_habits} із {len(habits)}.")
    else:
        lines.append("🔁 <b>Звички:</b> ще не створені.")
    if finance:
        lines.append("\n💳 <b>Фінанси за сьогодні</b>")
        for row in finance:
            lines.append(
                f"Витрати {money(int(row['expenses']), int(row['currency_code']))} · "
                f"доходи {money(int(row['income']), int(row['currency_code']))}"
            )
    else:
        lines.append("\n💳 <b>Фінанси:</b> операцій за сьогодні немає.")
    return "\n".join(lines)


async def weekly_report(user_id: int, report_end: date | None = None) -> str:
    today = report_end or local_today()
    start = today - timedelta(days=6)
    start_key, end_key = start.isoformat(), today.isoformat()
    profile = await account_profile(user_id)
    activities = await db.activities_between(profile['id'], start_key, end_key)
    food_rows = await db.food_period(profile['id'], start_key, end_key)
    food_rows = report_food_rows(profile, food_rows, activities)
    totals = food_totals(food_rows)
    food_days = len({row['log_date'] for row in food_rows})
    activity_days = len(activities)
    total_steps = sum(float(row['steps']) for row in activities)
    total_active = sum(float(row['active_calories']) for row in activities)
    sleeps = await db.sleeps_between(profile['id'], start_key, end_key)
    sleep_rows = [row for row in sleeps if row['asleep_sec'] > 0]
    habits = await db.habit_period_summary(user_id, start_key, end_key)
    finance = await db.finance_report_summary(user_id, start_key, end_key)
    previous_finance_start = (start - timedelta(days=7)).isoformat()
    previous_finance_end = (start - timedelta(days=1)).isoformat()
    previous_finance = await db.finance_report_summary(user_id, previous_finance_start, previous_finance_end)

    lines = [f"🗓 <b>Огляд за 7 днів · {start.strftime('%d.%m')}–{today.strftime('%d.%m.%Y')}</b>", ""]
    if food_days:
        lines.extend([
            f"🥗 <b>Раціон:</b> у середньому {totals['calories'] / food_days:,.0f} ккал "
            f"за день із записами ({food_days} із 7 днів)",
            f"Середнє КБЖУ: Б {totals['protein'] / food_days:.0f} г · "
            f"Ж {totals['fat'] / food_days:.0f} г · В {totals['carbs'] / food_days:.0f} г",
        ])
    else:
        lines.append("🥗 <b>Раціон:</b> за цей період записів немає.")
    if activity_days:
        lines.append(
            f"🏃 <b>Активність:</b> {total_steps:,.0f} кроків · "
            f"{total_active:,.0f} активних ккал за {activity_days} дн. з даними"
        )
    else:
        lines.append("🏃 <b>Активність:</b> за цей період даних немає.")
    if sleep_rows:
        avg_sleep = sum(float(row['asleep_sec']) for row in sleep_rows) / len(sleep_rows) / 3600
        lines.append(f"😴 <b>Сон:</b> у середньому {avg_sleep:.1f} год за {len(sleep_rows)} ночей з даними")
    if habits['habits']:
        habit_rate = round(habits['completed'] / habits['opportunities'] * 100) if habits['opportunities'] else 0
        lines.append(
            f"🔁 <b>Звички:</b> виконано {habits['completed']} із "
            f"{habits['opportunities']} можливих відміток ({habit_rate}%)."
        )
    else:
        lines.append("🔁 <b>Звички:</b> ще не створені.")
    if finance:
        lines.append("\n💳 <b>Фінанси за 7 днів</b>")
        for row in finance:
            lines.append(
                f"Витрати {money(int(row['expenses']), int(row['currency_code']))} · "
                f"доходи {money(int(row['income']), int(row['currency_code']))}"
            )
    else:
        lines.append("\n💳 <b>Фінанси:</b> операцій за цей період немає.")
    insights = []
    if food_days:
        insights.append(f"Раціон внесено у {food_days} із 7 днів; середнє вище рахується лише за дні із записами.")
    if activity_days:
        avg_steps = total_steps / activity_days
        if avg_steps >= 8000:
            insights.append(f"У дні з даними ви проходили в середньому {avg_steps:,.0f} кроків.")
        elif activity_days >= 3:
            insights.append(f"Активність записана за {activity_days} дні; середнє — {avg_steps:,.0f} кроків. Коротка прогулянка може допомогти додати рух у спокійніші дні.")
    if habits['habits'] and habits['opportunities']:
        if habit_rate >= 80:
            insights.append("Звички виконувалися регулярно — спробуйте зберегти цей темп наступного тижня.")
        elif habit_rate < 50:
            insights.append("Виконання звичок нижче половини плану. Можна почати з однієї найважливішої звички.")
    previous_by_currency = {int(row['currency_code']): int(row['expenses']) for row in previous_finance}
    for row in finance:
        currency = int(row['currency_code'])
        old_spending = previous_by_currency.get(currency)
        current_spending = int(row['expenses'])
        if old_spending is not None and old_spending > 0:
            change_percent = round((current_spending - old_spending) / old_spending * 100)
            if abs(change_percent) >= 10:
                direction = "зросли" if change_percent > 0 else "зменшилися"
                insights.append(
                    f"Витрати {direction} на {abs(change_percent)}% проти попередніх 7 днів "
                    f"({money(current_spending, currency)} проти {money(old_spending, currency)})."
                )
    if insights:
        lines.extend(["\n💡 <b>Що помітно</b>", *[f"• {escape(item)}" for item in insights[:3]]])
    return "\n".join(lines)


async def monthly_report(user_id: int, report_end: date | None = None) -> str:
    """Summarize the current calendar month through report_end (today by default)."""
    today = report_end or local_today()
    start = today.replace(day=1)
    start_key, end_key = start.isoformat(), today.isoformat()

    previous_month_year = today.year if today.month > 1 else today.year - 1
    previous_month_number = today.month - 1 if today.month > 1 else 12
    previous_month_start = date(previous_month_year, previous_month_number, 1)
    previous_month_end_day = min(today.day, calendar.monthrange(previous_month_year, previous_month_number)[1])
    previous_month_end = date(previous_month_year, previous_month_number, previous_month_end_day)

    profile = await account_profile(user_id)
    activities = await db.activities_between(profile['id'], start_key, end_key)
    food_rows = await db.food_period(profile['id'], start_key, end_key)
    food_rows = report_food_rows(profile, food_rows, activities)
    totals = food_totals(food_rows)
    food_days = len({row['log_date'] for row in food_rows})
    activity_days = len(activities)
    total_steps = sum(float(row['steps']) for row in activities)
    total_active = sum(float(row['active_calories']) for row in activities)
    sleeps = await db.sleeps_between(profile['id'], start_key, end_key)
    sleep_rows = [row for row in sleeps if row['asleep_sec'] > 0]
    habits = await db.habit_period_summary(user_id, start_key, end_key)
    finance = await db.finance_report_summary(user_id, start_key, end_key)
    previous_finance = await db.finance_report_summary(
        user_id, previous_month_start.isoformat(), previous_month_end.isoformat()
    )

    lines = [
        f"📆 <b>Підсумок місяця · {start.strftime('%d.%m')}–{today.strftime('%d.%m.%Y')}</b>",
        "",
        "Поточний календарний місяць на цей день.",
    ]
    if food_days:
        lines.extend([
            f"\n🥗 <b>Раціон</b>",
            f"Записи за {food_days} із {today.day} днів · у середньому {totals['calories'] / food_days:,.0f} ккал за день із записами",
            f"Середнє КБЖУ: Б {totals['protein'] / food_days:.0f} г · Ж {totals['fat'] / food_days:.0f} г · В {totals['carbs'] / food_days:.0f} г",
        ])
    else:
        lines.append("\n🥗 <b>Раціон:</b> записів за цей місяць поки немає.")

    if activity_days:
        lines.append(
            f"\n🏃 <b>Активність</b>\n{total_steps:,.0f} кроків · {total_active:,.0f} активних ккал "
            f"за {activity_days} дн. з даними"
        )
    else:
        lines.append("\n🏃 <b>Активність:</b> даних за цей місяць поки немає.")

    if sleep_rows:
        avg_sleep = sum(float(row['asleep_sec']) for row in sleep_rows) / len(sleep_rows) / 3600
        lines.append(f"\n😴 <b>Сон:</b> у середньому {avg_sleep:.1f} год за {len(sleep_rows)} ночей з даними")

    if habits['habits']:
        habit_rate = round(habits['completed'] / habits['opportunities'] * 100) if habits['opportunities'] else 0
        lines.append(
            f"\n🔁 <b>Звички:</b> виконано {habits['completed']} із {habits['opportunities']} "
            f"можливих відміток ({habit_rate}%)."
        )
    else:
        lines.append("\n🔁 <b>Звички:</b> активних звичок поки немає.")

    if finance:
        lines.append("\n💳 <b>Фінанси</b>")
        previous_by_currency = {int(row['currency_code']): row for row in previous_finance}
        for row in finance:
            currency = int(row['currency_code'])
            expenses = int(row['expenses'])
            income = int(row['income'])
            lines.append(f"Витрати {money(expenses, currency)} · доходи {money(income, currency)}")
            previous = previous_by_currency.get(currency)
            if previous and int(previous['expenses']) > 0:
                old_expenses = int(previous['expenses'])
                change = round((expenses - old_expenses) / old_expenses * 100)
                direction = "більше" if change > 0 else "менше" if change < 0 else "так само"
                lines.append(
                    f"Порівняно з 1–{previous_month_end.day} {previous_month_start.strftime('%m')}: "
                    f"{abs(change)}% {direction} витрат ({money(old_expenses, currency)} раніше)."
                )
    else:
        lines.append("\n💳 <b>Фінанси:</b> операцій за цей місяць поки немає.")

    return "\n".join(lines)


async def more_markup(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [('📥 Імпорт', 'import'), ('🍎 Продукти', 'products')],
        [('📤 Експорт моїх даних', 'export')],
        [('ℹ️ Допомога', 'help')],
    ]
    if await is_admin(user_id):
        rows.insert(2, [('💾 Повна копія бази', 'backup')])
    rows.append([('← До спорту і раціону', 'sport:menu')])
    return buttons(rows)


def remaining_value(target: float, consumed: float) -> str:
    difference = round(target - consumed, 1)
    return f"{difference:g}" if difference >= 0 else f"перевищено на {abs(difference):g}"


def progress_line(label: str, target: float, consumed: float, unit: str) -> str:
    if target > 0:
        ratio = max(0.0, consumed / target)
        percent = round(ratio * 100)
        filled = min(10, round(ratio * 10))
        bar = "█" * filled + "░" * (10 - filled)
    else:
        percent = 0
        bar = "░" * 10
    remaining = remaining_value(target, consumed)
    return (
        f"{label}: [{bar}] {percent}%\n"
        f"   з’їдено {consumed:g} / {target:g} {unit} · залишилось {remaining} {unit}"
    )


def diet_title(diet_type: str) -> str:
    return "🏋️ Тренувальний раціон" if diet_type == DIET_TRAINING else "🛋 Звичайний раціон"


async def food_screen(user_id: int, diet_type: str = DIET_REST) -> tuple[str, InlineKeyboardMarkup]:
    profile = await account_profile(user_id)
    split_mode = profile["diet_mode"] == "split"
    diet_type = diet_type if split_mode else DIET_REST
    weight, activity, _ = await source_values(profile, diet_type)
    targets = calculate_targets(profile, weight, activity)
    day_type = "тренувальний" if await current_day_is_training(profile) else "звичайний"
    rows = await db.food_day(profile["id"], local_today().isoformat(), diet_type)
    totals = food_totals(rows)
    title = diet_title(diet_type) if split_mode else "🍽 Раціон"
    date_line = f"Раціон за {local_today().isoformat()} · {day_type} день" if split_mode else f"Раціон за {local_today().isoformat()}"
    text = (
        f"{title}\n{date_line}\n\n"
        f"🎯 Цільові показники\n\n"
        f"{progress_line('Калорії', targets['total'], totals['calories'], 'ккал')}\n\n"
        f"{progress_line('Білки', targets['protein'], totals['protein'], 'г')}\n\n"
        f"{progress_line('Жири', targets['fat'], totals['fat'], 'г')}\n\n"
        f"{progress_line('Вуглеводи', targets['carbs'], totals['carbs'], 'г')}"
    )
    diet_buttons: list[list[tuple[str, str]]] = []
    if rows:
        text += "\n\n📋 Додані продукти:"
        for row in rows:
            multiplier = row["quantity"] if row["unit"] == "шт" else row["quantity"] / 100
            calories = round(row["calories"] * multiplier, 1)
            protein = round(row["protein"] * multiplier, 1)
            fat = round(row["fat"] * multiplier, 1)
            carbs = round(row["carbs"] * multiplier, 1)
            text += f"\n• {row['meal']}: {row['name']} × {row['quantity']:g} {row['unit']} — {calories:g} ккал, Б {protein:g}, Ж {fat:g}, В {carbs:g}"
            if await may_manage_diet(user_id):
                diet_buttons.append([("✏️ " + row["name"], f"food_edit:{diet_type}:{row['id']}"), ("🗑", f"food_remove:{diet_type}:{row['id']}")])
    if await may_manage_diet(user_id):
        diet_buttons.insert(0, [('➕ Додати продукт', f'food_add:{diet_type}')])
        recent_meals = await db.recent_food_meals(profile['id'], diet_type)
        if recent_meals:
            diet_buttons.insert(1, [('⚡ Повторити прийом їжі', f'food_repeat_menu:{diet_type}')])
        diet_buttons.append([('💡 Порада на сьогодні', f'food_advice:{diet_type}')])
        diet_buttons.append([('↩️ Видалити останній', f'food_delete:{diet_type}'), ('🔁 Як учора', f'food_yesterday:{diet_type}')])
    else:
        text += "\n\nℹ️ Раціон веде коуч. Самостійне редагування вимкнено."
    diet_buttons.append([('🔁 Змінити раціон', 'food')])
    diet_buttons.append([('← До спорту і раціону', 'sport:menu')])
    return text, buttons(diet_buttons)


@router.callback_query(F.data.startswith("food_repeat_menu:"))
async def food_repeat_menu(callback: CallbackQuery) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    diet_type = callback.data.split(":", 1)[1]
    if diet_type not in {DIET_REST, DIET_TRAINING}:
        await callback.answer("Невідомий тип раціону.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    meals = await db.recent_food_meals(profile['id'], diet_type)
    if not meals:
        await callback.answer("Немає попередніх прийомів їжі для повторення.", show_alert=True)
        return
    rows = [[(
        f"{meal['meal']} · {meal['log_date']} · {meal['item_count']} продукт(и)",
        f"food_repeat:{meal['seed_id']}:{diet_type}",
    )] for meal in meals]
    rows.append([("← Назад до раціону", f"food:{diet_type}")])
    await callback.message.edit_text(
        "⚡ <b>Повторити прийом їжі</b>\n\nОберіть попередній прийом. Його продукти та кількості буде додано до сьогоднішнього раціону.",
        reply_markup=buttons(rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("food_repeat:"))
async def food_repeat(callback: CallbackQuery) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    try:
        _, seed_id, diet_type = callback.data.split(":")
        seed_id = int(seed_id)
    except (ValueError, TypeError):
        await callback.answer("Цей прийом їжі вже недоступний.", show_alert=True)
        return
    if diet_type not in {DIET_REST, DIET_TRAINING}:
        await callback.answer("Невідомий тип раціону.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    added = await db.repeat_food_meal(profile['id'], seed_id, local_today().isoformat())
    if not added:
        await callback.answer("Не вдалося повторити прийом. Оновіть екран раціону.", show_alert=True)
        return
    text, markup = await food_screen(callback.from_user.id, diet_type)
    await callback.message.edit_text(f"✅ Додано продуктів: {added}.\n\n{text}", reply_markup=markup)
    await callback.answer("Додано")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    account, created = await db.ensure_account(message.from_user.id)
    if account["access_status"] == "banned":
        await message.answer("⛔ Ваш доступ заблоковано адміністратором.")
        return
    if not account["is_admin"] and account["access_status"] != "approved":
        if account["access_status"] == "rejected":
            await db.set_access(message.from_user.id, "pending")
            account = await db.account(message.from_user.id)
        if (created or account["access_status"] == "pending") and admin_id:
            try:
                await message.bot.send_message(
                    admin_id,
                    f"🔔 Нова заявка на доступ\nTelegram ID: {message.from_user.id}\nІмʼя: {message.from_user.full_name}",
                    reply_markup=buttons([[('✅ Прийняти', f'adm:approve:{message.from_user.id}'), ('❌ Відхилити', f'adm:reject:{message.from_user.id}')]]),
                )
                logging.info("Access request sent to admin %s for user %s", admin_id, message.from_user.id)
            except Exception as exc:
                logging.error("Could not send access request to admin %s: %s", admin_id, exc)
                await message.answer("⏳ Ваш акаунт очікує підтвердження. Адміністратор ще не отримав сповіщення, перевірте ADMIN_TELEGRAM_ID.")
                return
        await message.answer("⏳ Заявку на доступ надіслано адміністратору. Очікуйте рішення.")
        return
    profile = await db.active_profile(message.from_user.id)
    if not profile:
        await db.create_profile(message.from_user.id)
        await message.answer("Вітаю! Створено профіль «Я» з дефолтними значеннями. Їх можна змінити в 👤 Профіль.")
    await message.answer(main_text(), reply_markup=await home_markup(message.from_user.id), parse_mode="HTML")


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer("/start — головне меню\n/help — довідка\n/export — експорт ваших даних\n/backup — повна копія бази (лише адміністратор)\n/cancel — скасувати поточну дію\n\nОсобистий експорт містить тільки ваші дані.")


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Немає активної дії для скасування.")
        return
    await state.clear()
    await message.answer("Дію скасовано. Оберіть розділ:", reply_markup=await home_markup(message.from_user.id))


@router.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(main_text(), reply_markup=await home_markup(callback.from_user.id), parse_mode="HTML")
    await callback.answer("Дію скасовано")


@router.message(Command("backup"))
async def backup_command(message: Message) -> None:
    if await access_denied(message):
        return
    if not await is_admin(message.from_user.id):
        await message.answer("Повна копія бази доступна лише адміністратору. Для особистих даних скористайтеся /export.")
        return
    await send_database_backup(message)


async def send_user_export(message: Message) -> None:
    tables = await db.export_all(message.from_user.id)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for table, rows in tables.items():
            if not rows:
                bundle.writestr(f"{table}.csv", "")
                continue
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})
            bundle.writestr(f"{table}.csv", output.getvalue().encode("utf-8-sig"))
    await message.answer_document(
        BufferedInputFile(archive.getvalue(), filename=f"my_bot_data_{local_today().isoformat()}.zip"),
        caption="📤 Ваші дані збережено в окремих CSV-файлах. Архів містить раціон, здоров’я, фінанси, цілі, звички та записи рефлексії.",
    )


async def send_database_backup(message: Message) -> None:
    payload = await db.sqlite_snapshot()
    stamp = datetime.now(APP_TIMEZONE).strftime("%Y%m%d_%H%M")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("fitness_bot.sqlite3", payload)
    await message.answer_document(
        BufferedInputFile(archive.getvalue(), filename=f"bot_database_{stamp}.zip"),
        caption="💾 Повна копія бази даних бота. Містить дані всіх користувачів; зберігайте файл у приватному місці.",
    )


@router.message(Command("export"))
async def export_command(message: Message) -> None:
    if await access_denied(message):
        return
    await send_user_export(message)


@router.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if await access_denied(callback):
        return
    await callback.message.edit_text(main_text(), reply_markup=await home_markup(callback.from_user.id), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "settings:menu")
async def settings_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if await access_denied(callback):
        return
    await state.clear()
    preferences = await db.user_preferences(callback.from_user.id)
    daily_time = preferences["daily_summary_time"] if preferences else None
    status = f"Увімкнене на {daily_time} (час Києва)." if daily_time else "Нагадування вимкнені."
    await callback.message.edit_text(
        "⚙️ <b>Налаштування</b>\n\n"
        f"📊 Щоденний підсумок: {status}\n"
        "Після ввімкнення бот надсилатиме його один раз на день. Нагадування можна вимкнути тут.",
        reply_markup=settings_markup(daily_time),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "settings:report-time")
async def daily_summary_time_start(callback: CallbackQuery, state: FSMContext) -> None:
    if await access_denied(callback):
        return
    await state.clear()
    await state.set_state(InputFlow.daily_summary_time)
    await callback.message.edit_text(
        "⏰ <b>Час щоденного підсумку</b>\n\nВведіть час у форматі <code>ГГ:ХХ</code> за київським часом, наприклад <code>20:00</code>.",
        reply_markup=buttons([[('✖️ Скасувати', 'settings:menu')]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(InputFlow.daily_summary_time)
async def daily_summary_time_received(message: Message, state: FSMContext) -> None:
    if await access_denied(message):
        await state.clear()
        return
    value = (message.text or "").strip()
    try:
        parsed_time = datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError:
        await message.answer("Введіть час у форматі ГГ:ХХ, наприклад 20:00.", reply_markup=buttons([[('✖️ Скасувати', 'settings:menu')]]))
        return
    await db.set_daily_summary_time(message.from_user.id, parsed_time)
    if reminder_wakeup is not None:
        reminder_wakeup.set()
    await state.clear()
    await message.answer(
        f"✅ Щоденний підсумок надходитиме о {parsed_time} за київським часом.",
        reply_markup=settings_markup(parsed_time),
    )


@router.callback_query(F.data == "settings:report-off")
async def daily_summary_time_disable(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await db.set_daily_summary_time(callback.from_user.id, None)
    if reminder_wakeup is not None:
        reminder_wakeup.set()
    await callback.message.edit_text(
        "🔕 Щоденне нагадування вимкнено.",
        reply_markup=settings_markup(None),
    )
    await callback.answer()


@router.callback_query(F.data == "sport:menu")
async def sport_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if await access_denied(callback):
        return
    await state.clear()
    await callback.message.edit_text(await home_text(callback.from_user.id), reply_markup=await sport_markup(callback.from_user.id), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "reports:menu")
async def reports_menu_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await callback.message.edit_text(
        "📊 <b>Підсумки</b>\n\nОберіть період. Звіт формується на запит із даних, уже збережених у боті.",
        reply_markup=reports_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "reports:today")
async def daily_report_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await callback.message.edit_text(
        await daily_report(callback.from_user.id),
        reply_markup=reports_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "reports:week")
async def weekly_report_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await callback.message.edit_text(
        await weekly_report(callback.from_user.id),
        reply_markup=reports_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "reports:month")
async def monthly_report_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await callback.message.edit_text(
        await monthly_report(callback.from_user.id),
        reply_markup=reports_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "more")
async def more_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await callback.message.edit_text("⋯ <b>Інструменти</b>\n\nІмпорт даних, база продуктів, резервна копія та допомога.", reply_markup=await more_markup(callback.from_user.id), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "ℹ️ <b>Як користуватися</b>\n\n"
        "🍽 <b>Раціон</b> — додавання продуктів і контроль БЖВ.\n"
        "⚖️ <b>Вага</b> — щоденні вимірювання та темп змін.\n"
        "🩺 <b>Здоровʼя</b> — активність і сон з Health Export Kit.\n"
        "📥 <b>Імпорт</b> — надішліть файл документом.\n"
        "📤 <b>Експорт</b> — архів ваших даних у CSV.\n"
        "💾 <b>Резервна копія</b> — повний знімок бази для адміністратора.\n\n"
        "Одиниці «г/мл» рахуються на 100 одиниць, «шт» — на одну штуку.",
        reply_markup=more_nav(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "backup")
async def backup_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    if not await is_admin(callback.from_user.id):
        await callback.answer("Повна копія доступна лише адміністратору.", show_alert=True)
        return
    await send_database_backup(callback.message)
    await callback.answer("Резервну копію надіслано")


@router.callback_query(F.data == "export")
async def export_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await send_user_export(callback.message)
    await callback.answer("Експорт надіслано")


@router.callback_query(F.data == "profiles")
async def profiles_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    profile = await account_profile(callback.from_user.id)
    await callback.message.edit_text(
        f"👤 Ваш профіль: {profile['name']}\n\nУ кожного користувача один профіль. Його параметри можна змінити у фітнес-розділі.",
        reply_markup=buttons([[('← До меню «Ще»', 'more')]]),
    )
    await callback.answer()


@router.callback_query(F.data == "admin")
async def admin_callback(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Цей розділ доступний лише адміністратору.", show_alert=True)
        return
    accounts = await db.accounts()
    markup = admin_markup(accounts)
    markup.inline_keyboard.insert(0, [InlineKeyboardButton(text="📥 Імпорт FoodDatabase.md", callback_data="adm:foods_import")])
    await callback.message.edit_text(
        "🛡 <b>Адмін-панель</b>\n\nКерування доступами, користувачами, їхніми фітнес-профілями та спільною базою продуктів. Оберіть користувача для дії:",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:user:"))
async def admin_user(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    user_id = int(callback.data.rsplit(":", 1)[1])
    account = await db.account(user_id)
    if not account:
        await callback.answer("Користувача не знайдено.", show_alert=True)
        return
    profiles = await db.profiles(user_id)
    if not profiles:
        await db.create_profile(user_id)
        profiles = await db.profiles(user_id)
    profile = await admin_target_profile(user_id, profiles)
    status = {"approved": "схвалений", "pending": "очікує", "rejected": "відхилений", "banned": "заблокований"}.get(account["access_status"], account["access_status"])
    diet_mode = "окремі раціони: тренувальний + звичайний" if profile["diet_mode"] == "split" else "один раціон"
    username = f"@{account['username']}" if account["username"] else "не задано"
    display_name = " ".join(part for part in (account["first_name"], account["last_name"]) if part) or "не вказане"
    text = (
        f"👤 <b>Користувач</b>\n"
        f'ID: <a href="tg://user?id={user_id}">{user_id}</a>\n'
        f"Нік: {escape(username)}\n"
        f"Ім’я: {escape(display_name)}\n\n"
        f"Статус: {escape(status)}\n"
        f"Профіль: {escape(profile['name'])}\n"
        f"Режим: <b>{escape(profile['goal_mode'])}</b>\n"
        f"Тип раціонів: <b>{escape(diet_mode)}</b>\n"
        f"Самостійний раціон: {'дозволено' if account['can_manage_diet'] else 'заборонено'}"
    )
    actions = []
    if account["access_status"] == "approved":
        actions.append([("🎯 Набір", f"adm:mode:{user_id}:набір"), ("⚖️ Підтримання", f"adm:mode:{user_id}:підтримання")])
        actions.append([("🔥 Схуднення", f"adm:mode:{user_id}:схуднення")])
        actions.append([("🍽 Один раціон", f"adm:dietmode:{user_id}:single"), ("🏋️ Два раціони", f"adm:dietmode:{user_id}:split")])
    if account["access_status"] in {"pending", "rejected"}:
        actions.append([("✅ Схвалити", f"adm:approve:{user_id}")])
    if account["access_status"] == "approved":
        actions.append([("🍽 Заборонити раціон" if account["can_manage_diet"] else "🍽 Дозволити раціон", f"adm:diet:{user_id}")])
        actions.append([("👤 Вести цього користувача", f"adm:select:{user_id}")])
        actions.append([("⏳ Відкликати доступ", f"adm:pending:{user_id}")])
        actions.append([("⛔ Заблокувати", f"adm:ban:{user_id}")])
    if account["access_status"] == "banned":
        actions.append([("♻️ Розблокувати", f"adm:unban:{user_id}")])
    actions.append([("← До списку", "admin")])
    await callback.message.edit_text(text, reply_markup=buttons(actions), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm:"))
async def admin_action(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    if callback.data.startswith("adm:dietmode:"):
        parts = callback.data.split(":")
        user_id = int(parts[2])
        mode = parts[3] if len(parts) > 3 else None
        if mode not in {"single", "split"}:
            await callback.message.edit_text("🍽 Оберіть режим раціонів:", reply_markup=buttons([
                [("🍽 Один раціон", f"adm:dietmode:{user_id}:single"), ("🏋️ Два раціони", f"adm:dietmode:{user_id}:split")],
                [("← До адмін-панелі", "admin")],
            ]))
            await callback.answer()
            return
        profiles = await db.profiles(user_id)
        if not profiles:
            await db.create_profile(user_id)
            profiles = await db.profiles(user_id)
        profile = await admin_target_profile(user_id, profiles)
        await db.update_profile(profile["id"], diet_mode=mode)
        await callback.message.edit_text("🛡 Режим раціонів оновлено.", reply_markup=buttons([
            [("← До користувача", f"adm:user:{user_id}")],
            [("← До адмін-панелі", "admin")],
        ]))
        await callback.answer("Режим раціонів оновлено")
        return
    if callback.data == "adm:own":
        await db.set_managed_profile(callback.from_user.id, None)
        await profile_callback(callback)
        return
    if callback.data == "adm:foods_import":
        await callback.message.answer("📥 Надішліть цей файл документом: FoodDatabase.md\n\nПеревіряються заголовок # FoodDatabase, структура таблиці, всі 7 колонок, одиниці г/мл/шт і числові значення. Випадкові файли будуть відхилені.")
        await callback.answer()
        return
    if callback.data.startswith("adm:mode:"):
        _, _, raw_user_id, mode = callback.data.split(":")
        user_id = int(raw_user_id)
        profiles = await db.profiles(user_id)
        if not profiles:
            await db.create_profile(user_id)
            profiles = await db.profiles(user_id)
        profile = await admin_target_profile(user_id, profiles)
        source = {"набір": "surplus_gain", "підтримання": "surplus_maintenance", "схуднення": "deficit_loss"}[mode]
        await db.update_profile(profile["id"], goal_mode=mode, surplus_deficit=profile[source])
        await callback.message.edit_text("🛡 Режим профілю оновлено.", reply_markup=buttons([[('← До користувача', f'adm:user:{user_id}')], [('← До адмін-панелі', 'admin')]]))
        await callback.answer("Режим оновлено")
        return
    _, action, raw_user_id = callback.data.split(":")
    user_id = int(raw_user_id)
    if action == "approve":
        await db.set_access(user_id, "approved")
        await callback.bot.send_message(user_id, "✅ Адміністратор схвалив вашу заявку. Тепер ви можете користуватися ботом.")
    elif action == "reject":
        await db.set_access(user_id, "rejected")
        await callback.bot.send_message(user_id, "❌ Адміністратор відхилив заявку. Ви можете повторити запит командою /start пізніше.")
    elif action == "ban":
        await db.set_access(user_id, "banned")
        await callback.bot.send_message(user_id, "⛔ Ваш доступ заблоковано адміністратором.")
    elif action == "unban":
        await db.set_access(user_id, "approved")
        await callback.bot.send_message(user_id, "♻️ Доступ відновлено адміністратором.")
    elif action == "pending":
        await db.set_access(user_id, "pending")
        await callback.bot.send_message(user_id, "⏳ Ваш доступ відкликано. Очікуйте повторного підтвердження адміністратором.")
    elif action == "diet":
        account = await db.account(user_id)
        await db.set_diet_permission(user_id, not bool(account["can_manage_diet"]))
    elif action == "select":
        profiles = await db.profiles(user_id)
        if not profiles:
            await db.create_profile(user_id)
            profiles = await db.profiles(user_id)
        profile = await admin_target_profile(user_id, profiles)
        await db.set_managed_profile(callback.from_user.id, profile["id"])
        await callback.message.edit_text(await home_text(callback.from_user.id), reply_markup=await sport_markup(callback.from_user.id), parse_mode="HTML")
        await callback.answer(f"Профіль «{profile['name']}» відкрито")
        return
    await callback.message.edit_text("🛡 Зміни збережено.", reply_markup=buttons([[('← До адмін-панелі', 'admin')]]))
    await callback.answer("Збережено")


@router.callback_query(F.data.startswith("switch:"))
async def switch_profile(callback: CallbackQuery) -> None:
    await callback.answer("У кожного користувача один профіль.", show_alert=True)


@router.callback_query(F.data == "profile_add")
async def profile_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("У кожного користувача один профіль. Змініть його параметри в розділі «Профіль».", show_alert=True)


def saved_training_weekdays(profile) -> list[int] | None:
    value = profile["training_weekdays"]
    if value is None:
        return None
    try:
        return sorted({int(day) for day in json.loads(value) if 0 <= int(day) <= 6})
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def training_days_keyboard(profile) -> InlineKeyboardMarkup:
    selected = saved_training_weekdays(profile)
    selected_set = set(selected or [])
    rows = [[(
        f"{'✅' if day in selected_set else '▫️'} {label}",
        f"dietdays:toggle:{day}",
    )] for day, label in enumerate(WEEKDAY_LABELS)]
    rows.extend([
        [("🤖 Автовизначення за активністю", "dietdays:auto")],
        [("✅ Готово", "dietdays:done")],
    ])
    return buttons(rows)


def training_days_text(profile) -> str:
    selected = saved_training_weekdays(profile)
    if selected is None:
        current = "Зараз: автовизначення за записом тренування."
    elif selected:
        current = "Зараз: " + ", ".join(WEEKDAY_LABELS[day] for day in selected) + "."
    else:
        current = "Зараз: днів тренування не вибрано; щодня діє звичайний раціон."
    return (
        "🗓 <b>Дні тренувань</b>\n\n"
        "Оберіть дні тижня, коли використовувати тренувальний раціон. "
        "В інші дні підсумок враховуватиме звичайний раціон.\n\n"
        f"{current}\n\nНатискайте на день, щоб увімкнути або вимкнути його."
    )


@router.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    manual = "🖐️ Ручний режим: УВІМКНЕНО" if profile["manual_mode"] else "📡 Авторежим (з експорту здоровʼя)"
    diet_mode = "один раціон" if profile["diet_mode"] == "single" else "тренувальний + звичайний"
    text = (
        f"👤 <b>Профіль · {profile['name']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📏 Зріст: {profile['height']:g} см  ·  Вік: {profile['age']}  ·  Стать: {profile['sex']}\n"
        f"🎯 Ціль: <b>{profile['goal_mode']}</b>  ·  корекція {profile['surplus_deficit']:g} ккал\n"
        f"⚖️ Цільова вага: {profile['target_weight'] or 'не задана'} кг\n"
        f"🥩 Білок: {profile['protein_per_kg']:g} г/кг  ·  🥑 Жири: {profile['fat_per_kg']:g} г/кг\n"
        f"🍽 Раціони: <b>{diet_mode}</b>\n"
        f"📊 Джерело даних: {'ручне' if profile['manual_mode'] else 'Health Export'}"
    )
    rows = [[('🎯 Набір', 'mode:набір'), ('⚖️ Підтримання', 'mode:підтримання'), ('🔥 Схуднення', 'mode:схуднення')], [('🔁 ' + manual, 'toggle_manual')], [('✏️ Зріст', 'edit:height'), ('✏️ Вік', 'edit:age'), ('✏️ Стать', 'edit:sex')], [('✏️ Цільова вага', 'edit:target_weight')], [('✏️ Ручна вага', 'edit:manual_weight'), ('✏️ Ручна активність', 'edit:manual_activity')], [('✏️ Білок/кг', 'edit:protein_per_kg'), ('✏️ Жири/кг', 'edit:fat_per_kg')], [('✏️ Профіцит/дефіцит', 'edit:surplus_deficit')]]
    if profile["diet_mode"] == "split":
        selected = saved_training_weekdays(profile)
        if selected is None:
            text += "\n🗓 Дні тренувань: автовизначення за активністю"
        else:
            days = ", ".join(WEEKDAY_LABELS[day] for day in selected) if selected else "не вибрано"
            text += f"\n🗓 Дні тренувань: {days}"
        rows.append([('🗓 Налаштувати дні тренувань', 'dietdays:menu')])
    rows.append([('← До спорту і раціону', 'sport:menu')])
    markup = buttons(rows)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "dietdays:menu")
async def training_days_menu(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] != "split":
        await callback.answer("Перемикніть профіль на два раціони, щоб налаштувати дні тренувань.", show_alert=True)
        return
    await callback.message.edit_text(training_days_text(profile), reply_markup=training_days_keyboard(profile), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("dietdays:toggle:"))
async def training_days_toggle(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] != "split":
        await callback.answer("Налаштування днів доступне лише для двох раціонів.", show_alert=True)
        return
    try:
        day = int(callback.data.rsplit(":", 1)[1])
        if not 0 <= day <= 6:
            raise ValueError
    except ValueError:
        await callback.answer("Невідомий день тижня.", show_alert=True)
        return
    selected = set(saved_training_weekdays(profile) or [])
    selected.symmetric_difference_update({day})
    await db.update_profile(profile["id"], training_weekdays=json.dumps(sorted(selected)))
    profile = await account_profile(callback.from_user.id)
    await callback.message.edit_text(training_days_text(profile), reply_markup=training_days_keyboard(profile), parse_mode="HTML")
    await callback.answer("Збережено")


@router.callback_query(F.data == "dietdays:auto")
async def training_days_auto(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] != "split":
        await callback.answer("Налаштування доступне лише для двох раціонів.", show_alert=True)
        return
    await db.update_profile(profile["id"], training_weekdays=None)
    profile = await account_profile(callback.from_user.id)
    await callback.message.edit_text(training_days_text(profile), reply_markup=training_days_keyboard(profile), parse_mode="HTML")
    await callback.answer("Автовизначення увімкнено")


@router.callback_query(F.data == "dietdays:done")
async def training_days_done(callback: CallbackQuery) -> None:
    await profile_callback(callback)


@router.callback_query(F.data.startswith("mode:"))
async def mode_callback(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    mode = callback.data.split(":", 1)[1]
    source = {"набір": "surplus_gain", "підтримання": "surplus_maintenance", "схуднення": "deficit_loss"}[mode]
    await db.update_profile(profile["id"], goal_mode=mode, surplus_deficit=profile[source])
    await callback.message.edit_text(await home_text(callback.from_user.id), reply_markup=await sport_markup(callback.from_user.id), parse_mode="HTML")
    await callback.answer("Режим оновлено")


@router.callback_query(F.data == "toggle_manual")
async def toggle_manual(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    await db.update_profile(profile["id"], manual_mode=0 if profile["manual_mode"] else 1)
    await profile_callback(callback)


@router.callback_query(F.data.startswith("edit:"))
async def edit_profile(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    prompts = {"height": "Введіть зріст (100–250 см):", "age": "Введіть вік (10–120):", "sex": "Введіть стать: чоловіча або жіноча.", "target_weight": "Введіть цільову вагу (20–300 кг):", "manual_weight": "Введіть ручну вагу (20–300 кг):", "manual_activity": "Введіть ручну активність у ккал (0 або більше):", "protein_per_kg": "Введіть білок у г/кг (0 або більше):", "fat_per_kg": "Введіть жири у г/кг (0 або більше):", "surplus_deficit": "Введіть робочий профіцит/дефіцит у ккал (0 або більше):"}
    await state.set_state(InputFlow.profile_value)
    await state.update_data(field=field)
    await callback.message.edit_text(prompts[field], reply_markup=nav())
    await callback.answer()


@router.message(InputFlow.profile_value)
async def profile_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    profile = await account_profile(message.from_user.id)
    value = message.text.strip()
    try:
        if data["field"] == "name":
            await state.clear()
            await message.answer("У кожного користувача один профіль. Його параметри можна змінити в розділі «Профіль».", reply_markup=await sport_markup(message.from_user.id))
            return
        elif data["field"] == "sex":
            sex = value.casefold()
            if sex not in {"чоловіча", "жіноча"}:
                raise ValueError
            await db.update_profile(profile["id"], sex=sex)
        else:
            parsed = parse_number(value)
            field = data["field"]
            limits = {"height": (100, 250), "age": (10, 120), "target_weight": (20, 300), "manual_weight": (20, 300), "manual_activity": (0, 100000), "protein_per_kg": (0, 10), "fat_per_kg": (0, 10), "surplus_deficit": (0, 10000)}
            if not limits[field][0] <= parsed <= limits[field][1]:
                raise ValueError
            await db.update_profile(profile["id"], **{field: int(parsed) if field == "age" else parsed})
        await state.clear()
        await message.answer(await home_text(message.from_user.id), reply_markup=await sport_markup(message.from_user.id), parse_mode="HTML")
    except (ValueError, KeyError):
        await message.answer("Некоректне значення. Перевірте число та діапазон.")


@router.callback_query(F.data == "food")
async def food_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] == "single":
        text, markup = await food_screen(callback.from_user.id, DIET_REST)
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()
        return
    await callback.message.edit_text(
        "🍽 <b>Оберіть раціон</b>",
        reply_markup=buttons([
            [('🏋️ Тренувальний раціон', f'food:{DIET_TRAINING}')],
            [('🛋 Звичайний раціон', f'food:{DIET_REST}')],
            [('← До спорту і раціону', 'sport:menu')],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("food:"))
async def selected_food_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    diet_type = callback.data.split(":", 1)[1]
    if diet_type not in {DIET_TRAINING, DIET_REST}:
        await callback.answer("Невідомий тип раціону", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] == "single":
        diet_type = DIET_REST
    text, markup = await food_screen(callback.from_user.id, diet_type)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


def advice_candidate(row, remaining: dict[str, float], familiar: bool):
    unit = row["unit"]
    if unit not in {"г", "мл", "шт"} or row["calories"] <= 0 or remaining["calories"] <= 0:
        return None

    multiplier = 1 if unit == "шт" else 0.01
    nutrients_per_unit = {
        "calories": row["calories"] * multiplier,
        "protein": row["protein"] * multiplier,
        "fat": row["fat"] * multiplier,
        "carbs": row["carbs"] * multiplier,
    }
    step, maximum = {
        "шт": (1, 4),
        "мл": (50, 500),
        "г": (10, 400),
    }[unit]
    calorie_budget = remaining["calories"] + max(40, remaining["calories"] * 0.12)
    desired_quantities = [remaining["calories"] / nutrients_per_unit["calories"]]
    for nutrient in ("protein", "carbs", "fat"):
        if remaining[nutrient] > 0 and nutrients_per_unit[nutrient] > 0:
            desired_quantities.append(remaining[nutrient] / nutrients_per_unit[nutrient])

    quantities = set()
    for desired in desired_quantities:
        for factor in (0.75, 1.0, 1.25):
            quantity = max(step, min(maximum, round(desired * factor / step) * step))
            quantities.add(quantity)

    best = None
    for quantity in quantities:
        added = {key: round(value * quantity, 1) for key, value in nutrients_per_unit.items()}
        if added["calories"] <= 0 or added["calories"] > calorie_budget:
            continue
        coverage = {
            nutrient: min(1.0, added[nutrient] / max(remaining[nutrient], 1))
            for nutrient in ("protein", "carbs", "fat", "calories")
        }
        overshoot = max(0.0, added["calories"] - remaining["calories"]) / max(remaining["calories"], 1)
        score = (
            coverage["protein"] * 0.35
            + coverage["carbs"] * 0.25
            + coverage["fat"] * 0.10
            + coverage["calories"] * 0.30
            - overshoot * 1.5
            + (0.03 if familiar else 0)
        )
        if best is None or score > best[0]:
            best = (score, row, quantity, added, "білок" if coverage["protein"] >= coverage["carbs"] else "вуглеводи")
    return best


@router.callback_query(F.data.startswith("food_advice:"))
async def food_advice(callback: CallbackQuery) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    diet_type = callback.data.split(":", 1)[1]
    today = local_today().isoformat()
    weight, activity, _ = await source_values(profile, diet_type)
    targets = calculate_targets(profile, weight, activity)
    food_rows = await db.food_day(profile["id"], today, diet_type)
    totals = food_totals(food_rows)
    remaining = {
        "calories": round(targets["total"] - totals["calories"], 1),
        "protein": round(targets["protein"] - totals["protein"], 1),
        "fat": round(targets["fat"] - totals["fat"], 1),
        "carbs": round(targets["carbs"] - totals["carbs"], 1),
    }
    current, previous = await db.activity_pair(profile["id"], today)
    activity_note = ""
    if current and previous:
        current_total = total_activity(current)
        previous_total = total_activity(previous)
        difference = round(current_total - previous_total)
        activity_note = f"\nАктивність: {current_total:.0f} ккал проти {previous_total:.0f} ккал минулого тижня ({difference:+d})."

    current_product_ids = {row["product_id"] for row in food_rows}
    catalog = await db.foods(await db.food_owner(callback.from_user.id))
    candidates = []
    for row in catalog:
        candidate = advice_candidate(row, remaining, row["id"] in current_product_ids)
        if candidate:
            candidates.append(candidate)

    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        message = "💡 Порада на сьогодні\n\n"
        if remaining["calories"] <= 0:
            message += "Калорійну ціль уже досягнуто або перевищено. Додавати продукт не рекомендую."
        elif remaining["protein"] > 10:
            message += "Не вистачає білка, але серед уже доданих продуктів немає зручного варіанта в межах залишку калорій."
        else:
            message += "Раціон уже близький до цільових показників. Додатковий продукт не потрібен."
        await callback.message.edit_text(message + activity_note, reply_markup=nav())
        await callback.answer()
        return

    lines = [
        f"💡 <b>Порада на сьогодні</b>{activity_note}",
        "",
        f"Залишилось: {remaining['calories']:g} ккал · Б {remaining['protein']:g} г · Ж {remaining['fat']:g} г · В {remaining['carbs']:g} г",
        "",
        "Оберіть одну порцію. Розрахунок враховує калорії та всі макронутрієнти:",
        "",
    ]
    advice_buttons = []
    for index, (_, row, quantity, nutrients, priority) in enumerate(candidates[:3], 1):
        safe_name = escape(row["name"])
        lines.append(f"{index}. <b>{safe_name}</b> — {quantity:g} {row['unit']} (+{nutrients['calories']:g} ккал, Б +{nutrients['protein']:g} г, Ж +{nutrients['fat']:g} г, В +{nutrients['carbs']:g} г; {priority})")
        advice_buttons.append([(f"✅ Додати {row['name']} {quantity:g} {row['unit']}", f"food_advice_add:{diet_type}:{row['id']}:{quantity:g}")])
    advice_buttons.append([('❌ Не додавати', f'food:{diet_type}')])
    await callback.message.edit_text("\n".join(lines), reply_markup=buttons(advice_buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("food_advice_add:"))
async def food_advice_add(callback: CallbackQuery) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    _, diet_type, raw_product_id, raw_quantity = callback.data.split(":")
    profile = await account_profile(callback.from_user.id)
    log_date = local_today().isoformat()
    product_id = int(raw_product_id)
    quantity = float(raw_quantity)
    rows = await db.food_day(profile["id"], log_date, diet_type)
    if any(row["meal"] == "Рекомендація" and row["product_id"] == product_id for row in rows):
        text, markup = await food_screen(callback.from_user.id, diet_type)
        await callback.message.edit_text("✅ Цю рекомендацію вже було додано.\n\n" + text, reply_markup=markup)
        await callback.answer("Рекомендація вже додана", show_alert=True)
        return
    try:
        await db.add_food_log(profile["id"], log_date, "Рекомендація", product_id, quantity, diet_type)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        await food_advice(callback)
        return
    text, markup = await food_screen(callback.from_user.id, diet_type)
    await callback.message.edit_text("✅ Рекомендацію додано.\n\n" + text, reply_markup=markup)
    await callback.answer("Додано")


@router.callback_query(F.data.startswith("food_add:"))
async def food_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    diet_type = callback.data.split(":", 1)[1]
    await open_food_add(callback.message, state, diet_type, edit=True)
    await callback.answer()


async def open_food_add(message: Message, state: FSMContext, diet_type: str = DIET_REST, edit: bool = False) -> None:
    await state.clear()
    await state.update_data(diet_type=diet_type)
    text = "➕ Додавання продукту\n\nКрок 1 з 3: оберіть прийом їжі:"
    markup = buttons([
            [('🌅 Сніданок', 'meal:Сніданок'), ('☀️ Обід', 'meal:Обід')],
            [('🌙 Вечеря', 'meal:Вечеря'), ('🍎 Перекус', 'meal:Перекус')],
            [('✏️ Інша назва', 'meal:custom')],
            [('← Назад', f'food:{diet_type}')],
        ])
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("meal:"))
async def meal_selected(callback: CallbackQuery, state: FSMContext) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    meal = callback.data.split(":", 1)[1]
    if meal == "custom":
        await state.set_state(InputFlow.food_meal)
        await callback.message.edit_text("Крок 1 з 3: введіть назву прийому їжі:", reply_markup=nav())
    else:
        await state.update_data(meal=meal)
        await state.set_state(InputFlow.food_product)
        data = await state.get_data()
        await show_food_choices(callback.message, callback.from_user.id, data.get("diet_type", DIET_REST), edit=True)
    await callback.answer()


async def show_food_choices(message: Message, user_id: int, diet_type: str = DIET_REST, edit: bool = False) -> None:
    foods = await db.foods(await db.food_owner(user_id))
    rows = [[(food["name"], f"food_pick:{food['id']}")] for food in foods]
    rows += [[('🔎 Пошук за назвою', 'food_search')], [('← Назад', f'food:{diet_type}')]]
    text = "Крок 2 з 3: оберіть продукт зі списку:"
    if edit:
        await message.edit_text(text, reply_markup=buttons(rows))
    else:
        await message.answer(text, reply_markup=buttons(rows))


@router.callback_query(F.data == "food_search")
async def food_search(callback: CallbackQuery, state: FSMContext) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    await state.set_state(InputFlow.food_product)
    data = await state.get_data()
    await state.update_data(diet_type=data.get("diet_type", DIET_REST))
    await callback.message.edit_text("Введіть назву продукту або частину назви:", reply_markup=nav())
    await callback.answer()


@router.callback_query(F.data.startswith("food_pick:"))
async def food_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    product_id = int(callback.data.split(":", 1)[1])
    foods = await db.foods(await db.food_owner(callback.from_user.id))
    product = next((food for food in foods if food["id"] == product_id), None)
    if not product:
        await callback.answer("Продукт не знайдено. Відкрийте список ще раз.", show_alert=True)
        return
    await state.update_data(product_id=product["id"], unit=product["unit"], product_name=product["name"])
    await state.set_state(InputFlow.food_quantity)
    hint = "кількість у штуках" if product["unit"] == "шт" else f"кількість у {product['unit']}"
    await callback.message.edit_text(
        f"Крок 3 з 3: {product['name']}\n\nВведіть {hint}:\nНаприклад: 2 або 150",
        reply_markup=nav(),
    )
    await callback.answer()


@router.message(InputFlow.food_meal)
async def food_meal(message: Message, state: FSMContext) -> None:
    if not await may_manage_diet(message.from_user.id):
        await state.clear()
        await message.answer("Самостійне ведення раціону не дозволене.")
        return
    await state.update_data(meal=message.text.strip())
    await state.set_state(InputFlow.food_product)
    data = await state.get_data()
    await show_food_choices(message, message.from_user.id, data.get("diet_type", DIET_REST))


@router.message(InputFlow.food_product)
async def food_product(message: Message, state: FSMContext) -> None:
    if not await may_manage_diet(message.from_user.id):
        await state.clear()
        await message.answer("Самостійне ведення раціону не дозволене.")
        return
    foods = await db.foods(await db.food_owner(message.from_user.id), message.text)
    if not foods:
        await message.answer("Не знайшов продукт. Додайте його в 🍎 Продукти або спробуйте інший запит.")
        return
    await state.update_data(product_id=foods[0]["id"], unit=foods[0]["unit"], product_name=foods[0]["name"])
    await state.set_state(InputFlow.food_quantity)
    hint = "скільки штук?" if foods[0]["unit"] == "шт" else f"скільки {foods[0]['unit']}?"
    await message.answer(f"Обрано: {foods[0]['name']}. Введіть, {hint}")


@router.message(InputFlow.food_quantity)
async def food_quantity(message: Message, state: FSMContext) -> None:
    if not await may_manage_diet(message.from_user.id):
        await state.clear()
        await message.answer("Самостійне ведення раціону не дозволене.")
        return
    try:
        quantity = parse_number(message.text)
        if quantity <= 0:
            raise ValueError
        data = await state.get_data()
        profile = await account_profile(message.from_user.id)
        diet_type = data.get("diet_type", DIET_REST)
        await db.add_food_log(profile["id"], local_today().isoformat(), data["meal"], data["product_id"], quantity, diet_type)
        await state.clear()
        text, markup = await food_screen(message.from_user.id, diet_type)
        await message.answer("✅ Додано.\n\n" + text, reply_markup=markup)
    except (ValueError, KeyError):
        await message.answer("Кількість має бути числом більше нуля.")


@router.callback_query(F.data.startswith("food_edit:"))
async def food_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    _, diet_type, raw_log_id = callback.data.split(":")
    log_id = int(raw_log_id)
    await state.set_state(InputFlow.food_edit_quantity)
    await state.update_data(log_id=log_id, diet_type=diet_type)
    await callback.message.edit_text("Введіть нову кількість продукту. Десяткові значення можна писати через кому:", reply_markup=nav())
    await callback.answer()


@router.message(InputFlow.food_edit_quantity)
async def food_edit_quantity(message: Message, state: FSMContext) -> None:
    if not await may_manage_diet(message.from_user.id):
        await state.clear()
        await message.answer("Самостійне ведення раціону не дозволене.")
        return
    try:
        quantity = parse_number(message.text)
        if quantity <= 0:
            raise ValueError
        data = await state.get_data()
        profile = await account_profile(message.from_user.id)
        if not await db.update_food_log(profile["id"], int(data["log_id"]), quantity):
            raise ValueError
        await state.clear()
        text, markup = await food_screen(message.from_user.id, data.get("diet_type", DIET_REST))
        await message.answer("✅ Кількість оновлено.\n\n" + text, reply_markup=markup)
    except (ValueError, KeyError, TypeError):
        await message.answer("Кількість має бути числом більше нуля.")


@router.callback_query(F.data.startswith("food_remove:"))
async def food_remove(callback: CallbackQuery) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    _, diet_type, raw_log_id = callback.data.split(":")
    log_id = int(raw_log_id)
    removed = await db.delete_food_log(profile["id"], log_id)
    await callback.answer("Запис видалено" if removed else "Запис уже відсутній")
    text, markup = await food_screen(callback.from_user.id, diet_type)
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("food_delete:"))
async def food_delete(callback: CallbackQuery) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    diet_type = callback.data.split(":", 1)[1]
    await db.delete_last_food(profile["id"], diet_type)
    await callback.answer("Останній запис видалено")
    text, markup = await food_screen(callback.from_user.id, diet_type)
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("food_yesterday:"))
async def food_yesterday(callback: CallbackQuery) -> None:
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Самостійне ведення раціону не дозволене.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    diet_type = callback.data.split(":", 1)[1]
    count = await db.copy_yesterday(profile["id"], local_today().isoformat(), diet_type)
    await callback.answer(f"Скопійовано записів: {count}")
    text, markup = await food_screen(callback.from_user.id, diet_type)
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "weight")
async def weight_callback(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    if profile["manual_mode"]:
        text = f"⚖️ Поточна ручна вага: {profile['manual_weight'] or 71} кг\n🎯 Ціль: {profile['target_weight'] or 'не задана'} кг\nІсторія ваги з JSON-експортів не використовується."
    else:
        rows = await db.weights(profile["id"])
        stats = weight_stats(rows, profile["target_weight"])
        text = "⚖️ Історія ваги\n" + (f"Поточна: {stats['current']} кг · Перша: {stats['first']} кг · Зміна: {stats['change']:+.1f} кг\nТемп: {stats['pace']:+.2f} кг/тиждень · До цілі: {stats['eta']}\nВимірювань: {stats['count']} · Остання дата: {stats['last_date']}" if rows else "Даних ще немає.")
    await callback.message.edit_text(text, reply_markup=buttons([[('➕ Записати вагу', 'weight_add')], [('← До спорту і раціону', 'sport:menu')]]))
    await callback.answer()


@router.callback_query(F.data == "weight_add")
async def weight_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InputFlow.weight)
    await callback.message.edit_text("Введіть вагу за сьогодні (20–300 кг):", reply_markup=nav())
    await callback.answer()


@router.message(InputFlow.weight)
async def weight_message(message: Message, state: FSMContext) -> None:
    try:
        weight = parse_number(message.text)
        if not 20 <= weight <= 300:
            raise ValueError
        profile = await account_profile(message.from_user.id)
        if profile["manual_mode"]:
            await db.update_profile(profile["id"], manual_weight=weight)
        await db.upsert_weight(profile["id"], local_today().isoformat(), weight)
        await state.clear()
        await message.answer("✅ Вагу збережено.", reply_markup=await sport_markup(message.from_user.id))
    except ValueError:
        await message.answer("Введіть число від 20 до 300 кг.")


@router.callback_query(F.data == "products")
async def products_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    foods = await db.foods(await db.food_owner(callback.from_user.id))
    text = "🍎 Продукти\n" + ("\n".join(f"{f['name']} — Б{f['protein']} Ж{f['fat']} В{f['carbs']} · {f['calories']} ккал/{f['unit']}" for f in foods) if foods else "База порожня.")
    product_buttons = [[('➕ Додати продукт', 'product_add')]] if await is_admin(callback.from_user.id) else []
    await callback.message.edit_text(text, reply_markup=buttons(product_buttons + [[('← До меню «Ще»', 'more')]]))
    await callback.answer()


@router.callback_query(F.data == "product_add")
async def product_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Продукти додає лише адміністратор.", show_alert=True)
        return
    await state.set_state(InputFlow.food_name)
    await callback.message.edit_text("Введіть назву продукту:", reply_markup=nav())
    await callback.answer()


@router.message(InputFlow.food_name)
async def product_name(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Продукти додає лише адміністратор.")
        return
    name = message.text.strip()
    if not name or len(name) > 255:
        await message.answer("Назва має містити від 1 до 255 символів.")
        return
    await state.update_data(name=name)
    await state.set_state(InputFlow.food_macros)
    await message.answer("Введіть через пробіл: білки жири вуглеводи калорії (на 100 г/мл або на 1 шт).")


@router.message(InputFlow.food_macros)
async def product_macros(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Продукти додає лише адміністратор.")
        return
    try:
        values = [parse_number(item) for item in message.text.split()]
        if len(values) != 4 or min(values) < 0:
            raise ValueError
        await state.update_data(protein=values[0], fat=values[1], carbs=values[2], calories=values[3])
        await state.set_state(InputFlow.food_unit)
        await message.answer("Введіть одиницю: г, мл або шт. Для «шт» БЖВ вводяться на одну штуку.")
    except ValueError:
        await message.answer("Потрібні чотири невідʼємні числа через пробіл.")


@router.message(InputFlow.food_unit)
async def product_unit(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Продукти додає лише адміністратор.")
        return
    unit = message.text.strip().lower()
    if unit not in {"г", "мл", "шт"}:
        await message.answer("Одиниця має бути: г, мл або шт.")
        return
    data = await state.get_data()
    try:
        await db.add_food(await db.food_owner(message.from_user.id), {**data, "unit": unit, "unit_weight": None})
        await state.clear()
        await message.answer("✅ Продукт додано.", reply_markup=await sport_markup(message.from_user.id))
    except sqlite3.IntegrityError:
        await message.answer("Продукт із такою назвою вже існує. Дублікати не створюються.")


@router.callback_query(F.data == "health")
async def health_callback(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    if profile["manual_mode"]:
        activity = await db.activity_on_date(profile["id"], local_today().isoformat())
        text = f"🩺 <b>Здоровʼя</b>\n\nРучний режим\n⚖️ Вага: {profile['manual_weight'] or 71} кг\n🔥 Активність для розрахунку цілі: {profile['manual_activity'] or 0} ккал/день"
        if activity:
            text += f"\n\n📅 <b>Запис за сьогодні</b>\n👣 {activity['steps']:,.0f} кроків · {activity['active_calories']:,.0f} активних ккал"
        text += "\n\nСон і тренди з’являться після імпорту даних Health Export."
    else:
        activities = await db.activities(profile["id"], 7)
        weights = await db.weights(profile["id"])
        sleeps = await db.sleeps(profile["id"], 7)
        averages = weekly_activity_by_day_type(activities)
        lines = ["🩺 <b>Здоровʼя за останні 7 днів</b>"]
        if weights:
            lines.append(f"⚖️ Остання вага: {weights[-1]['weight']:g} кг")
        if activities:
            matching_rest = [row for row in activities if not is_training_day(row)]
            matching_training = [row for row in activities if is_training_day(row)]
            activity_rest = averages["rest"] if matching_rest else weekly_activity(activities)
            activity_training = averages["training"] if matching_training else weekly_activity(activities)
            lines.extend([
                f"👣 Середні кроки: {sum(row['steps'] for row in activities) / len(activities):.0f}/день",
                f"🔥 Активність у звичайний день: {activity_rest:.0f} ккал",
                f"🏋️ Активність у тренувальний день: {activity_training:.0f} ккал",
            ])
        if sleeps:
            sleep_rows = [row for row in sleeps if row["asleep_sec"] > 0]
            if sleep_rows:
                average_hours = sum(row["asleep_sec"] for row in sleep_rows) / len(sleep_rows) / 3600
                lines.append(f"😴 Середній сон: {average_hours:.1f} год · записів: {len(sleep_rows)}")
                if sleeps[0]["avg_heart"]:
                    lines.append(f"❤️ Середній пульс уві сні за останній запис: {sleeps[0]['avg_heart']:.0f} уд/хв")
        if len(lines) == 1:
            lines.append("Дані ще не імпортовані. Надішліть JSON-файл Health Export.")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=buttons([
        [('✍️ Додати активність', 'health:activity:add')],
        [('📥 Імпорт Health Export', 'import')],
        [('📈 Графіки', 'charts')],
        [('← До спорту і раціону', 'sport:menu')],
    ]), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "health:activity:add")
async def manual_activity_start(callback: CallbackQuery, state: FSMContext) -> None:
    if await access_denied(callback):
        return
    await state.set_state(InputFlow.manual_activity)
    await callback.message.edit_text(
        "✍️ <b>Записати активність за сьогодні</b>\n\n"
        "Введіть кількість кроків і активні калорії через пробіл.\n"
        "Наприклад: <code>8500 420</code>.\n\n"
        "Якщо за сьогодні вже є імпорт, для кроків і калорій збережеться більше значення.",
        reply_markup=nav(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(InputFlow.manual_activity)
async def manual_activity_received(message: Message, state: FSMContext) -> None:
    if await access_denied(message):
        await state.clear()
        return
    try:
        values = [parse_number(value) for value in (message.text or "").split()]
        if len(values) != 2 or any(value < 0 for value in values):
            raise ValueError
        steps, active_calories = values
        if steps > 200_000 or active_calories > 10_000:
            raise ValueError
        profile = await account_profile(message.from_user.id)
        await db.upsert_activity(profile["id"], {
            "log_date": local_today().isoformat(),
            "steps": steps,
            "active_calories": active_calories,
            "distance": 0,
            "floors": 0,
            "workout_count": 0,
        })
        await state.clear()
        await message.answer(
            f"✅ Активність записано: {steps:,.0f} кроків · {active_calories:,.0f} активних ккал.",
            reply_markup=buttons([[('🩺 До здоровʼя', 'health')], [('← До спорту і раціону', 'sport:menu')]]),
        )
    except ValueError:
        await message.answer("Введіть два невід’ємні числа: кроки та активні ккал. Наприклад: 8500 420.", reply_markup=nav())


@router.callback_query(F.data == "import")
async def import_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text("📥 Надішліть JSON-файл документом. Підтримується Health Export Kit. Нерозпізнані формати будуть відхилені.", reply_markup=more_nav())
    await callback.answer()


@router.message(F.document)
async def document_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    if await access_denied(message):
        return
    if message.document and message.document.file_size and message.document.file_size > 20 * 1024 * 1024:
        await message.answer("Файл завеликий. Максимальний розмір імпорту — 20 МБ.", reply_markup=nav())
        return
    document: Document = message.document
    filename = document.file_name or ""
    try:
        file = await bot.get_file(document.file_id)
        content = await bot.download_file(file.file_path)
        raw = content.read()
    except Exception:
        await message.answer("Не вдалося завантажити документ із Telegram. Спробуйте надіслати його ще раз.")
        return
    await message.answer(f"📥 Файл отримано: {filename or 'без назви'}. Перевіряю структуру...")
    is_food_database = filename.lower().endswith(".md") or raw.decode("utf-8-sig", errors="replace").lstrip().lower().startswith("# fooddatabase")
    if is_food_database:
        if not await is_admin(message.from_user.id):
            await message.answer("Базу продуктів імпортує лише адміністратор.")
            return
        try:
            products = parse_food_database(raw, filename)
            owner_id = await db.food_owner(message.from_user.id)
            for product in products:
                await db.upsert_food(owner_id, product)
        except Exception as exc:
            logging.error("FoodDatabase import failed: %s", exc)
            if isinstance(exc, IndexError):
                exc = ImportErrorMessage("Структура FoodDatabase.md пошкоджена: у таблиці не вистачає колонок.")
            elif not isinstance(exc, ImportErrorMessage):
                exc = ImportErrorMessage(f"Не вдалося зберегти продукт: {exc}")
            await message.answer(str(exc))
            return
        await message.answer(f"✅ Імпортовано продуктів: {len(products)}. Існуючі записи оновлено.", reply_markup=await sport_markup(message.from_user.id))
        return
    if filename.lower().endswith(".json") and await is_admin(message.from_user.id):
        try:
            products = parse_food_database(raw, filename)
        except (ImportErrorMessage, IndexError):
            products = []
        if products:
            owner_id = await db.food_owner(message.from_user.id)
            for product in products:
                await db.upsert_food(owner_id, product)
            await message.answer(f"✅ Відновлено продуктів: {len(products)}. Існуючі записи оновлено.", reply_markup=await sport_markup(message.from_user.id))
            return
    try:
        parsed = parse_health_export(raw)
    except ImportErrorMessage as exc:
        await message.answer(str(exc))
        return
    profile = await account_profile(message.from_user.id)
    for item in parsed["activities"]:
        await db.upsert_activity(profile["id"], item)
    for item in parsed["weights"]:
        await db.upsert_weight(profile["id"], item["log_date"], item["weight"], "import")
    for item in parsed["sleeps"]:
        await db.upsert_sleep(profile["id"], item)
    imported_dates = sorted({
        item["log_date"]
        for kind in ("activities", "weights", "sleeps")
        for item in parsed[kind]
    })
    date_range = (
        f"{imported_dates[0]} — {imported_dates[-1]} ({len(imported_dates)} дн.)"
        if imported_dates
        else "немає дат для імпорту"
    )
    import_summary = (
        "✅ <b>Імпорт здоров’я завершено</b>\n"
        f"📅 Діапазон даних: {date_range}\n"
        f"🏃 Днів активності: {len(parsed['activities'])}\n"
        f"⚖️ Записів ваги: {len(parsed['weights'])}\n"
        f"😴 Зведень сну: {len(parsed['sleeps'])}\n\n"
        "За дні, які вже були в базі, значення активності та тривалості сну об’єднані через максимум."
    )
    today = next((item for item in parsed["activities"] if item["log_date"] == local_today().isoformat()), None)
    if today:
        await state.set_state(InputFlow.import_workout)
        await message.answer(
            f"{import_summary}\n\n"
            f"Сьогодні: {today['steps']:.0f} кроків · {today['active_calories']:.0f} активних ккал.\n"
            "Введіть калорії тренування або 0, якщо тренування не було:",
            parse_mode="HTML",
            reply_markup=buttons([[('Пропустити', 'import_workout:skip')]]),
        )
    else:
        await message.answer(import_summary, reply_markup=await sport_markup(message.from_user.id), parse_mode="HTML")


@router.message(InputFlow.import_workout)
async def import_workout(message: Message, state: FSMContext) -> None:
    try:
        kcal = parse_number(message.text)
        if kcal < 0:
            raise ValueError
        profile = await account_profile(message.from_user.id)
        await db.set_workout_calories(profile["id"], local_today().isoformat(), kcal)
        await state.clear()
        await message.answer("✅ Тренування враховано в цілі на сьогодні.", reply_markup=await sport_markup(message.from_user.id))
    except ValueError:
        await message.answer("Введіть невідʼємне число або 0.")


@router.callback_query(F.data == "import_workout:skip")
async def import_workout_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if await access_denied(callback):
        return
    if await state.get_state() != InputFlow.import_workout.state:
        await callback.answer("Немає тренування, яке можна пропустити.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=await sport_markup(callback.from_user.id))
    await callback.answer("Тренування пропущено")


@router.callback_query(F.data == "charts")
async def charts_callback(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    if profile["manual_mode"]:
        await callback.message.edit_text("📈 Графіки недоступні в ручному режимі: немає часового ряду імпортованих даних.", reply_markup=nav())
        await callback.answer()
        return
    weights = await db.weights(profile["id"])
    activities = await db.activities(profile["id"])
    if not weights and not activities:
        await callback.message.edit_text("📈 Для графіків потрібні імпортовані дані.", reply_markup=nav())
        await callback.answer()
        return
    options = []
    if weights:
        options.append([("⚖️ Вага", "charts:weight")])
    if activities:
        options.extend([
            [("👣 Кроки", "charts:steps")],
            [("🔥 Активні калорії", "charts:active_calories")],
            [("🏋️ Калорії тренувань", "charts:workout_calories")],
            [("📏 Дистанція", "charts:distance")],
        ])
    options.append([("← До спорту і раціону", "sport:menu")])
    await callback.message.edit_text("📈 <b>Графіки</b>\n\nОберіть показник:", reply_markup=buttons(options), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("charts:"))
async def chart_selected(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    profile = await account_profile(callback.from_user.id)
    field = callback.data.split(":", 1)[1]
    if field == "weight":
        rows = await db.weights(profile["id"])
        if not rows:
            await callback.answer("Записів ваги ще немає.", show_alert=True)
            return
        path = weight_chart(rows)
        title = "⚖️ Графік ваги"
        filename = "weight.png"
    else:
        chart_fields = {
            "steps": ("Кроки", "кроків"),
            "active_calories": ("Активні калорії", "ккал"),
            "workout_calories": ("Калорії тренувань", "ккал"),
            "distance": ("Дистанція", "км"),
        }
        if field not in chart_fields:
            await callback.answer("Невідомий показник.", show_alert=True)
            return
        rows = await db.activities(profile["id"])
        if not rows:
            await callback.answer("Даних активності ще немає.", show_alert=True)
            return
        chart_title, unit = chart_fields[field]
        path = activity_chart(rows, field, chart_title, unit)
        title = f"🏃 Графік: {chart_title.casefold()}"
        filename = f"{field}.png"
    try:
        await callback.message.answer_document(BufferedInputFile(path.read_bytes(), filename=filename), caption=title)
    finally:
        path.unlink(missing_ok=True)
    await callback.answer()


async def reminder_loop(bot: Bot) -> None:
    kyiv = APP_TIMEZONE
    last_checked_minute: str | None = None
    while True:
        if await db.active_daily_summary_count() == 0 and await db.active_habit_reminder_count() == 0:
            if reminder_wakeup is None:
                await asyncio.sleep(3600)
            else:
                await reminder_wakeup.wait()
                reminder_wakeup.clear()
            continue
        now = datetime.now(kyiv)
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if minute_key != last_checked_minute:
            last_checked_minute = minute_key
            local_date = now.date().isoformat()
            time_value = now.strftime("%H:%M")
            try:
                user_ids = await db.due_daily_summary_users(time_value, local_date)
            except Exception:
                logging.exception("Could not check scheduled daily summaries")
                user_ids = []
            for user_id in user_ids:
                try:
                    claimed = await db.claim_daily_summary(user_id, time_value, local_date)
                    if not claimed:
                        continue
                    text = await daily_report(user_id, now.date())
                    await bot.send_message(
                        user_id,
                        text,
                        reply_markup=reports_markup(),
                        parse_mode="HTML",
                    )
                except Exception:
                    logging.exception("Could not send scheduled daily summary to user %s", user_id)
            try:
                habit_rows = await db.due_habit_reminders(time_value, local_date)
            except Exception:
                logging.exception("Could not check habit reminders")
                habit_rows = []
            for habit in habit_rows:
                try:
                    claimed = await db.claim_habit_reminder(habit["id"], habit["telegram_id"], time_value, local_date)
                    if not claimed:
                        continue
                    await bot.send_message(
                        int(habit["telegram_id"]),
                        f"🔁 Нагадування про звичку: <b>{escape(habit['name'])}</b>\n\nВідкрийте «Цілі та звички», щоб відмітити виконання.",
                        reply_markup=buttons([[('🎯 Цілі та звички', 'goals:menu')]]),
                        parse_mode="HTML",
                    )
                except Exception:
                    logging.exception("Could not send habit reminder %s to user %s", habit["id"], habit["telegram_id"])
        next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        delay = max(0.05, (next_minute - datetime.now(kyiv)).total_seconds())
        await asyncio.sleep(delay)


async def main() -> None:
    global db, admin_id, reminder_wakeup
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    db = Database(settings.database_path)
    bot: Bot | None = None
    reminder_task: asyncio.Task | None = None
    try:
        await db.connect()
        admin_id = await db.configure_admin(settings.admin_telegram_id)
        if not admin_id:
            raise RuntimeError("Адміністратора не налаштовано. Вкажіть ADMIN_TELEGRAM_ID у .env або відновіть наявний доступ адміністратора у базі.")
        configure_finance(db)
        configure_reflection(db)
        bot = Bot(settings.bot_token)
        reminder_wakeup = asyncio.Event()
        configure_goals(db, reminder_wakeup)
        reminder_task = asyncio.create_task(reminder_loop(bot), name="daily-summary-reminders")
        dispatcher = Dispatcher()
        dispatcher.message.outer_middleware(WhitelistMiddleware())
        dispatcher.callback_query.outer_middleware(WhitelistMiddleware())
        dispatcher.include_router(router)
        dispatcher.include_router(finance_router)
        dispatcher.include_router(goals_router)
        dispatcher.include_router(reflection_router)
        await dispatcher.start_polling(bot)
    except TelegramUnauthorizedError as exc:
        logger.critical("Telegram відхилив BOT_TOKEN. Перевірте .env і токен у @BotFather.")
        raise RuntimeError("Telegram не авторизував бота: перевірте BOT_TOKEN у .env") from exc
    except TelegramNetworkError as exc:
        logger.critical("Не вдалося підключитися до Telegram API: %s", exc)
        raise RuntimeError("Немає з'єднання з Telegram API. Перевірте інтернет або DNS.") from exc
    finally:
        if reminder_task is not None:
            reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await reminder_task
        await db.close()
        if bot is not None:
            await bot.session.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
