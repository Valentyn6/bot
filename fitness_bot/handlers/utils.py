"""Утилітні функції для бота."""

import logging
from datetime import date
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from calculations import calculate_targets, food_totals, weekly_activity
from constants import DEFAULT_PROFILE_WEIGHT, DEFAULT_ACTIVITY_KCAL

logger = logging.getLogger(__name__)


def buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Побудувати клавіатуру з кнопок."""
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*(InlineKeyboardButton(text=text, callback_data=data) for text, data in row))
    return builder.as_markup()


def nav() -> InlineKeyboardMarkup:
    """Клавіатура для повернення на головну меню."""
    return buttons([[("← Назад", "home")]])


def more_nav() -> InlineKeyboardMarkup:
    """Клавіатура для повернення в меню «Ще»."""
    return buttons([[("← До меню «Ще»", "more")]])


async def account_profile(db: Any, user_id: int) -> dict[str, Any]:
    """Отримати активний профіль користувача."""
    account, _ = await db.ensure_account(user_id)
    profile = await db.active_profile(user_id)
    if not profile:
        await db.create_profile(user_id)
        profile = await db.active_profile(user_id)
    return profile


async def account_access(db: Any, user_id: int) -> dict[str, Any]:
    """Отримати інформацію про доступ користувача."""
    account, _ = await db.ensure_account(user_id)
    return account


async def is_admin(db: Any, user_id: int) -> bool:
    """Перевірити, чи користувач адміністратор."""
    account = await account_access(db, user_id)
    return bool(account["is_admin"])


async def may_manage_diet(db: Any, user_id: int) -> bool:
    """Перевірити, чи може користувач керувати своїм раціоном."""
    account = await account_access(db, user_id)
    return bool(account["is_admin"] or account["can_manage_diet"])


async def source_values(
    db: Any, profile: dict[str, Any]
) -> tuple[float, float, str]:
    """Отримати вагу та активність для розрахунків.

    Повертає:
        (вага, активність_ккал, попередження)
    """
    if profile["manual_mode"]:
        return profile["manual_weight"] or DEFAULT_PROFILE_WEIGHT, profile["manual_activity"] or DEFAULT_ACTIVITY_KCAL, ""

    weights = await db.weights(profile["id"])
    activities = await db.activities(profile["id"], 7)
    weight = weights[-1]["weight"] if weights else DEFAULT_PROFILE_WEIGHT
    activity = weekly_activity(activities)

    warning = ""
    if not weights and not activities:
        warning = f"⚠️ Немає ні ваги, ні активності — використано дефолти: {DEFAULT_PROFILE_WEIGHT} кг, {DEFAULT_ACTIVITY_KCAL} ккал."
    elif not weights:
        warning = f"⚠️ Немає даних ваги — використано дефолт {DEFAULT_PROFILE_WEIGHT} кг. Активність: {round(activity)} ккал."
    elif not activities:
        warning = f"⚠️ Немає даних активності — використано {DEFAULT_ACTIVITY_KCAL} ккал. Вага: {weight} кг."

    return weight, activity, warning


def total_activity(row: dict[str, Any]) -> float:
    """Повернути експортовані активні калорії."""
    return row.get("active_calories", 0)


def remaining_value(target: float, consumed: float) -> str:
    """Розрахувати залишок відносно цілі."""
    difference = round(target - consumed, 1)
    return (
        f"{difference:g}"
        if difference >= 0
        else f"перевищено на {abs(difference):g}"
    )


def progress_line(label: str, target: float, consumed: float, unit: str) -> str:
    """Створити лінію прогресу у вигляді текстового графіку.

    Повертає:
        Форматований рядок з прогрес-бар'ом та числовими показниками
    """
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
        f"   з'їдено {consumed:g} / {target:g} {unit} · залишилось {remaining} {unit}"
    )


async def home_text(db: Any, user_id: int) -> str:
    """Сгенерувати текст головного меню."""
    profile = await account_profile(db, user_id)
    weight, activity, warning = await source_values(db, profile)
    targets = calculate_targets(profile, weight, activity)

    return (
        f"🏋️ Фітнес-трекер · {profile['name']}\n\n"
        f"🎯 Ціль на день: {targets['total']} ккал | Білки {targets['protein']}г | Жири {targets['fat']}г | Вуглеводи {targets['carbs']}г\n"
        f"⚖️ Вага: {weight:g} кг · ІМТ: {targets['bmi']}\n"
        f"Режим: {profile['goal_mode']} · {'🖐️ ручний' if profile['manual_mode'] else '📡 авто'}\n"
        f"{warning}"
    )


async def home_markup(db: Any, user_id: int) -> InlineKeyboardMarkup:
    """Клавіатура для головного меню."""
    rows = [
        [("🍽 Раціон", "food"), ("⚖️ Вага", "weight")],
        [("📈 Графіки", "charts"), ("🩺 Здоровʼя", "health")],
        [("👤 Профіль", "profile"), ("⋯ Ще", "more")],
    ]
    return buttons(rows)


async def more_markup(db: Any, user_id: int) -> InlineKeyboardMarkup:
    """Клавіатура для розширеного меню."""
    rows = [
        [("📥 Імпорт", "import"), ("🍎 Продукти", "products")],
        [("👥 Профілі", "profiles"), ("💾 Бекап", "backup")],
        [("ℹ️ Допомога", "help")],
    ]
    if await is_admin(db, user_id):
        rows.append([("🛡 Адмін-панель", "admin")])
    rows.append([("← Назад", "home")])
    return buttons(rows)
