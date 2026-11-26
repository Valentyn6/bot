from __future__ import annotations

import io
import json
import logging
import sqlite3
import asyncio
import csv
import zipfile
import calendar
import os
import tempfile
from contextlib import suppress
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Document, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .calculations import calculate_targets, food_totals, is_profile_training_day, is_training_day, normalize_date, parse_number, weekly_activity, weekly_activity_by_day_type, weight_stats
from .charts import activity_chart, weight_chart
from .chart_delivery import send_graph
from .config import Settings
from .db import Database
from .backup import BackupError, make_backup_archive, read_backup_archive
from .import_health import ImportErrorMessage, parse_food_database, parse_health_export
from .nutrition_planner import FoodOption, MealFoodRule, MealOption, PlanResult, MEAL_LABELS, generate_day_plan, rotate_grains_by_date
from .logger_config import setup_logging
from .finance import router as finance_router
from .finance.router import configure as configure_finance, money
from .goals import router as goals_router
from .goals.router import configure as configure_goals
from .reflection import router as reflection_router
from .reflection.router import configure as configure_reflection
from .training.router import configure as configure_training, router as training_router
from .time_utils import APP_TIMEZONE, local_today


router = Router()
db: Database
admin_id = 0
backup_encryption_key = ""
maintenance_mode = False
active_requests = 0
maintenance_changed: asyncio.Event | None = None
reminder_wakeup: asyncio.Event | None = None
DIET_TRAINING = "training"
DIET_REST = "rest"
WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
NUTRITION_MEALS = {
    "breakfast": ("🌅 Сніданок", 30),
    "lunch": ("☀️ Обід", 35),
    "dinner": ("🌙 Вечеря", 35),
    "snack": ("🍎 Перекус", 0),
}
NUTRITION_BASES = {
    "database": "як у базі продуктів",
    "dry": "суха вага",
    "raw": "сира вага",
    "cooked": "готова вага",
    "piece": "штуки",
}


def backup_key_ready() -> bool:
    return len(backup_encryption_key) >= 32 and not backup_encryption_key.lower().startswith(("replace-with-", "change_me"))


class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        global active_requests
        user = getattr(event, "from_user", None)
        restore_confirm = isinstance(event, CallbackQuery) and event.data == "backup:restore-confirm"
        if maintenance_mode and not restore_confirm:
            if isinstance(event, CallbackQuery):
                await event.answer("Бот тимчасово відновлює базу. Спробуйте за хвилину.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Бот тимчасово відновлює базу. Спробуйте за хвилину.")
            return None
        active_requests += 1
        try:
            return await self._handle(handler, event, data)
        finally:
            active_requests -= 1
            if maintenance_changed:
                maintenance_changed.set()

    async def _handle(self, handler, event, data):
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
    backup_schedule = State()
    backup_restore_upload = State()
    backup_restore_confirm = State()
    nutrition_plan_portions = State()
    nutrition_auto_meal_name = State()
    nutrition_auto_meal_rename = State()
    nutrition_auto_food_portions = State()


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
    if profile["diet_mode"] in {"split", "auto"} and configured_schedule is not None:
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
        [('🛒 Список покупок на тиждень', 'shopping:list')],
        [('📊 Підсумки', 'reports:menu')],
        [('🏋️ Тренувальна програма', 'training:menu')],
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
    diet_type = DIET_TRAINING if profile['diet_mode'] in {'split', 'auto'} and training else DIET_REST
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
    auto_mode = profile["diet_mode"] == "auto"
    if auto_mode:
        diet_type = DIET_TRAINING if await current_day_is_training(profile) else DIET_REST
    else:
        diet_type = diet_type if split_mode else DIET_REST
    weight, activity, _ = await source_values(profile, diet_type)
    targets = calculate_targets(profile, weight, activity)
    day_type = "тренувальний" if await current_day_is_training(profile) else "звичайний"
    rows = await db.food_day(profile["id"], local_today().isoformat(), diet_type)
    totals = food_totals(rows)
    title = "🤖 Автоматичний раціон" if auto_mode else diet_title(diet_type) if split_mode else "🍽 Раціон"
    date_line = f"Раціон за {local_today().isoformat()} · {day_type} день" if split_mode or auto_mode else f"Раціон за {local_today().isoformat()}"
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
        diet_buttons.append([('🤖 Налаштувати й скласти меню' if auto_mode else '🤖 Скласти раціон на день', 'nutrition:auto:menu' if auto_mode else f'nutrition:menu:{diet_type}')])
        diet_buttons.append([('💡 Порада на сьогодні', f'food_advice:{diet_type}')])
        diet_buttons.append([('↩️ Видалити останній', f'food_delete:{diet_type}'), ('🔁 Як учора', f'food_yesterday:{diet_type}')])
    else:
        text += "\n\nℹ️ Раціон веде коуч. Самостійне редагування вимкнено."
    if not auto_mode:
        diet_buttons.append([('🔁 Змінити раціон', 'food')])
    diet_buttons.append([('← До спорту і раціону', 'sport:menu')])
    return text, buttons(diet_buttons)


async def nutrition_planner_access(callback: CallbackQuery) -> bool:
    if await access_denied(callback):
        return False
    if not await may_manage_diet(callback.from_user.id):
        await callback.answer("Налаштовувати генератор може власник або коуч.", show_alert=True)
        return False
    return True


async def nutrition_auto_settings_screen(profile, owner_id: int):
    meals = await db.nutrition_auto_meals(profile["id"])
    rules = await db.nutrition_auto_rules(profile["id"], owner_id)
    rows = []
    for meal in meals:
        count = sum(1 for rule in rules if int(rule["meal_id"]) == int(meal["id"]) and rule["enabled"])
        rows.append([(f"🍽 {meal['name']} · {count} продуктів", f"nutrition:auto:meal:{meal['id']}")])
    rows.extend([
        [("➕ Створити прийом їжі", "nutrition:auto:meal:add")],
        [(f"🔁 Повторення продуктів: {'дозволено' if profile['nutrition_allow_duplicates'] else 'заборонено'}", "nutrition:auto:duplicates")],
        [("📐 Межі порцій і основа ваги", "nutrition:auto:bounds")],
    ])
    if meals:
        rows.extend([
            [("✨ Створити меню на сьогодні", "nutrition:auto:generate")],
            [("🗓 Створити план на 7 днів", "nutrition:auto:week:generate")],
        ])
        saved_week = await db.nutrition_auto_week_plan_for_date(profile["id"], local_today().isoformat())
        if saved_week:
            rows.append([("📋 Відкрити поточний план", f"nutrition:auto:week:view:{saved_week['id']}")])
    rows.append([("← До раціону", "food")])
    if not meals:
        intro = "Поки не створено жодного прийому їжі. Додайте, наприклад, сніданок, обід і вечерю, а потім виберіть дозволені продукти для кожного."
    else:
        intro = "Створіть потрібні прийоми їжі, задайте порядок і виберіть дозволені продукти. Ці правила спільні для тренувальних і звичайних днів; ціль КБЖВ визначається за розкладом тренувань."
    text = f"🤖 <b>Автоматичний раціон</b>\n\n{intro}"
    return text, buttons(rows)


async def nutrition_auto_meal_detail_screen(profile_id: int, owner_id: int, meal_id: int):
    meals = list(await db.nutrition_auto_meals(profile_id))
    meal = next((item for item in meals if int(item["id"]) == meal_id), None)
    if not meal:
        return None, None
    rules = await db.nutrition_auto_rules(profile_id, owner_id)
    selected = {int(row["food_id"]) for row in rules if int(row["meal_id"]) == meal_id and row["enabled"]}
    foods = await db.foods(owner_id)
    rows = []
    for food in foods:
        check = "✅" if int(food["id"]) in selected else "▫️"
        rows.append([(f"{check} {food['name'][:48]}", f"nutrition:auto:toggle:{meal_id}:{food['id']}")])
    position = next(index for index, item in enumerate(meals) if int(item["id"]) == meal_id)
    actions = []
    if position:
        actions.append(("⬆️", f"nutrition:auto:move:{meal_id}:-1"))
    if position + 1 < len(meals):
        actions.append(("⬇️", f"nutrition:auto:move:{meal_id}:1"))
    if actions:
        rows.append(actions)
    rows.extend([
        [("✏️ Перейменувати", f"nutrition:auto:rename:{meal_id}")],
        [("📐 Межі порцій і вага", f"nutrition:auto:bounds:{meal_id}")],
        [("🗑 Видалити прийом їжі", f"nutrition:auto:delete:{meal_id}")],
        [("← До автоматичного раціону", "nutrition:auto:menu")],
    ])
    return f"🍽 <b>{escape(meal['name'])}</b>\n\nНатискайте продукти, щоб дозволити або заборонити їх у цьому прийомі їжі.", buttons(rows)


@router.callback_query(F.data == "nutrition:auto:menu")
async def nutrition_auto_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not await nutrition_planner_access(callback):
        return
    await state.clear()
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] != "auto":
        await callback.answer("Спершу виберіть режим «Автоматичний раціон» в адмін-панелі.", show_alert=True)
        return
    text, markup = await nutrition_auto_settings_screen(profile, await db.food_owner(callback.from_user.id))
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "nutrition:auto:meal:add")
async def nutrition_auto_meal_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await nutrition_planner_access(callback):
        return
    await state.clear()
    await state.set_state(InputFlow.nutrition_auto_meal_name)
    await callback.message.edit_text(
        "Введіть назву нового прийому їжі (до 32 символів), наприклад «Сніданок» або «Перекус 2».",
        reply_markup=buttons([[('✖️ Скасувати', 'nutrition:auto:menu')]]),
    )
    await callback.answer()


@router.message(InputFlow.nutrition_auto_meal_name)
async def nutrition_auto_meal_add_received(message: Message, state: FSMContext) -> None:
    if await access_denied(message) or not await may_manage_diet(message.from_user.id):
        await state.clear()
        return
    try:
        profile = await account_profile(message.from_user.id)
        await db.add_nutrition_auto_meal(profile["id"], message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    text, markup = await nutrition_auto_settings_screen(profile, await db.food_owner(message.from_user.id))
    await message.answer("✅ Прийом їжі створено. Тепер відкрийте його й виберіть продукти.\n\n" + text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.regexp(r"^nutrition:auto:meal:\d+$"))
async def nutrition_auto_meal_detail(callback: CallbackQuery, state: FSMContext) -> None:
    if not await nutrition_planner_access(callback):
        return
    await state.clear()
    try:
        meal_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Прийом їжі не знайдено.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    text, markup = await nutrition_auto_meal_detail_screen(profile["id"], await db.food_owner(callback.from_user.id), meal_id)
    if not text:
        await callback.answer("Прийом їжі вже видалено.", show_alert=True)
        return
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:auto:rename:"))
async def nutrition_auto_meal_rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await nutrition_planner_access(callback):
        return
    meal_id = int(callback.data.rsplit(":", 1)[1])
    profile = await account_profile(callback.from_user.id)
    meal = next((item for item in await db.nutrition_auto_meals(profile["id"]) if int(item["id"]) == meal_id), None)
    if not meal:
        await callback.answer("Прийом їжі не знайдено.", show_alert=True)
        return
    await state.clear()
    await state.set_state(InputFlow.nutrition_auto_meal_rename)
    await state.update_data(nutrition_auto_meal_id=meal_id)
    await callback.message.edit_text(
        f"Введіть нову назву для «{escape(meal['name'])}» (до 32 символів).",
        reply_markup=buttons([[('✖️ Скасувати', f"nutrition:auto:meal:{meal_id}")]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(InputFlow.nutrition_auto_meal_rename)
async def nutrition_auto_meal_rename_received(message: Message, state: FSMContext) -> None:
    if await access_denied(message) or not await may_manage_diet(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    try:
        profile = await account_profile(message.from_user.id)
        await db.rename_nutrition_auto_meal(profile["id"], int(data["nutrition_auto_meal_id"]), message.text or "")
    except (ValueError, KeyError) as exc:
        await message.answer(str(exc) or "Не вдалося змінити назву.")
        return
    await state.clear()
    text, markup = await nutrition_auto_meal_detail_screen(profile["id"], await db.food_owner(message.from_user.id), int(data["nutrition_auto_meal_id"]))
    await message.answer("✅ Назву змінено.\n\n" + text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("nutrition:auto:move:"))
async def nutrition_auto_meal_move(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, _, raw_id, raw_direction = callback.data.split(":")
        meal_id, direction = int(raw_id), int(raw_direction)
    except ValueError:
        await callback.answer("Не вдалося змінити порядок.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    await db.move_nutrition_auto_meal(profile["id"], meal_id, direction)
    text, markup = await nutrition_auto_meal_detail_screen(profile["id"], await db.food_owner(callback.from_user.id), meal_id)
    if text:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Порядок оновлено.")


@router.callback_query(F.data.regexp(r"^nutrition:auto:delete:\d+$"))
async def nutrition_auto_meal_delete_prompt(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    meal_id = int(callback.data.rsplit(":", 1)[1])
    profile = await account_profile(callback.from_user.id)
    meal = next((item for item in await db.nutrition_auto_meals(profile["id"]) if int(item["id"]) == meal_id), None)
    if not meal:
        await callback.answer("Прийом їжі вже видалено.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Видалити «{escape(meal['name'])}» і налаштування його продуктів?",
        reply_markup=buttons([
            [("Так, видалити", f"nutrition:auto:delete-confirm:{meal_id}")],
            [("← Назад", f"nutrition:auto:meal:{meal_id}")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:auto:delete-confirm:"))
async def nutrition_auto_meal_delete_confirm(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    meal_id = int(callback.data.rsplit(":", 1)[1])
    profile = await account_profile(callback.from_user.id)
    await db.delete_nutrition_auto_meal(profile["id"], meal_id)
    text, markup = await nutrition_auto_settings_screen(profile, await db.food_owner(callback.from_user.id))
    await callback.message.edit_text("🗑 Прийом їжі та його правила видалено.\n\n" + text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Видалено")


@router.callback_query(F.data.startswith("nutrition:auto:toggle:"))
async def nutrition_auto_food_toggle(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, _, meal_text, food_text = callback.data.split(":")
        meal_id, food_id = int(meal_text), int(food_text)
    except ValueError:
        await callback.answer("Некоректний продукт.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    rules = await db.nutrition_auto_rules(profile["id"], await db.food_owner(callback.from_user.id))
    currently_enabled = any(int(rule["meal_id"]) == meal_id and int(rule["food_id"]) == food_id and rule["enabled"] for rule in rules)
    try:
        await db.set_nutrition_auto_food_enabled(profile["id"], meal_id, food_id, not currently_enabled, await db.food_owner(callback.from_user.id))
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    text, markup = await nutrition_auto_meal_detail_screen(profile["id"], await db.food_owner(callback.from_user.id), meal_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Продукт дозволено." if not currently_enabled else "Продукт заборонено.")


@router.callback_query(F.data == "nutrition:auto:duplicates")
async def nutrition_auto_duplicates_toggle(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    profile = await account_profile(callback.from_user.id)
    enabled = not bool(profile["nutrition_allow_duplicates"])
    await db.update_profile(profile["id"], nutrition_allow_duplicates=int(enabled))
    profile = await account_profile(callback.from_user.id)
    text, markup = await nutrition_auto_settings_screen(profile, await db.food_owner(callback.from_user.id))
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Повторення дозволено." if enabled else "Повторення заборонено.")


@router.callback_query(F.data == "nutrition:auto:bounds")
async def nutrition_auto_bounds_list(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    profile = await account_profile(callback.from_user.id)
    rules = [row for row in await db.nutrition_auto_rules(profile["id"], await db.food_owner(callback.from_user.id)) if row["enabled"]]
    rows = [[(
        f"{rule['meal_name']} · {rule['name'][:24]} · до {rule['maximum']:g} {rule['unit']}",
        f"nutrition:auto:rule:{rule['meal_id']}:{rule['food_id']}",
    )] for rule in rules]
    rows.append([("← До автоматичного раціону", "nutrition:auto:menu")])
    text = "📐 <b>Межі порцій і основа ваги</b>\n\nВиберіть продукт, щоб змінити його порцію, крок або вагу."
    if not rules:
        text += "\n\nПоки немає дозволених продуктів. Спочатку створіть прийом їжі й виберіть у ньому продукти."
    await callback.message.edit_text(text, reply_markup=buttons(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:auto:bounds:"))
async def nutrition_auto_meal_bounds(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        meal_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Прийом їжі не знайдено.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    rules = [row for row in await db.nutrition_auto_rules(profile["id"], await db.food_owner(callback.from_user.id)) if int(row["meal_id"]) == meal_id and row["enabled"]]
    rows = [[(
        f"{rule['name'][:30]} · до {rule['maximum']:g} {rule['unit']}",
        f"nutrition:auto:rule:{meal_id}:{rule['food_id']}",
    )] for rule in rules]
    rows.append([("← До прийому їжі", f"nutrition:auto:meal:{meal_id}")])
    name = next((item["name"] for item in await db.nutrition_auto_meals(profile["id"]) if int(item["id"]) == meal_id), "Прийом їжі")
    text = f"📐 <b>Порції · {escape(name)}</b>\n\nОберіть продукт для редагування."
    if not rules:
        text += "\n\nДля цього прийому ще не вибрано продуктів."
    await callback.message.edit_text(text, reply_markup=buttons(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:auto:rule:"))
async def nutrition_auto_rule(callback: CallbackQuery, state: FSMContext) -> None:
    if not await nutrition_planner_access(callback):
        return
    await state.clear()
    try:
        _, _, _, meal_text, food_text = callback.data.split(":")
        meal_id, food_id = int(meal_text), int(food_text)
    except ValueError:
        await callback.answer("Некоректний продукт.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    owner_id = await db.food_owner(callback.from_user.id)
    row = next((item for item in await db.nutrition_auto_rules(profile["id"], owner_id) if int(item["meal_id"]) == meal_id and int(item["food_id"]) == food_id and item["enabled"]), None)
    if not row:
        await callback.answer("Продукт не дозволений для цього прийому.", show_alert=True)
        return
    basis = NUTRITION_BASES.get(row["weight_basis"], NUTRITION_BASES["database"])
    text = (
        f"📐 <b>{escape(row['name'])}</b> · {escape(row['meal_name'])}\n\n"
        f"Порція: {row['minimum']:g}–{row['maximum']:g} {row['unit']} · крок {row['step']:g}\n"
        f"Основа ваги: {basis}."
    )
    rows = [[("✏️ Змінити межі й крок", f"nutrition:auto:bounds-edit:{meal_id}:{food_id}")]]
    if row["unit"] == "шт":
        rows.append([("⚖️ Вага: штуки", f"nutrition:auto:basis:{meal_id}:{food_id}:piece")])
    else:
        rows.extend([
            [("🥣 Суха вага", f"nutrition:auto:basis:{meal_id}:{food_id}:dry"), ("🥩 Сира вага", f"nutrition:auto:basis:{meal_id}:{food_id}:raw")],
            [("🍲 Готова вага", f"nutrition:auto:basis:{meal_id}:{food_id}:cooked"), ("📦 Як у базі", f"nutrition:auto:basis:{meal_id}:{food_id}:database")],
        ])
    rows.append([("← До порцій", f"nutrition:auto:bounds:{meal_id}")])
    await callback.message.edit_text(text, reply_markup=buttons(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:auto:basis:"))
async def nutrition_auto_basis(callback: CallbackQuery, state: FSMContext) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, _, meal_text, food_text, basis = callback.data.split(":")
        meal_id, food_id = int(meal_text), int(food_text)
    except ValueError:
        await callback.answer("Некоректна основа ваги.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    updated = await db.set_nutrition_auto_weight_basis(profile["id"], meal_id, food_id, basis, await db.food_owner(callback.from_user.id))
    if not updated:
        await callback.answer("Спочатку дозвольте цей продукт для прийому.", show_alert=True)
        return
    row = next((item for item in await db.nutrition_auto_rules(profile["id"], await db.food_owner(callback.from_user.id)) if int(item["meal_id"]) == meal_id and int(item["food_id"]) == food_id and item["enabled"]), None)
    basis_label = NUTRITION_BASES.get(row["weight_basis"], NUTRITION_BASES["database"])
    text = f"📐 <b>{escape(row['name'])}</b> · {escape(row['meal_name'])}\n\nПорція: {row['minimum']:g}–{row['maximum']:g} {row['unit']} · крок {row['step']:g}\nОснова ваги: {basis_label}."
    rows = [[("✏️ Змінити межі й крок", f"nutrition:auto:bounds-edit:{meal_id}:{food_id}")]]
    if row["unit"] == "шт":
        rows.append([("⚖️ Вага: штуки", f"nutrition:auto:basis:{meal_id}:{food_id}:piece")])
    else:
        rows.extend([
            [("🥣 Суха вага", f"nutrition:auto:basis:{meal_id}:{food_id}:dry"), ("🥩 Сира вага", f"nutrition:auto:basis:{meal_id}:{food_id}:raw")],
            [("🍲 Готова вага", f"nutrition:auto:basis:{meal_id}:{food_id}:cooked"), ("📦 Як у базі", f"nutrition:auto:basis:{meal_id}:{food_id}:database")],
        ])
    rows.append([("← До порцій", f"nutrition:auto:bounds:{meal_id}")])
    await callback.message.edit_text(text, reply_markup=buttons(rows), parse_mode="HTML")
    await callback.answer("Основу ваги збережено.")


@router.callback_query(F.data.startswith("nutrition:auto:bounds-edit:"))
async def nutrition_auto_bounds_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, _, meal_text, food_text = callback.data.split(":")
        meal_id, food_id = int(meal_text), int(food_text)
    except ValueError:
        await callback.answer("Некоректні параметри порції.", show_alert=True)
        return
    await state.clear()
    await state.set_state(InputFlow.nutrition_auto_food_portions)
    await state.update_data(nutrition_auto_rule={"meal_id": meal_id, "food_id": food_id})
    await callback.message.edit_text(
        "Введіть мінімум, максимум і крок порції через пробіл. Наприклад: <code>0 120 5</code>. Для штук використовуйте цілі значення й крок 1.",
        reply_markup=buttons([[('✖️ Скасувати', f"nutrition:auto:rule:{meal_id}:{food_id}")]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(InputFlow.nutrition_auto_food_portions)
async def nutrition_auto_bounds_received(message: Message, state: FSMContext) -> None:
    if await access_denied(message) or not await may_manage_diet(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    try:
        if len((message.text or "").split()) != 3:
            raise ValueError("Введіть три числа: мінімум, максимум і крок.")
        minimum, maximum, step = (parse_number(value) for value in message.text.split())
        profile = await account_profile(message.from_user.id)
        rule = data["nutrition_auto_rule"]
        await db.update_nutrition_auto_food_rule(profile["id"], int(rule["meal_id"]), int(rule["food_id"]), minimum, maximum, step, await db.food_owner(message.from_user.id))
    except (ValueError, KeyError) as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer("✅ Межі порції збережено.", reply_markup=buttons([[('← До порцій', f"nutrition:auto:bounds:{rule['meal_id']}")]]))


async def generate_custom_nutrition_day(profile, owner_id: int, plan_date: date):
    meals = await db.nutrition_auto_meals(profile["id"])
    rows = [row for row in await db.nutrition_auto_rules(profile["id"], owner_id) if row["enabled"]]
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        food_id = int(row["food_id"])
        item = grouped.setdefault(food_id, {"row": row, "rules": []})
        item["rules"].append(MealFoodRule(
            meal_key=row["meal_key"], minimum=float(row["minimum"]), maximum=float(row["maximum"]),
            step=float(row["step"]), preferred=float(row["preferred"]) if row["preferred"] is not None else None,
        ))
    foods = []
    for food_id, item in grouped.items():
        row = item["row"]
        foods.append(FoodOption(
            product_id=food_id, name=row["name"], unit=row["unit"],
            calories=float(row["calories"]), protein=float(row["protein"]), fat=float(row["fat"]), carbs=float(row["carbs"]),
            rules=tuple(item["rules"]), weight_basis=row["weight_basis"],
        ))
    foods = rotate_grains_by_date(foods, plan_date)
    meal_options = [
        MealOption(key=row["meal_key"], calorie_share=1, required=True, label=row["name"])
        for row in meals
    ]
    activity = await db.activity_on_date(profile["id"], plan_date.isoformat())
    diet_type = DIET_TRAINING if is_profile_training_day(profile, activity, plan_date) else DIET_REST
    weight, activity_average, warning = await source_values(profile, diet_type, plan_date)
    calculated = calculate_targets(profile, weight, activity_average)
    targets = {
        "calories": float(calculated["total"]), "protein": float(calculated["protein"]),
        "fat": float(calculated["fat"]), "carbs": float(calculated["carbs"]),
    }
    if not meals:
        result = PlanResult("infeasible", message="Спочатку створіть хоча б один прийом їжі.")
    else:
        result = generate_day_plan(
            foods, targets, meal_options,
            allow_food_repeats=bool(profile["nutrition_allow_duplicates"]),
        )
    return result, targets, warning, meal_options, diet_type


async def refresh_custom_nutrition_week(profile, owner_id: int, plan_row):
    today = local_today()
    days = json.loads(plan_row["days_json"])
    changed = 0
    for index, old in enumerate(days):
        plan_date = date.fromisoformat(old["date"])
        if plan_date <= today or old.get("applied"):
            continue
        activity = await db.activity_on_date(profile["id"], plan_date.isoformat())
        diet_type = DIET_TRAINING if is_profile_training_day(profile, activity, plan_date) else DIET_REST
        weight, activity_average, _ = await source_values(profile, diet_type, plan_date)
        calculated = calculate_targets(profile, weight, activity_average)
        targets = {
            "calories": float(calculated["total"]), "protein": float(calculated["protein"]),
            "fat": float(calculated["fat"]), "carbs": float(calculated["carbs"]),
        }
        old_targets = old.get("targets") or {}
        if diet_type == old["diet_type"] and all(abs(targets[key] - float(old_targets.get(key, 0))) < 0.5 for key in targets):
            continue
        result, targets, warning, meals, diet_type = await generate_custom_nutrition_day(profile, owner_id, plan_date)
        if result.status not in {"optimal", "feasible"} or not result.items or not result.totals:
            logging.warning("Could not refresh custom nutrition plan for profile %s on %s: %s", profile["id"], plan_date, result.message)
            continue
        days[index] = {
            "date": old["date"], "diet_type": diet_type, "applied": False,
            "items": [{"product_id": item.product_id, "name": item.name, "meal_key": item.meal, "meal_name": item.meal_label, "quantity": item.quantity, "unit": item.unit, "weight_basis": item.weight_basis} for item in result.items],
            "totals": result.totals, "targets": result.targets or targets, "warning": warning,
        }
        changed += 1
    if changed:
        await db.update_nutrition_auto_week_plan_days(profile["id"], int(plan_row["id"]), days)
        plan_row = await db.nutrition_auto_week_plan(profile["id"], int(plan_row["id"]))
    return plan_row, days, changed


def custom_nutrition_week_screen(plan_row, days: list[dict], refreshed: int = 0):
    start = date.fromisoformat(plan_row["start_date"])
    end = date.fromisoformat(plan_row["end_date"])
    lines = [f"🗓 <b>План раціону · {start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}</b>"]
    if refreshed:
        lines.append(f"\nМайбутні дні оновлено за новими цілями: {refreshed}.")
    rows = []
    for day in days:
        current = date.fromisoformat(day["date"])
        label = "🏋️ тренувальний" if day["diet_type"] == DIET_TRAINING else "🛋 звичайний"
        targets, totals = day.get("targets") or {}, day.get("totals") or {}
        lines.append(
            f"\n<b>{WEEKDAY_LABELS[current.weekday()]} {current.strftime('%d.%m')}</b> · {label}\n"
            f"Ціль {float(targets.get('calories', 0)):.0f} ккал · меню {float(totals.get('calories', 0)):.0f} ккал"
            + (" · ✅ додано" if day.get("applied") else "")
        )
        rows.append([(f"{WEEKDAY_LABELS[current.weekday()]} {current.strftime('%d.%m')} · {label}", f"nutrition:auto:week:day:{plan_row['id']}:{current.isoformat()}")])
    rows.extend([
        [("🔄 Перегенерувати тиждень", "nutrition:auto:week:generate")],
        [("← До автоматичного раціону", "nutrition:auto:menu")],
    ])
    return "\n".join(lines), buttons(rows)


def custom_nutrition_week_day_screen(plan_id: int, day: dict):
    current = date.fromisoformat(day["date"])
    targets, totals = day.get("targets") or {}, day.get("totals") or {}
    kind = "тренувальний" if day["diet_type"] == DIET_TRAINING else "звичайний"
    lines = [
        f"🗓 <b>{WEEKDAY_LABELS[current.weekday()]} · {current.strftime('%d.%m.%Y')}</b>",
        f"{kind.capitalize()} день · цілі {float(targets.get('calories', 0)):.0f} ккал · меню {float(totals.get('calories', 0)):.0f} ккал",
    ]
    grouped: dict[str, list[dict]] = {}
    for item in day.get("items", []):
        grouped.setdefault(item.get("meal_name") or item.get("meal_key") or "Прийом їжі", []).append(item)
    for meal_name, items in grouped.items():
        lines.extend(("", f"<b>{escape(meal_name)}</b>"))
        lines.extend(f"• {escape(item['name'])} — {float(item['quantity']):g} {escape(item['unit'])}" for item in items)
    lines.extend((
        "",
        f"<b>Разом:</b> {float(totals.get('calories', 0)):.0f} ккал · Б {float(totals.get('protein', 0)):.1f} г · Ж {float(totals.get('fat', 0)):.1f} г · В {float(totals.get('carbs', 0)):.1f} г",
    ))
    rows = []
    if current == local_today() and not day.get("applied"):
        rows.append([("✅ Додати до раціону за сьогодні", f"nutrition:auto:week:apply:{plan_id}:{current.isoformat()}")])
    if current >= local_today() and not day.get("applied"):
        rows.append([("🔄 Перегенерувати цей день", f"nutrition:auto:week:regenerate:{plan_id}:{current.isoformat()}")])
    rows.append([("← До плану на 7 днів", f"nutrition:auto:week:view:{plan_id}")])
    return "\n".join(lines), buttons(rows)


@router.callback_query(F.data == "nutrition:auto:week:generate")
async def nutrition_auto_week_generate(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] != "auto":
        await callback.answer("Планування доступне в режимі «Автоматичний раціон».", show_alert=True)
        return
    owner_id = await db.food_owner(callback.from_user.id)
    start = local_today()
    existing_plan = await db.nutrition_auto_week_plan_for_date(profile["id"], start.isoformat())
    existing_days = {item["date"]: item for item in json.loads(existing_plan["days_json"])} if existing_plan else {}
    days = []
    await callback.answer("Створюю меню на сім днів…")
    for offset in range(7):
        plan_date = start + timedelta(days=offset)
        previous = existing_days.get(plan_date.isoformat())
        if previous and (plan_date < start or previous.get("applied")):
            days.append(previous)
            continue
        try:
            result, targets, warning, _, diet_type = await generate_custom_nutrition_day(profile, owner_id, plan_date)
        except (ValueError, KeyError) as exc:
            await callback.message.edit_text(f"⚠️ Не вдалося скласти меню на {plan_date.strftime('%d.%m')}: {escape(str(exc))}", reply_markup=buttons([[('📐 Налаштування', 'nutrition:auto:menu')]]))
            return
        if result.status not in {"optimal", "feasible"} or not result.items or not result.totals:
            await callback.message.edit_text(
                f"⚠️ Не вдалося скласти меню на {WEEKDAY_LABELS[plan_date.weekday()]} {plan_date.strftime('%d.%m')}.\n\n{escape(result.message or 'Перевірте прийоми їжі, продукти та порції.')}",
                reply_markup=buttons([[('📐 Налаштування', 'nutrition:auto:menu')]]),
            )
            return
        days.append({
            "date": plan_date.isoformat(), "diet_type": diet_type, "applied": False,
            "items": [{"product_id": item.product_id, "name": item.name, "meal_key": item.meal, "meal_name": item.meal_label, "quantity": item.quantity, "unit": item.unit, "weight_basis": item.weight_basis} for item in result.items],
            "totals": result.totals, "targets": result.targets or targets, "warning": warning,
        })
    plan_id = await db.save_nutrition_auto_week_plan(profile["id"], start.isoformat(), (start + timedelta(days=6)).isoformat(), days)
    plan_row = await db.nutrition_auto_week_plan(profile["id"], plan_id)
    text, markup = custom_nutrition_week_screen(plan_row, days)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("nutrition:auto:week:view:"))
async def nutrition_auto_week_view(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        plan_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("План не знайдено.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    plan_row = await db.nutrition_auto_week_plan(profile["id"], plan_id)
    if not plan_row:
        await callback.answer("План не знайдено.", show_alert=True)
        return
    plan_row, days, refreshed = await refresh_custom_nutrition_week(profile, await db.food_owner(callback.from_user.id), plan_row)
    text, markup = custom_nutrition_week_screen(plan_row, days, refreshed)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


async def nutrition_auto_week_get_day(callback: CallbackQuery, plan_id: int, day_text: str):
    profile = await account_profile(callback.from_user.id)
    plan_row = await db.nutrition_auto_week_plan(profile["id"], plan_id)
    if not plan_row:
        await callback.answer("План не знайдено.", show_alert=True)
        return None
    days = json.loads(plan_row["days_json"])
    day = next((entry for entry in days if entry["date"] == day_text), None)
    if not day:
        await callback.answer("День не знайдено.", show_alert=True)
        return None
    return profile, plan_row, days, day


@router.callback_query(F.data.startswith("nutrition:auto:week:day:"))
async def nutrition_auto_week_day(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, _, _, plan_text, day_text = callback.data.split(":")
        plan_id = int(plan_text)
    except ValueError:
        await callback.answer("Некоректний день.", show_alert=True)
        return
    payload = await nutrition_auto_week_get_day(callback, plan_id, day_text)
    if not payload:
        return
    _, _, _, day = payload
    text, markup = custom_nutrition_week_day_screen(plan_id, day)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:auto:week:regenerate:"))
async def nutrition_auto_week_regenerate_day(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, _, _, plan_text, day_text = callback.data.split(":")
        plan_id = int(plan_text)
    except ValueError:
        await callback.answer("Некоректний день.", show_alert=True)
        return
    payload = await nutrition_auto_week_get_day(callback, plan_id, day_text)
    if not payload:
        return
    profile, plan_row, days, day = payload
    plan_date = date.fromisoformat(day_text)
    if plan_date < local_today() or day.get("applied"):
        await callback.answer("Минулі або вже додані дні не змінюються.", show_alert=True)
        return
    await callback.answer("Перегенеровую день…")
    result, targets, warning, _, diet_type = await generate_custom_nutrition_day(profile, await db.food_owner(callback.from_user.id), plan_date)
    if result.status not in {"optimal", "feasible"} or not result.items or not result.totals:
        await callback.answer(result.message or "Не вдалося скласти меню.", show_alert=True)
        return
    new_day = {
        "date": day_text, "diet_type": diet_type, "applied": False,
        "items": [{"product_id": item.product_id, "name": item.name, "meal_key": item.meal, "meal_name": item.meal_label, "quantity": item.quantity, "unit": item.unit, "weight_basis": item.weight_basis} for item in result.items],
        "totals": result.totals, "targets": result.targets or targets, "warning": warning,
    }
    days = [new_day if entry["date"] == day_text else entry for entry in days]
    await db.update_nutrition_auto_week_plan_days(profile["id"], plan_id, days)
    text, markup = custom_nutrition_week_day_screen(plan_id, new_day)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("nutrition:auto:week:apply:"))
async def nutrition_auto_week_apply_day(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, _, _, plan_text, day_text = callback.data.split(":")
        plan_id = int(plan_text)
    except ValueError:
        await callback.answer("Некоректний день.", show_alert=True)
        return
    payload = await nutrition_auto_week_get_day(callback, plan_id, day_text)
    if not payload:
        return
    profile, plan_row, days, day = payload
    if day_text != local_today().isoformat() or day.get("applied"):
        await callback.answer("Додати можна лише сьогоднішній план і тільки один раз.", show_alert=True)
        return
    items = [{"product_id": item["product_id"], "meal": item.get("meal_name") or item.get("meal_key"), "quantity": item["quantity"], "unit": item["unit"]} for item in day["items"]]
    draft_id = await db.save_nutrition_plan_draft(profile["id"], day["diet_type"], day_text, items, day["totals"], day["targets"])
    try:
        applied = await db.apply_nutrition_plan_draft(profile["id"], draft_id, local_today().isoformat())
    except Exception:
        logging.exception("Failed to apply weekly auto nutrition plan %s for profile %s", plan_id, profile["id"])
        await callback.answer("Не вдалося додати план. Фактичний раціон не змінено.", show_alert=True)
        return
    if not applied:
        await callback.answer("План уже доданий або дата минула.", show_alert=True)
        return
    day["applied"] = True
    await db.update_nutrition_auto_week_plan_days(profile["id"], plan_id, days)
    text, markup = await food_screen(callback.from_user.id, day["diet_type"])
    await callback.message.edit_text("✅ План додано до фактичного раціону.\n\n" + text, reply_markup=markup)
    await callback.answer("Раціон оновлено")


@router.callback_query(F.data == "nutrition:auto:generate")
async def nutrition_auto_generate(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] != "auto":
        await callback.answer("Цей генератор доступний у режимі «Автоматичний раціон».", show_alert=True)
        return
    today = local_today()
    owner_id = await db.food_owner(callback.from_user.id)
    try:
        result, targets, warning, meal_options, diet_type = await generate_custom_nutrition_day(profile, owner_id, today)
    except (ValueError, KeyError) as exc:
        await callback.answer(f"Не вдалося скласти меню: {exc}", show_alert=True)
        return
    if result.status not in {"optimal", "feasible"} or not result.items or not result.totals:
        await callback.message.edit_text(
            "⚠️ <b>Не вдалося скласти меню</b>\n\n" + escape(result.message or "Перевірте прийоми їжі, вибрані продукти й межі порцій."),
            reply_markup=buttons([
                [("📐 Налаштування автоматичного раціону", "nutrition:auto:menu")],
                [("← До раціону", "food")],
            ]),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    totals = result.totals
    target_values = result.targets or targets
    day_label = "тренувальний день" if diet_type == DIET_TRAINING else "звичайний день"
    lines = [
        f"🧠 <b>Чернетка меню · {today.strftime('%d.%m.%Y')}</b>",
        f"Сьогодні {day_label} — цілі визначені за розкладом тренувань.",
        "",
        f"Цілі: {target_values['calories']:.0f} ккал · Б {target_values['protein']:.0f} г · Ж {target_values['fat']:.0f} г · В {target_values['carbs']:.0f} г",
    ]
    for meal in meal_options:
        meal_items = [item for item in result.items if item.meal == meal.key]
        if not meal_items:
            continue
        lines.extend(("", f"<b>{escape(meal.label or meal.key)}</b>"))
        for item in meal_items:
            basis = NUTRITION_BASES.get(item.weight_basis, NUTRITION_BASES["database"])
            suffix = f" · {basis}" if item.weight_basis != "database" else ""
            lines.append(f"• {escape(item.name)} — {item.quantity:g} {item.unit}{suffix}")
    lines.extend((
        "",
        f"<b>Разом:</b> {totals['calories']:.0f} ккал · Б {totals['protein']:.1f} г · Ж {totals['fat']:.1f} г · В {totals['carbs']:.1f} г",
        f"Відхилення: {totals['calories'] - target_values['calories']:+.0f} ккал · Б {totals['protein'] - target_values['protein']:+.1f} г · Ж {totals['fat'] - target_values['fat']:+.1f} г · В {totals['carbs'] - target_values['carbs']:+.1f} г",
        "",
        "Це чернетка — до щоденника вона потрапить лише після підтвердження.",
    ))
    if warning:
        lines.extend(("", escape(warning)))
    draft_id = await db.save_nutrition_plan_draft(
        profile["id"], diet_type, today.isoformat(),
        [{"product_id": item.product_id, "meal": item.meal_label, "quantity": item.quantity, "unit": item.unit} for item in result.items],
        totals, target_values,
    )
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=buttons([
            [("✅ Додати до фактичного раціону", f"nutrition:apply:{draft_id}")],
            [("🔄 Згенерувати ще раз", "nutrition:auto:generate")],
            [("📐 Налаштування автоматичного раціону", "nutrition:auto:menu")],
            [("← До раціону", "food")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer("Чернетку готово.")


def nutrition_meal_label(meal_key: str) -> str:
    return NUTRITION_MEALS.get(meal_key, (MEAL_LABELS.get(meal_key, meal_key), 0))[0]


def nutrition_rules_by_food(rows):
    grouped = {}
    for row in rows:
        item = grouped.setdefault(int(row["food_id"]), {"row": row, "rules": []})
        if row["enabled"]:
            item["rules"].append(MealFoodRule(
                meal_key=row["meal_key"], minimum=float(row["minimum"]), maximum=float(row["maximum"]),
                step=float(row["step"]), preferred=float(row["preferred"]) if row["preferred"] is not None else None,
            ))
    return grouped


async def nutrition_settings_text(profile_id: int, diet_type: str, owner_id: int):
    rows = await db.nutrition_rules(profile_id, diet_type, owner_id)
    counts = {key: sum(1 for row in rows if row["meal_key"] == key and row["enabled"]) for key in NUTRITION_MEALS}
    kind = "тренувальний" if diet_type == DIET_TRAINING else "звичайний"
    text = (
        f"🤖 <b>Генератор раціону · {kind}</b>\n\n"
        "Спочатку позначте, які продукти можна ставити в кожен прийом їжі. Це жорсткі правила: наприклад, якщо гречку не дозволити на сніданок, генератор її туди не поставить.\n\n"
        "Для кожного вибраного продукту можна налаштувати межі порції, крок і основу ваги. Дозволені крупи й макарони бот чергуватиме за днями, обираючи одну на прийом їжі. Поживність береться з бази: обирайте суху/сиру/готову вагу відповідно до того, для якого стану продукту внесені його КБЖВ. Чернетка не змінює щоденник, доки ви її не підтвердите."
    )
    markup_rows = [
        [(f"{label} · {counts[key]} продуктів", f"nutrition:meal:{diet_type}:{key}")]
        for key, (label, _) in NUTRITION_MEALS.items()
    ]
    markup_rows.extend([
        [("📐 Межі порцій і основа ваги", f"nutrition:bounds-list:{diet_type}")],
        [("✨ Згенерувати на сьогодні", f"nutrition:generate:{diet_type}")],
        [("← До раціону", f"food:{diet_type}")],
    ])
    return text, buttons(markup_rows)


@router.callback_query(F.data.startswith("nutrition:menu:"))
async def nutrition_planner_menu(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    diet_type = callback.data.rsplit(":", 1)[1]
    if diet_type not in {DIET_REST, DIET_TRAINING}:
        await callback.answer("Невідомий тип раціону.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] == "auto":
        text, markup = await nutrition_auto_settings_screen(profile, await db.food_owner(callback.from_user.id))
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback.answer()
        return
    if profile["diet_mode"] != "split":
        diet_type = DIET_REST
    owner_id = await db.food_owner(callback.from_user.id)
    text, markup = await nutrition_settings_text(profile["id"], diet_type, owner_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:meal:"))
async def nutrition_planner_meal(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, diet_type, meal_key = callback.data.split(":")
    except ValueError:
        await callback.answer("Некоректний прийом їжі.", show_alert=True)
        return
    if diet_type not in {DIET_REST, DIET_TRAINING} or meal_key not in NUTRITION_MEALS:
        await callback.answer("Невідомий прийом їжі.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    owner_id = await db.food_owner(callback.from_user.id)
    foods = await db.foods(owner_id)
    rules = await db.nutrition_rules(profile["id"], diet_type, owner_id)
    selected = {int(row["food_id"]) for row in rules if row["meal_key"] == meal_key and row["enabled"]}
    rows = [
        [(f"{'✅' if food['id'] in selected else '▫️'} {food['name'][:48]}", f"nutrition:toggle:{diet_type}:{meal_key}:{food['id']}")]
        for food in foods
    ]
    rows.extend([
        [("📐 Налаштувати порції та вагу", f"nutrition:bounds-list:{diet_type}")],
        [("← До налаштувань генератора", f"nutrition:menu:{diet_type}")],
    ])
    text = f"{nutrition_meal_label(meal_key)}\n\nНатискайте продукти, щоб дозволити або заборонити їх у цьому прийомі їжі.\n\nДля сніданку можна, наприклад, вибрати вівсянку, яйця й банан, залишивши рис і гречку вимкненими."
    await callback.message.edit_text(text, reply_markup=buttons(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:toggle:"))
async def nutrition_planner_toggle(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, diet_type, meal_key, food_id_text = callback.data.split(":")
        food_id = int(food_id_text)
    except ValueError:
        await callback.answer("Некоректний продукт.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    owner_id = await db.food_owner(callback.from_user.id)
    rows = await db.nutrition_rules(profile["id"], diet_type, owner_id)
    enabled = any(int(row["food_id"]) == food_id and row["meal_key"] == meal_key and row["enabled"] for row in rows)
    try:
        await db.set_nutrition_food_enabled(profile["id"], food_id, diet_type, meal_key, not enabled, owner_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    foods = await db.foods(owner_id)
    rules = await db.nutrition_rules(profile["id"], diet_type, owner_id)
    selected = {int(row["food_id"]) for row in rules if row["meal_key"] == meal_key and row["enabled"]}
    keyboard_rows = [
        [(f"{'✅' if food['id'] in selected else '▫️'} {food['name'][:48]}", f"nutrition:toggle:{diet_type}:{meal_key}:{food['id']}")]
        for food in foods
    ]
    keyboard_rows.extend([
        [("📐 Налаштувати порції та вагу", f"nutrition:bounds-list:{diet_type}")],
        [("← До налаштувань генератора", f"nutrition:menu:{diet_type}")],
    ])
    await callback.message.edit_reply_markup(reply_markup=buttons(keyboard_rows))
    await callback.answer("Продукт дозволено." if not enabled else "Продукт заборонено.")


@router.callback_query(F.data.startswith("nutrition:bounds-list:"))
async def nutrition_planner_bounds_list(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    diet_type = callback.data.rsplit(":", 1)[1]
    if diet_type not in {DIET_REST, DIET_TRAINING}:
        await callback.answer("Невідомий тип раціону.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    owner_id = await db.food_owner(callback.from_user.id)
    rows = [row for row in await db.nutrition_rules(profile["id"], diet_type, owner_id) if row["enabled"]]
    keyboard_rows = [
        [(f"{nutrition_meal_label(row['meal_key'])} · {row['name'][:24]} · до {row['maximum']:g} {row['unit']}", f"nutrition:rule:{diet_type}:{row['meal_key']}:{row['food_id']}")]
        for row in rows
    ]
    keyboard_rows.extend([
        [("← До налаштувань генератора", f"nutrition:menu:{diet_type}")],
    ])
    diet_label = "тренувальний" if diet_type == DIET_TRAINING else "звичайний"
    text = f"📐 <b>Порції та основа ваги · {diet_label} раціон</b>\n\nМежі зберігаються окремо для кожного продукту в кожному прийомі їжі. Виберіть рядок із потрібною назвою прийому їжі. Початковий максимум для продуктів у грамах/мл — 150 одиниць, для штук — 4."
    if not rows:
        text += "\n\nСпочатку виберіть продукти для сніданку, обіду або вечері."
    await callback.message.edit_text(text, reply_markup=buttons(keyboard_rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:rule:"))
async def nutrition_planner_rule(callback: CallbackQuery, state: FSMContext) -> None:
    if not await nutrition_planner_access(callback):
        return
    await state.clear()
    try:
        _, _, diet_type, meal_key, food_id_text = callback.data.split(":")
        food_id = int(food_id_text)
    except ValueError:
        await callback.answer("Некоректні параметри продукту.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    owner_id = await db.food_owner(callback.from_user.id)
    row = next((item for item in await db.nutrition_rules(profile["id"], diet_type, owner_id) if int(item["food_id"]) == food_id and item["meal_key"] == meal_key and item["enabled"]), None)
    if not row:
        await callback.answer("Продукт не вибрано.", show_alert=True)
        return
    basis = NUTRITION_BASES.get(row["weight_basis"], NUTRITION_BASES["database"])
    diet_label = "тренувальний" if diet_type == DIET_TRAINING else "звичайний"
    text = (
        f"📐 <b>{escape(row['name'])}</b> · {nutrition_meal_label(meal_key)} · {diet_label} раціон\n\n"
        f"Порція: {row['minimum']:g}–{row['maximum']:g} {row['unit']} · крок {row['step']:g}\n"
        f"Основа ваги: {basis}.\n\n"
        "Крок має бути цілим для яєць. Для круп зазначайте суху вагу, для м’яса — сиру."
    )
    markup_rows = [
        [("✏️ Змінити межі й крок", f"nutrition:bounds-edit:{diet_type}:{meal_key}:{food_id}")],
    ]
    if row["unit"] == "шт":
        markup_rows.append([("⚖️ Вага: штуки", f"nutrition:basis:{diet_type}:{food_id}:piece")])
    else:
        markup_rows.extend([
            [("🥣 Суха вага", f"nutrition:basis:{diet_type}:{food_id}:dry"), ("🥩 Сира вага", f"nutrition:basis:{diet_type}:{food_id}:raw")],
            [("🍲 Готова вага", f"nutrition:basis:{diet_type}:{food_id}:cooked"), ("📦 Як у базі", f"nutrition:basis:{diet_type}:{food_id}:database")],
        ])
    markup_rows.append([("← До списку порцій", f"nutrition:bounds-list:{diet_type}")])
    await callback.message.edit_text(text, reply_markup=buttons(markup_rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("nutrition:basis:"))
async def nutrition_planner_basis(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, diet_type, food_id_text, basis = callback.data.split(":")
        food_id = int(food_id_text)
    except ValueError:
        await callback.answer("Некоректна основа ваги.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    owner_id = await db.food_owner(callback.from_user.id)
    updated = await db.set_nutrition_weight_basis(profile["id"], food_id, basis, owner_id)
    if not updated:
        await callback.answer("Спочатку виберіть продукт для генератора.", show_alert=True)
        return
    rows = [row for row in await db.nutrition_rules(profile["id"], diet_type, owner_id) if row["enabled"]]
    keyboard_rows = [
        [(f"{nutrition_meal_label(row['meal_key'])} · {row['name'][:24]} · до {row['maximum']:g} {row['unit']}", f"nutrition:rule:{diet_type}:{row['meal_key']}:{row['food_id']}")]
        for row in rows
    ]
    keyboard_rows.append([("← До налаштувань генератора", f"nutrition:menu:{diet_type}")])
    diet_label = "тренувальний" if diet_type == DIET_TRAINING else "звичайний"
    await callback.message.edit_text(f"📐 <b>Порції та основа ваги · {diet_label} раціон</b>\n\nОснова ваги збережена. Межі порцій показані окремо для кожного прийому їжі.", reply_markup=buttons(keyboard_rows), parse_mode="HTML")
    await callback.answer("Основу ваги збережено.")


@router.callback_query(F.data.startswith("nutrition:bounds-edit:"))
async def nutrition_planner_bounds_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        _, _, diet_type, meal_key, food_id_text = callback.data.split(":")
        food_id = int(food_id_text)
    except ValueError:
        await callback.answer("Некоректні параметри порції.", show_alert=True)
        return
    await state.clear()
    await state.set_state(InputFlow.nutrition_plan_portions)
    await state.update_data(nutrition_rule={"diet_type": diet_type, "meal_key": meal_key, "food_id": food_id})
    await callback.message.edit_text(
        "Введіть мінімум, максимум і крок порції через пробіл. Наприклад: <code>0 120 5</code>. Для продукту в штуках — цілі значення з кроком 1, наприклад <code>0 4 1</code>.",
        reply_markup=buttons([[('✖️ Скасувати', f"nutrition:rule:{diet_type}:{meal_key}:{food_id}")]]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(InputFlow.nutrition_plan_portions)
async def nutrition_planner_bounds_received(message: Message, state: FSMContext) -> None:
    if await access_denied(message):
        return
    if not await may_manage_diet(message.from_user.id):
        await state.clear()
        await message.answer("Керувати генератором може власник або коуч.")
        return
    data = await state.get_data()
    rule = data.get("nutrition_rule", {})
    values = (message.text or "").split()
    try:
        if len(values) != 3:
            raise ValueError
        minimum, maximum, step = (parse_number(value) for value in values)
        profile = await account_profile(message.from_user.id)
        owner_id = await db.food_owner(message.from_user.id)
        await db.update_nutrition_food_rule(profile["id"], int(rule["food_id"]), rule["diet_type"], rule["meal_key"], minimum, maximum, step, owner_id)
    except (ValueError, KeyError) as exc:
        await message.answer(str(exc) or "Введіть три числа: мінімум, максимум і крок.")
        return
    await state.clear()
    rows = await db.nutrition_rules(profile["id"], rule["diet_type"], owner_id)
    selected = next((item for item in rows if int(item["food_id"]) == int(rule["food_id"]) and item["meal_key"] == rule["meal_key"]), None)
    if not selected:
        await message.answer("Не знайшов це правило. Відкрийте налаштування порцій і виберіть продукт ще раз.")
        return
    await message.answer(
        f"✅ Межі збережено: {selected['name']} · {nutrition_meal_label(rule['meal_key'])} — {selected['minimum']:g}–{selected['maximum']:g} {selected['unit']}, крок {selected['step']:g}.",
        reply_markup=buttons([[('← До порцій', f"nutrition:bounds-list:{rule['diet_type']}")]]),
    )


@router.callback_query(F.data.startswith("nutrition:generate:"))
async def nutrition_plan_generate(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    diet_type = callback.data.rsplit(":", 1)[1]
    if diet_type not in {DIET_REST, DIET_TRAINING}:
        await callback.answer("Невідомий тип раціону.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] == "auto":
        await nutrition_auto_generate(callback)
        return
    if profile["diet_mode"] != "split":
        diet_type = DIET_REST
    owner_id = await db.food_owner(callback.from_user.id)
    rules = [row for row in await db.nutrition_rules(profile["id"], diet_type, owner_id) if row["enabled"]]
    grouped = nutrition_rules_by_food(rules)
    today = local_today()
    foods = []
    for food_id, item in grouped.items():
        row = item["row"]
        if not item["rules"]:
            continue
        foods.append(FoodOption(
            product_id=food_id, name=row["name"], unit=row["unit"],
            calories=float(row["calories"]), protein=float(row["protein"]), fat=float(row["fat"]), carbs=float(row["carbs"]),
            rules=tuple(item["rules"]), weight_basis=row["weight_basis"],
        ))
    foods = list(rotate_grains_by_date(foods, today))
    enabled_meals = {rule.meal_key for food in foods for rule in food.rules}
    meal_options = [MealOption(key, share, required=True) for key, (_, share) in NUTRITION_MEALS.items() if key != "snack"]
    if "snack" in enabled_meals:
        meal_options = [
            MealOption("breakfast", 25), MealOption("lunch", 30),
            MealOption("dinner", 30), MealOption("snack", 15, required=False),
        ]
    weight, activity, warning = await source_values(profile, diet_type, today)
    calculated_targets = calculate_targets(profile, weight, activity)
    targets = {
        "calories": float(calculated_targets["total"]),
        "protein": float(calculated_targets["protein"]),
        "fat": float(calculated_targets["fat"]),
        "carbs": float(calculated_targets["carbs"]),
    }
    try:
        result = generate_day_plan(foods, targets, meal_options)
    except (ValueError, KeyError) as exc:
        await callback.answer(f"Не вдалося скласти меню: {exc}", show_alert=True)
        return
    if result.status not in {"optimal", "feasible"} or not result.items or not result.totals:
        await callback.message.edit_text(
            "⚠️ <b>Не вдалося скласти допустимий раціон</b>\n\n" + (result.message or "Перевірте продукти для кожного обов’язкового прийому їжі."),
            reply_markup=buttons([[('📐 Налаштувати продукти й порції', f"nutrition:menu:{diet_type}")], [('← До раціону', f"food:{diet_type}")]]),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    totals = result.totals
    target_values = result.targets or {key: float(targets[key]) for key in ("calories", "protein", "fat", "carbs")}
    lines = [
        f"🧠 <b>Чернетка раціону · {today.strftime('%d.%m.%Y')}</b>",
        "",
        f"Цілі: {target_values['calories']:.0f} ккал · Б {target_values['protein']:.0f} г · Ж {target_values['fat']:.0f} г · В {target_values['carbs']:.0f} г",
    ]
    for meal in meal_options:
        meal_items = [item for item in result.items if item.meal == meal.key]
        if not meal_items:
            continue
        lines.extend(("", f"<b>{nutrition_meal_label(meal.key)}</b>"))
        for item in meal_items:
            basis = NUTRITION_BASES.get(item.weight_basis, NUTRITION_BASES["database"])
            suffix = f" · {basis}" if item.weight_basis != "database" else ""
            lines.append(f"• {escape(item.name)} — {item.quantity:g} {item.unit}{suffix}")
    lines.extend((
        "",
        f"<b>Разом:</b> {totals['calories']:.0f} ккал · Б {totals['protein']:.1f} г · Ж {totals['fat']:.1f} г · В {totals['carbs']:.1f} г",
        f"Відхилення: {totals['calories'] - target_values['calories']:+.0f} ккал · Б {totals['protein'] - target_values['protein']:+.1f} г · Ж {totals['fat'] - target_values['fat']:+.1f} г · В {totals['carbs'] - target_values['carbs']:+.1f} г",
        "",
        "Це чернетка, вона ще не записана як з’їдена їжа.",
    ))
    if warning:
        lines.extend(("", escape(warning)))
    draft_id = await db.save_nutrition_plan_draft(
        profile["id"], diet_type, today.isoformat(),
        [{"product_id": item.product_id, "meal": nutrition_meal_label(item.meal), "quantity": item.quantity, "unit": item.unit} for item in result.items],
        totals, target_values,
    )
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=buttons([
            [("✅ Додати до фактичного раціону", f"nutrition:apply:{draft_id}")],
            [("🔄 Згенерувати ще раз", f"nutrition:generate:{diet_type}")],
            [("📐 Налаштування генератора", f"nutrition:menu:{diet_type}")],
            [("← До раціону", f"food:{diet_type}")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer("Чернетку готово.")


@router.callback_query(F.data.startswith("nutrition:apply:"))
async def nutrition_plan_apply(callback: CallbackQuery) -> None:
    if not await nutrition_planner_access(callback):
        return
    try:
        draft_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Чернетку не знайдено.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    try:
        applied = await db.apply_nutrition_plan_draft(profile["id"], draft_id, local_today().isoformat())
    except Exception:
        logging.exception("Failed to apply generated nutrition plan for profile %s", profile["id"])
        await callback.answer("Не вдалося додати план. Фактичний раціон не змінено.", show_alert=True)
        return
    if not applied:
        await callback.answer("Цей план уже доданий або він не на сьогодні.", show_alert=True)
        return
    draft = await db.nutrition_plan_draft(profile["id"], draft_id)
    diet_type = draft["diet_type"]
    text, markup = await food_screen(callback.from_user.id, diet_type)
    await callback.message.edit_text(
        "✅ Новий план записано замість попереднього згенерованого меню на цей день. Ручні записи збережено. Перевірте раціон і за потреби змініть порції.\n\n" + text,
        reply_markup=markup,
    )
    await callback.answer("Раціон оновлено.")


async def shopping_list_screen(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    profile = await account_profile(user_id)
    today = local_today()
    if profile["diet_mode"] == "auto":
        plan_row = await db.nutrition_auto_week_plan_for_date(profile["id"], today.isoformat())
        if plan_row:
            plan_row, days, _ = await refresh_custom_nutrition_week(profile, await db.food_owner(user_id), plan_row)
            totals: dict[tuple[int, str], dict[str, Any]] = {}
            for day in days:
                if day.get("applied") or date.fromisoformat(day["date"]) < today:
                    continue
                for item in day.get("items", []):
                    key = (int(item["product_id"]), str(item["unit"]))
                    if key not in totals:
                        totals[key] = {"name": item["name"], "unit": item["unit"], "quantity": 0.0}
                    totals[key]["quantity"] += float(item["quantity"])
            start = date.fromisoformat(plan_row["start_date"])
            end = date.fromisoformat(plan_row["end_date"])
            lines = [f"🛒 <b>Список покупок · план {start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}</b>", "", "Кількості для сьогоднішнього й майбутніх днів, які ще не додані до щоденника."]
            if totals:
                lines.extend(["", "<b>Купити:</b>"])
                for item in sorted(totals.values(), key=lambda entry: str(entry["name"]).casefold()):
                    lines.append(f"• {escape(str(item['name']))} — {float(item['quantity']):g} {escape(str(item['unit']))}")
            else:
                lines.extend(["", "У плані не залишилося майбутніх меню для покупок."])
            return "\n".join(lines), buttons([
                [("📋 Відкрити план", f"nutrition:auto:week:view:{plan_row['id']}")],
                [("← До спорту і раціону", "sport:menu")],
            ])
        return (
            "🛒 <b>Список покупок</b>\n\nСпочатку створіть план автоматичного раціону на 7 днів.",
            buttons([[('🗓 Створити план на 7 днів', 'nutrition:auto:week:generate')], [('← До спорту і раціону', 'sport:menu')]]),
        )
    week_days = [today + timedelta(days=offset) for offset in range(7)]
    split_mode = profile["diet_mode"] in {"split", "auto"}
    selected_weekdays = saved_training_weekdays(profile) if split_mode else []
    auto_schedule_note = ""

    if split_mode and selected_weekdays is None:
        recent_since = today - timedelta(days=27)
        session_dates = await db.training_session_dates(profile["id"], recent_since.isoformat())
        recent_training_weekdays = {
            date.fromisoformat(session_date).weekday()
            for session_date in session_dates
            if session_date < today.isoformat()
        }
        selected_weekdays = sorted(recent_training_weekdays)
        auto_schedule_note = (
            "\nАвтовизначення: розклад оцінено за тренуваннями за останні 4 тижні."
            if selected_weekdays else
            "\nАвтовизначення: за останні 4 тижні тренувань не знайдено, тому для прогнозу враховано звичайний раціон на всі дні."
        )

    training_count = sum(day.weekday() in set(selected_weekdays or []) for day in week_days) if split_mode else 0
    rest_count = len(week_days) - training_count
    needed = {DIET_REST: rest_count, DIET_TRAINING: training_count}
    totals: dict[tuple[int, str], dict[str, object]] = {}
    source_dates: dict[str, str] = {}
    missing: list[str] = []

    for diet_type, day_count in needed.items():
        if not day_count:
            continue
        plan = await db.latest_food_plan_day(profile["id"], diet_type)
        if not plan:
            missing.append(diet_type)
            continue
        source_date, products = plan
        source_dates[diet_type] = source_date
        for product in products:
            key = (product["product_id"], product["unit"])
            if key not in totals:
                totals[key] = {"name": product["name"], "unit": product["unit"], "quantity": 0.0}
            totals[key]["quantity"] = float(totals[key]["quantity"]) + float(product["quantity"]) * day_count

    start_text = today.strftime("%d.%m")
    end_text = week_days[-1].strftime("%d.%m.%Y")
    if missing:
        diet_names = {DIET_TRAINING: "тренувальний", DIET_REST: "звичайний"}
        need_text = ", ".join(diet_names[item] for item in missing)
        rows = []
        for diet_type in missing:
            label = "тренувальний" if diet_type == DIET_TRAINING else "звичайний"
            rows.append([(f"🍽 Заповнити {label} раціон", f"food:{diet_type}")])
        rows.append([("← До спорту і раціону", "sport:menu")])
        return (
            f"🛒 <b>Список покупок · {start_text}–{end_text}</b>\n\n"
            f"Не можу скласти повний список: ще немає записаного {need_text} раціону. "
            "Додай продукти в цей раціон — тоді бот візьме останній заповнений день як шаблон.",
            buttons(rows),
        )

    lines = [
        f"🛒 <b>Список покупок · {start_text}–{end_text}</b>",
        "",
        f"На 7 днів: звичайних — {rest_count}, тренувальних — {training_count}.",
        "",
    ]
    for diet_type, source_date in source_dates.items():
        label = "тренувальний" if diet_type == DIET_TRAINING else "звичайний"
        lines.append(f"Шаблон {label} раціону: {source_date}.")
    lines.append(auto_schedule_note)
    if totals:
        lines.extend(["", "<b>Купити:</b>"])
        for item in sorted(totals.values(), key=lambda entry: str(entry["name"]).casefold()):
            quantity = float(item["quantity"])
            quantity_text = f"{quantity:g}".replace(".", ",")
            lines.append(f"• {escape(str(item['name']))} — {quantity_text} {escape(str(item['unit']))}")
    else:
        lines.extend(["", "У вибраних раціонах поки немає продуктів."])

    return "\n".join(line for line in lines if line is not None), buttons([
        [("🔄 Оновити список", "shopping:list")],
        [("← До спорту і раціону", "sport:menu")],
    ])


@router.callback_query(F.data == "shopping:list")
async def shopping_list_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    text, markup = await shopping_list_screen(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


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
        account = await db.account(message.from_user.id)
        await message.answer("Вітаю! Створено профіль «Я» з дефолтними значеннями. Їх можна змінити в 👤 Профіль.")
    if not account["setup_completed"]:
        await message.answer(
            "👋 <b>Налаштуймо бота</b>\n\n"
            "Профіль уже створено. Можеш одразу перейти до головного меню. Фінанси та інші розділи налаштовуються окремо, коли вони тобі знадобляться.",
            reply_markup=buttons([
                [("Відкрити головне меню", "setup:finish")],
            ]),
            parse_mode="HTML",
        )
        return
    await message.answer(main_text(), reply_markup=await home_markup(message.from_user.id), parse_mode="HTML")


@router.callback_query(F.data == "setup:finish")
async def finish_initial_setup(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await db.set_setup_completed(callback.from_user.id)
    await callback.message.edit_text(main_text(), reply_markup=await home_markup(callback.from_user.id), parse_mode="HTML")
    await callback.answer("Налаштування завершено")


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
    if message.from_user.id != admin_id or message.chat.type != "private":
        await message.answer("Керування повними копіями доступне лише власнику в особистому чаті.")
        return
    await show_backup_center(message)


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
        caption="📤 Ваші дані збережено в окремих CSV-файлах. Архів містить раціон, здоров’я, тренувальну програму й історію, фінанси, цілі, звички та записи рефлексії.",
    )


def backup_center_markup(schedule) -> InlineKeyboardMarkup:
    return buttons([
        [('💾 Створити копію зараз', 'backup:create')],
        [('📅 Автоматично: щодня', 'backup:daily'), ('🗓 Щотижня', 'backup:weekly')],
        [('📆 Щомісяця', 'backup:monthly'), ('🔕 Вимкнути розклад', 'backup:off')],
        [('♻️ Відновити з файла', 'backup:restore')],
        [('← До адмін-панелі', 'admin')],
    ])


async def backup_center_text() -> str:
    schedule = await db.backup_schedule()
    if schedule["frequency"] == "off":
        status = "Автоматичні копії вимкнені."
    else:
        freq = {"daily": "щодня", "weekly": f"щотижня ({WEEKDAY_LABELS[schedule['weekday']]})", "monthly": f"щомісяця ({schedule['monthday']} числа)"}.get(schedule["frequency"], "вимкнені")
        status = f"Розклад: {freq} о {schedule['send_time']} за Києвом."
    last = schedule["last_success_at"]
    if last:
        try:
            last_display = datetime.fromisoformat(last).astimezone(APP_TIMEZONE).strftime("%d.%m.%Y о %H:%M")
        except (TypeError, ValueError):
            last_display = str(last)
        status += f"\nОстання успішна копія: {last_display} за Києвом."
    else:
        status += "\nУспішних резервних копій ще немає."
    if schedule["last_error"]:
        attempt = schedule["last_attempt_at"]
        try:
            attempt_display = datetime.fromisoformat(attempt).astimezone(APP_TIMEZONE).strftime("%d.%m.%Y о %H:%M") if attempt else None
        except (TypeError, ValueError):
            attempt_display = str(attempt) if attempt else None
        status += "\n⚠️ Остання автоматична спроба завершилася помилкою" + (f" ({attempt_display})" if attempt_display else "") + "."
    key_status = "налаштований" if backup_key_ready() else "не налаштований"
    return (
        "🗄 <b>Резервні копії</b>\n\n"
        f"{status}\n\n"
        "Архів містить повну SQLite-базу бота: профілі й доступи, продукти та раціони, активність і тренування, фінансові рахунки й операції, цілі з прогресом, звички з відмітками, щоденну й тижневу рефлексію та налаштування. "
        "Копія шифрується перед надсиланням у Telegram.\n"
        f"🔐 Ключ шифрування: {key_status}. Задайте <code>BACKUP_ENCRYPTION_KEY</code> у .env (від 32 символів) і збережіть його окремо."
    )


async def show_backup_center(target: Message | CallbackQuery) -> None:
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(await backup_center_text(), reply_markup=backup_center_markup(None), parse_mode="HTML")
    else:
        await target.answer(await backup_center_text(), reply_markup=backup_center_markup(None), parse_mode="HTML")


async def send_database_backup(target: Message, *, automatic: bool = False) -> None:
    if not backup_key_ready():
        raise BackupError("Налаштуйте BACKUP_ENCRYPTION_KEY у .env (від 32 символів), щоб створювати зашифровані копії.")
    payload = await db.sqlite_snapshot()
    now = datetime.now(APP_TIMEZONE)
    archive = make_backup_archive(payload, backup_encryption_key, now)
    stamp = now.strftime("%Y%m%d_%H%M")
    caption = "🗄 Зашифрована повна копія бази. Для відновлення потрібен BACKUP_ENCRYPTION_KEY."
    await target.answer_document(BufferedInputFile(archive, filename=f"fitness_bot_backup_{stamp}.fbackup"), caption=caption)
    await db.record_manual_backup_success()


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
    await callback.message.edit_text("⋯ <b>Інструменти</b>\n\nІмпорт даних, база продуктів, особистий експорт і допомога.", reply_markup=await more_markup(callback.from_user.id), parse_mode="HTML")
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
        "🗄 <b>Резервні копії</b> — повна база бота для власника, у меню адмін-панелі.\n\n"
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
    await callback.message.edit_text(await backup_center_text(), reply_markup=backup_center_markup(None), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "backup:create")
async def backup_create_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    if callback.from_user.id != admin_id or callback.message.chat.type != "private":
        await callback.answer("Доступ лише власнику в особистому чаті.", show_alert=True)
        return
    try:
        await send_database_backup(callback.message)
    except BackupError as exc:
        await callback.message.answer(str(exc))
    else:
        await callback.message.edit_text(await backup_center_text(), reply_markup=backup_center_markup(None), parse_mode="HTML")
    await callback.answer("Копію підготовлено")


@router.callback_query(F.data.in_({"backup:daily", "backup:weekly", "backup:monthly", "backup:off", "backup:restore", "backup:cancel"}))
async def backup_action_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if await access_denied(callback):
        return
    if callback.from_user.id != admin_id or callback.message.chat.type != "private":
        await callback.answer("Доступ лише власнику в особистому чаті.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    if action in {"daily", "weekly", "monthly"}:
        if not backup_key_ready():
            await callback.answer("Спочатку налаштуйте BACKUP_ENCRYPTION_KEY на сервері.", show_alert=True)
            return
        await state.set_state(InputFlow.backup_schedule)
        await state.update_data(backup_frequency=action)
        hint = {"daily": "Введіть час за Києвом у форматі ГГ:ХХ, наприклад 20:00.", "weekly": "Введіть день і час, наприклад Пн 20:00.", "monthly": "Введіть число місяця та час, наприклад 1 20:00."}[action]
        await callback.message.edit_text(f"📅 <b>Розклад копій</b>\n\n{hint}", reply_markup=buttons([[('✖️ Скасувати', 'backup:cancel')]]), parse_mode="HTML")
    elif action == "off":
        await db.set_backup_schedule("off")
        if reminder_wakeup:
            reminder_wakeup.set()
        await callback.message.edit_text(await backup_center_text(), reply_markup=backup_center_markup(None), parse_mode="HTML")
    elif action == "restore":
        await state.clear()
        await state.set_state(InputFlow.backup_restore_upload)
        await callback.message.edit_text("♻️ Надішліть файл резервної копії документом. Підтримуються зашифровані .fbackup та старі ZIP-копії.", reply_markup=buttons([[('✖️ Скасувати', 'backup:cancel')]]))
    elif action == "cancel":
        await state.clear()
        await callback.message.edit_text(await backup_center_text(), reply_markup=backup_center_markup(None), parse_mode="HTML")
    await callback.answer()


@router.message(InputFlow.backup_schedule)
async def backup_schedule_received(message: Message, state: FSMContext) -> None:
    if message.from_user.id != admin_id or message.chat.type != "private":
        await state.clear()
        return
    data = await state.get_data()
    frequency = data.get("backup_frequency")
    parts = (message.text or "").strip().split()
    weekday = 0
    monthday = 1
    time_value = ""
    try:
        if frequency == "daily" and len(parts) == 1:
            time_value = datetime.strptime(parts[0], "%H:%M").strftime("%H:%M")
        elif frequency == "weekly" and len(parts) == 2:
            weekday = WEEKDAY_LABELS.index(parts[0].capitalize())
            time_value = datetime.strptime(parts[1], "%H:%M").strftime("%H:%M")
        elif frequency == "monthly" and len(parts) == 2:
            monthday = int(parts[0])
            if not 1 <= monthday <= 31:
                raise ValueError
            time_value = datetime.strptime(parts[1], "%H:%M").strftime("%H:%M")
        else:
            raise ValueError
    except ValueError:
        await message.answer("Формат не розпізнано. Спробуйте ще раз або /cancel.")
        return
    schedule_info = {"frequency": frequency, "send_time": time_value, "weekday": weekday, "monthday": monthday}
    baseline = scheduled_backup_occurrence(datetime.now(APP_TIMEZONE), schedule_info)
    await db.set_backup_schedule(frequency, time_value, weekday, monthday, baseline)
    if reminder_wakeup:
        reminder_wakeup.set()
    await state.clear()
    await message.answer("✅ Розклад збережено. Копії надходитимуть у цей особистий чат.", reply_markup=backup_center_markup(None))


@router.callback_query(F.data == "backup:restore-confirm")
async def backup_restore_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    global maintenance_mode
    if await access_denied(callback):
        return
    if callback.from_user.id != admin_id or callback.message.chat.type != "private":
        await callback.answer("Доступ лише власнику в особистому чаті.", show_alert=True)
        return
    data = await state.get_data()
    candidate = data.get("restore_database")
    if not isinstance(candidate, bytes):
        await callback.answer("Файл відновлення вже не доступний. Надішліть його знову.", show_alert=True)
        return
    database_path = Path(db.path)
    temp_path: Path | None = None
    safety_path: Path | None = None
    maintenance_mode = True
    try:
        while active_requests > 1:
            if maintenance_changed is None:
                await asyncio.sleep(0.1)
            else:
                maintenance_changed.clear()
                if active_requests > 1:
                    await maintenance_changed.wait()
        safety = await db.sqlite_snapshot()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=database_path.parent, suffix=".emergency.sqlite3", delete=False) as emergency:
            emergency.write(safety)
            safety_path = Path(emergency.name)
        safety_path.chmod(0o600)
        with tempfile.NamedTemporaryFile(dir=database_path.parent, suffix=".restore", delete=False) as staged:
            staged.write(candidate)
            temp_path = Path(staged.name)
        temp_path.chmod(0o600)
        await db.close()
        os.replace(temp_path, database_path)
        temp_path = None
        await db.connect()
        await db.configure_admin(admin_id)
    except Exception:
        logging.exception("Full database restore failed; restoring safety snapshot")
        if safety_path is None:
            await callback.message.answer("❌ Не вдалося створити аварійну копію. Відновлення не виконано, поточна база залишилася без змін.")
            return
        with suppress(Exception):
            await db.close()
        os.replace(safety_path, database_path)
        safety_path = None
        await db.connect()
        await db.configure_admin(admin_id)
        await callback.message.answer("❌ Не вдалося відновити файл. Поточні дані повернуто з аварійної копії.")
    else:
        await callback.message.answer("✅ Базу відновлено. Власника бота збережено.")
        if safety_path:
            safety_path.unlink(missing_ok=True)
            safety_path = None
    finally:
        maintenance_mode = False
        if temp_path:
            temp_path.unlink(missing_ok=True)
        await state.clear()
    await callback.message.answer(await backup_center_text(), reply_markup=backup_center_markup(None), parse_mode="HTML")
    await callback.answer()


@router.message(InputFlow.backup_restore_upload, F.document)
async def backup_restore_file_received(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user.id != admin_id or message.chat.type != "private":
        await state.clear()
        return
    document = message.document
    if not document or (document.file_size and document.file_size > 20 * 1024 * 1024):
        await message.answer("Файл завеликий або не розпізнаний. Максимум — 20 МБ.")
        return
    try:
        remote = await bot.get_file(document.file_id)
        stream = await bot.download_file(remote.file_path)
        raw = stream.read()
        database_bytes, details = read_backup_archive(raw, backup_encryption_key)
    except BackupError as exc:
        await message.answer(f"❌ {exc}")
        return
    except Exception:
        logging.exception("Could not download/validate database backup")
        await message.answer("❌ Не вдалося прочитати файл копії. Перевірте файл і спробуйте ще раз.")
        return
    await state.update_data(restore_database=database_bytes, restore_details=details)
    await state.set_state(InputFlow.backup_restore_confirm)
    created = details.get("created_at") or "дату не вказано (старий формат)"
    warning = "\n⚠️ Старий архів не зашифрований." if details.get("legacy_unencrypted") else ""
    counts = details.get("record_counts", {})
    await message.answer(
        f"🔎 <b>Копію перевірено</b>\n\nДата: {escape(str(created))}\nКористувачів: {details['users']} · профілів: {details['profiles']}\n"
        f"Записів: раціон {counts.get('food_log', 0)}, активність {counts.get('activity_log', 0)}, фінанси {counts.get('finance_transactions', 0)}, "
        f"цілі {counts.get('goals', 0)}, звички {counts.get('habits', 0)} / відміток {counts.get('habit_logs', 0)}, "
        f"рефлексія {counts.get('reflection_entries', 0)} + тижневих підсумків {counts.get('weekly_reflections', 0)}, тренування {counts.get('training_sessions', 0)}.\n"
        f"Таблиць: {details['tables']}\n\n"
        f"⚠️ Підтвердження повністю замінить поточні дані бази.{warning}\nПеред цим бот створить аварійну копію поточного стану.",
        reply_markup=buttons([[('♻️ Так, відновити', 'backup:restore-confirm')], [('✖️ Скасувати', 'backup:cancel')]]),
        parse_mode="HTML",
    )


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
    markup.inline_keyboard.insert(0, [InlineKeyboardButton(text="🗄 Резервні копії", callback_data="backup")])
    markup.inline_keyboard.insert(1, [InlineKeyboardButton(text="📥 Імпорт FoodDatabase.md", callback_data="adm:foods_import")])
    await callback.message.edit_text(
        "🛡 <b>Адмін-панель</b>\n\nРезервні копії, доступи, користувачі, фітнес-профілі та спільна база продуктів. Оберіть дію або користувача:",
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
    diet_mode = {"single": "один раціон", "split": "два раціони", "auto": "автоматичний раціон"}.get(profile["diet_mode"], "один раціон")
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
        actions.append([("🤖 Автоматичний раціон", f"adm:dietmode:{user_id}:auto")])
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
        if mode not in {"single", "split", "auto"}:
            await callback.message.edit_text("🍽 Оберіть режим раціонів:", reply_markup=buttons([
                [("🍽 Один раціон", f"adm:dietmode:{user_id}:single"), ("🏋️ Два раціони", f"adm:dietmode:{user_id}:split")],
                [("🤖 Автоматичний раціон", f"adm:dietmode:{user_id}:auto")],
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
        await callback.bot.send_message(user_id, "✅ Адміністратор схвалив вашу заявку. Надішліть /start, щоб налаштувати бота й перейти до меню.")
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
    if profile["diet_mode"] == "auto":
        instruction = "У ці дні бот автоматично застосовуватиме тренувальну ціль. Налаштування продуктів і прийомів їжі спільні для всіх днів."
    else:
        instruction = "У ці дні бот використовуватиме тренувальний раціон, а в інші — звичайний."
    return (
        "🗓 <b>Дні тренувань</b>\n\n"
        f"Оберіть дні тижня. {instruction}\n\n"
        f"{current}\n\nНатискайте на день, щоб увімкнути або вимкнути його."
    )


@router.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    manual = "🖐️ Ручний режим: УВІМКНЕНО" if profile["manual_mode"] else "📡 Авторежим (з експорту здоровʼя)"
    diet_mode = {"single": "один раціон", "split": "тренувальний + звичайний", "auto": "автоматичне меню"}.get(profile["diet_mode"], "один раціон")
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
    rows = [
        [('🎯 Набір', 'mode:набір'), ('⚖️ Підтримання', 'mode:підтримання'), ('🔥 Схуднення', 'mode:схуднення')],
        [('🔁 ' + manual, 'toggle_manual')],
    ]
    if profile["manual_mode"]:
        rows.append([('✏️ Зріст', 'edit:height'), ('✏️ Вік', 'edit:age'), ('✏️ Стать', 'edit:sex')])
    else:
        rows.append([('✍️ Зріст · вручну', 'edit:height'), ('✍️ Вік · вручну', 'edit:age'), ('✍️ Стать · вручну', 'edit:sex')])
    rows.append([('✏️ Цільова вага', 'edit:target_weight')])
    if profile["manual_mode"]:
        rows.append([('✏️ Ручна вага', 'edit:manual_weight'), ('✏️ Ручна активність', 'edit:manual_activity')])
    rows.extend([
        [('✏️ Білок/кг', 'edit:protein_per_kg'), ('✏️ Жири/кг', 'edit:fat_per_kg')],
        [('✏️ Профіцит/дефіцит', 'edit:surplus_deficit')],
    ])
    if profile["diet_mode"] in {"split", "auto"}:
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
    if profile["diet_mode"] not in {"split", "auto"}:
        await callback.answer("Налаштування доступне в режимах «Два раціони» та «Автоматичне меню».", show_alert=True)
        return
    await callback.message.edit_text(training_days_text(profile), reply_markup=training_days_keyboard(profile), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("dietdays:toggle:"))
async def training_days_toggle(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    profile = await account_profile(callback.from_user.id)
    if profile["diet_mode"] not in {"split", "auto"}:
        await callback.answer("Налаштування днів доступне в режимах «Два раціони» та «Автоматичне меню».", show_alert=True)
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
    if profile["diet_mode"] not in {"split", "auto"}:
        await callback.answer("Налаштування доступне в режимах «Два раціони» та «Автоматичне меню».", show_alert=True)
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
    updates = {"manual_mode": 0 if profile["manual_mode"] else 1}
    if profile["manual_mode"]:
        heights = await db.heights(profile["id"])
        if heights:
            updates["height"] = heights[-1]["height"]
    await db.update_profile(profile["id"], **updates)
    await profile_callback(callback)


@router.callback_query(F.data.startswith("edit:"))
async def edit_profile(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    if field == "sex":
        profile = await account_profile(callback.from_user.id)
        await callback.message.edit_text(
            "Оберіть стать:",
            reply_markup=buttons([
                [(('✅ ' if profile["sex"] == "чоловіча" else '') + 'Чоловіча', 'sex:чоловіча')],
                [(('✅ ' if profile["sex"] == "жіноча" else '') + 'Жіноча', 'sex:жіноча')],
                [('← До профілю', 'profile')],
            ]),
        )
        await callback.answer()
        return
    prompts = {"height": "Введіть зріст (100–250 см):", "age": "Введіть вік (10–120):", "sex": "Введіть стать: чоловіча або жіноча.", "target_weight": "Введіть цільову вагу (20–300 кг):", "manual_weight": "Введіть ручну вагу (20–300 кг):", "manual_activity": "Введіть ручну активність у ккал (0 або більше):", "protein_per_kg": "Введіть білок у г/кг (0 або більше):", "fat_per_kg": "Введіть жири у г/кг (0 або більше):", "surplus_deficit": "Введіть робочий профіцит/дефіцит у ккал (0 або більше):"}
    await state.set_state(InputFlow.profile_value)
    await state.update_data(field=field)
    await callback.message.edit_text(prompts[field], reply_markup=nav())
    await callback.answer()


@router.callback_query(F.data.startswith("sex:"))
async def select_profile_sex(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    sex = callback.data.split(":", 1)[1]
    if sex not in {"чоловіча", "жіноча"}:
        await callback.answer("Невідоме значення статі.", show_alert=True)
        return
    profile = await account_profile(callback.from_user.id)
    await db.update_profile(profile["id"], sex=sex)
    await profile_callback(callback)


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
    if profile["diet_mode"] in {"single", "auto"}:
        diet_type = DIET_TRAINING if profile["diet_mode"] == "auto" and await current_day_is_training(profile) else DIET_REST
        text, markup = await food_screen(callback.from_user.id, diet_type)
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
    elif profile["diet_mode"] == "auto":
        diet_type = DIET_TRAINING if await current_day_is_training(profile) else DIET_REST
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
            nutrient: (
                min(1.0, added[nutrient] / max(remaining[nutrient], 1))
                if remaining[nutrient] > 0
                else 0.0
            )
            for nutrient in ("protein", "carbs", "fat", "calories")
        }
        overshoot = max(0.0, added["calories"] - remaining["calories"]) / max(remaining["calories"], 1)
        macro_weights = {"protein": 0.35, "carbs": 0.25, "fat": 0.10}
        macro_penalty = 0.0
        for nutrient, weight in macro_weights.items():
            if remaining[nutrient] <= 0:
                # Do not reward adding a nutrient whose target has already been met.
                macro_penalty += weight * min(1.0, added[nutrient] / 10) * 0.5
            elif added[nutrient] > remaining[nutrient]:
                excess_ratio = (added[nutrient] - remaining[nutrient]) / max(remaining[nutrient], 5)
                macro_penalty += weight * min(1.0, excess_ratio) * 0.35
        score = (
            coverage["protein"] * 0.35
            + coverage["carbs"] * 0.25
            + coverage["fat"] * 0.10
            + coverage["calories"] * 0.30
            - overshoot * 1.5
            - macro_penalty
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
        activity_note = (
            f"\nАктивні калорії: сьогодні {current_total:.0f} ккал · "
            f"того самого дня минулого тижня {previous_total:.0f} ккал ({difference:+d})."
        )

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
        merged = any(row["product_id"] == product_id for row in rows)
        await db.add_food_log_quantity(profile["id"], log_date, diet_type, product_id, quantity)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        await food_advice(callback)
        return
    text, markup = await food_screen(callback.from_user.id, diet_type)
    result = "✅ Порцію рекомендації додано до вже записаного продукту." if merged else "✅ Рекомендацію додано до раціону."
    await callback.message.edit_text(result + "\n\n" + text, reply_markup=markup)
    await callback.answer("Порцію збільшено" if merged else "Додано")


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
        if rows:
            pace = stats["pace"]
            pace_text = (
                f"{pace:+.2f} кг/тиждень"
                if pace is not None
                else "недостатньо даних (потрібно 3+ вимірювання у 2+ тижнях)"
            )
            text = (
                "⚖️ Історія ваги\n"
                f"Поточна: {stats['current']} кг · Перша: {stats['first']} кг\n"
                f"Зміна за весь період: {stats['change']:+.1f} кг\n"
                f"Поточний темп за 30 днів: {pace_text} · До цілі: {stats['eta']}\n"
                f"Вимірювань: {stats['count']} · Остання дата: {stats['last_date']}"
            )
        else:
            text = "⚖️ Історія ваги\nДаних ще немає."
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
    for item in parsed["heights"]:
        await db.upsert_height(profile["id"], item["log_date"], item["height"], "import")
    if parsed["heights"] and not profile["manual_mode"]:
        latest_height = max(parsed["heights"], key=lambda item: item["log_date"])
        known_heights = await db.heights(profile["id"])
        if not known_heights or latest_height["log_date"] >= known_heights[-1]["log_date"]:
            await db.update_profile(profile["id"], height=latest_height["height"])
    for item in parsed["sleeps"]:
        await db.upsert_sleep(profile["id"], item)
    imported_dates = sorted({
        item["log_date"]
        for kind in ("activities", "weights", "heights", "sleeps")
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
        f"📏 Записів зросту: {len(parsed['heights'])}\n"
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
    range_start = (local_today() - timedelta(days=29)).isoformat()
    weights = [row for row in await db.weights(profile["id"]) if row["log_date"] >= range_start]
    activities = await db.activities(profile["id"], days=30)
    if not weights and not activities:
        await callback.message.edit_text("📈 За останні 30 днів даних для графіків немає.", reply_markup=nav())
        await callback.answer()
        return
    options = []
    if weights:
        options.append([("⚖️ Вага", "charts:weight")])
    if activities:
        options.extend([
            [("👣 Кроки", "charts:steps")],
            [("🔥 Активні калорії", "charts:active_calories")],
            [("📏 Дистанція", "charts:distance")],
        ])
    options.append([("← До спорту і раціону", "sport:menu")])
    await callback.message.edit_text("📈 <b>Графіки · останні 30 днів</b>\n\nОберіть показник:", reply_markup=buttons(options), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("charts:"))
async def chart_selected(callback: CallbackQuery, state: FSMContext) -> None:
    if await access_denied(callback):
        return
    profile = await account_profile(callback.from_user.id)
    field = callback.data.split(":", 1)[1]
    range_start = (local_today() - timedelta(days=29)).isoformat()
    if field == "weight":
        rows = [row for row in await db.weights(profile["id"]) if row["log_date"] >= range_start]
        if not rows:
            await callback.answer("За останні 30 днів записів ваги немає.", show_alert=True)
            return
        path = weight_chart(rows)
        title = "⚖️ Графік ваги · останні 30 днів"
        filename = "weight.png"
    else:
        chart_fields = {
            "steps": ("Кроки", "кроків"),
            "active_calories": ("Активні калорії", "ккал"),
            "distance": ("Дистанція", "км"),
        }
        if field not in chart_fields:
            await callback.answer("Невідомий показник.", show_alert=True)
            return
        rows = await db.activities(profile["id"], days=30)
        if not rows:
            await callback.answer("За останні 30 днів даних активності немає.", show_alert=True)
            return
        chart_title, unit = chart_fields[field]
        path = activity_chart(rows, field, chart_title, unit)
        title = f"🏃 Графік: {chart_title.casefold()} · останні 30 днів"
        filename = f"{field}.png"
    try:
        await send_graph(callback, state, path, filename, title)
    finally:
        path.unlink(missing_ok=True)
    await callback.answer()


async def reminder_loop(bot: Bot) -> None:
    kyiv = APP_TIMEZONE
    last_checked_minute: str | None = None
    while True:
        schedule = await db.backup_schedule()
        if await db.active_daily_summary_count() == 0 and await db.active_habit_reminder_count() == 0 and schedule["frequency"] == "off":
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
            await maybe_send_scheduled_backup(bot, now, schedule)
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


def scheduled_backup_occurrence(now: datetime, schedule) -> str | None:
    frequency = schedule["frequency"]
    if frequency == "off":
        return None
    due_time = schedule["send_time"]
    current_day = now.date()
    if now.strftime("%H:%M") < due_time:
        current_day -= timedelta(days=1)
    if frequency == "daily":
        due_day = current_day
    elif frequency == "weekly":
        due_day = current_day - timedelta(days=(current_day.weekday() - schedule["weekday"]) % 7)
    elif frequency == "monthly":
        due_day = current_day.replace(day=min(schedule["monthday"], calendar.monthrange(current_day.year, current_day.month)[1]))
        if due_day > current_day:
            previous_month = (current_day.replace(day=1) - timedelta(days=1))
            due_day = previous_month.replace(day=min(schedule["monthday"], calendar.monthrange(previous_month.year, previous_month.month)[1]))
    else:
        return None
    return f"{frequency}:{due_day.isoformat()}:{due_time}"


async def maybe_send_scheduled_backup(bot: Bot, now: datetime, schedule) -> None:
    global backup_encryption_key, active_requests
    if maintenance_mode:
        return
    active_requests += 1
    occurrence: str | None = None
    try:
        occurrence = scheduled_backup_occurrence(now, schedule)
        if occurrence is None or not backup_key_ready():
            return
        if not await db.claim_backup_attempt(occurrence, now.isoformat(timespec="seconds")):
            return
        payload = await db.sqlite_snapshot()
        archive = make_backup_archive(payload, backup_encryption_key, now)
        stamp = now.strftime("%Y%m%d_%H%M")
        await bot.send_document(
            admin_id,
            BufferedInputFile(archive, filename=f"fitness_bot_backup_{stamp}.fbackup"),
            caption="🗄 Автоматична зашифрована копія повної бази. Для відновлення потрібен BACKUP_ENCRYPTION_KEY.",
        )
        await db.record_backup_result(True, occurrence)
    except Exception as exc:
        logging.exception("Automatic full database backup failed")
        if occurrence is not None:
            await db.record_backup_result(False, occurrence, str(exc))
        with suppress(Exception):
            await bot.send_message(admin_id, "⚠️ Не вдалося надіслати автоматичну резервну копію. Бот повторить спробу пізніше.")
    finally:
        active_requests -= 1
        if maintenance_changed:
            maintenance_changed.set()


async def main() -> None:
    global db, admin_id, reminder_wakeup, backup_encryption_key, maintenance_changed
    settings = Settings.from_env()
    backup_encryption_key = settings.backup_encryption_key
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
        configure_training(db)
        bot = Bot(settings.bot_token)
        reminder_wakeup = asyncio.Event()
        maintenance_changed = asyncio.Event()
        configure_goals(db, reminder_wakeup)
        reminder_task = asyncio.create_task(reminder_loop(bot), name="daily-summary-reminders")
        dispatcher = Dispatcher()
        dispatcher.message.outer_middleware(WhitelistMiddleware())
        dispatcher.callback_query.outer_middleware(WhitelistMiddleware())
        dispatcher.include_router(router)
        dispatcher.include_router(finance_router)
        dispatcher.include_router(goals_router)
        dispatcher.include_router(reflection_router)
        dispatcher.include_router(training_router)
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
