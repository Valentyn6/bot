from __future__ import annotations

import io
import json
import logging
import sqlite3
from datetime import date, timedelta
from html import escape
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Document, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .calculations import calculate_targets, food_totals, is_training_day, normalize_date, parse_number, weekly_activity, weekly_activity_by_day_type, weight_stats
from .charts import activity_chart, weight_chart
from .config import Settings
from .db import Database
from .import_health import ImportErrorMessage, parse_food_database, parse_health_export
from .logger_config import setup_logging


router = Router()
db: Database
admin_id = 0
DIET_TRAINING = "training"
DIET_REST = "rest"


class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        account, _ = await db.ensure_account(user_id)
        text_parts = (event.text or "").split(maxsplit=1) if isinstance(event, Message) else []
        command = text_parts[0] if text_parts else ""
        is_start = command == "/start" or command.startswith("/start@")
        if account["access_status"] == "approved" or (account["is_admin"] and account["access_status"] != "banned") or is_start:
            return await handler(event, data)
        text = "⛔ Ваш доступ заблоковано адміністратором." if account["access_status"] == "banned" else "⏳ Ви не додані до білого списку. Очікуйте підтвердження адміністратора."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return None


router.message.outer_middleware(WhitelistMiddleware())
router.callback_query.outer_middleware(WhitelistMiddleware())


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


def buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        builder.row(*(InlineKeyboardButton(text=text, callback_data=data) for text, data in row))
    return builder.as_markup()


def nav() -> InlineKeyboardMarkup:
    return buttons([[('← Назад', 'home')]])


def more_nav() -> InlineKeyboardMarkup:
    return buttons([[('← До меню «Ще»', 'more')]])


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
        diet = "раціон дозволено" if account["can_manage_diet"] else "раціон лише коуч"
        rows.append([(f"{status} {account['telegram_id']} · {diet}", f"adm:user:{account['telegram_id']}")])
    rows += [[("← До меню «Ще»", "more")]]
    return buttons(rows)


async def source_values(profile, diet_type: str | None = None):
    if profile["manual_mode"]:
        return profile["manual_weight"] or 71, profile["manual_activity"] or 0, ""
    weights = await db.weights(profile["id"])
    activities = await db.activities(profile["id"], 7)
    weight = weights[-1]["weight"] if weights else 71
    today = date.today().isoformat()
    today_activity = next((row for row in activities if row["log_date"] == today), None)
    training = is_training_day(today_activity) if today_activity else False
    averages = weekly_activity_by_day_type(activities)
    activity = averages[diet_type or ("training" if training else "rest")]
    warning = ""
    if not weights and not activities:
        warning = "⚠️ Немає ні ваги, ні активності — використано дефолти: 71 кг, 0 ккал."
    elif not weights:
        warning = f"⚠️ Немає даних ваги — використано дефолт 71 кг. Активність: {round(activity)} ккал."
    elif not activities:
        warning = f"⚠️ Немає даних активності — використано 0 ккал. Вага: {weight} кг."
    return weight, activity, warning


async def current_day_is_training(profile) -> bool:
    if profile["manual_mode"]:
        return False
    activities = await db.activities(profile["id"], 7)
    today = next((row for row in activities if row["log_date"] == date.today().isoformat()), None)
    return is_training_day(today) if today else False


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


async def home_markup(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [('🍽 Раціон', 'food'), ('⚖️ Вага', 'weight')],
        [('📈 Графіки', 'charts'), ('🩺 Здоровʼя', 'health')],
        [('👤 Профіль', 'profile'), ('⋯ Ще', 'more')],
    ]
    return buttons(rows)


async def more_markup(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [('📥 Імпорт', 'import'), ('🍎 Продукти', 'products')],
        [('👥 Профілі', 'profiles'), ('💾 Бекап', 'backup')],
        [('ℹ️ Допомога', 'help')],
    ]
    if await is_admin(user_id):
        rows.append([('🛡 Адмін-панель', 'admin')])
    rows.append([('← Назад', 'home')])
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
    weight, activity, _ = await source_values(profile, diet_type)
    targets = calculate_targets(profile, weight, activity)
    day_type = "тренувальний" if await current_day_is_training(profile) else "звичайний"
    rows = await db.food_day(profile["id"], date.today().isoformat(), diet_type)
    totals = food_totals(rows)
    text = (
        f"{diet_title(diet_type)}\nРаціон за {date.today().isoformat()} · {day_type} день\n\n"
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
        diet_buttons.append([('💡 Порада на сьогодні', f'food_advice:{diet_type}')])
        diet_buttons.append([('↩️ Видалити останній', f'food_delete:{diet_type}'), ('🔁 Як учора', f'food_yesterday:{diet_type}')])
    else:
        text += "\n\nℹ️ Раціон веде коуч. Самостійне редагування вимкнено."
    diet_buttons.append([('🔁 Змінити раціон', 'food')])
    diet_buttons.append([('← Назад', 'home')])
    return text, buttons(diet_buttons)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    global admin_id
    await state.clear()
    account, created = await db.ensure_account(message.from_user.id)
    if not admin_id:
        admin_id = await db.configure_admin(message.from_user.id)
        account = await db.account(message.from_user.id)
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
    await message.answer(await home_text(message.from_user.id), reply_markup=await home_markup(message.from_user.id), parse_mode="HTML")


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer("/start — головний екран\n/backup — JSON-бекап\n\nУсі дані ізольовані в межах вашого Telegram-акаунта. Дати вводяться у форматі YYYY-MM-DD.")


@router.message(Command("backup"))
async def backup_command(message: Message) -> None:
    if await access_denied(message):
        return
    payload = json.dumps(await db.export_all(message.from_user.id), ensure_ascii=False, indent=2).encode()
    await message.answer_document(BufferedInputFile(payload, filename="fitness_backup.json"))


@router.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if await access_denied(callback):
        return
    await callback.message.edit_text(await home_text(callback.from_user.id), reply_markup=await home_markup(callback.from_user.id), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "more")
async def more_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await callback.message.edit_text("⋯ <b>Інструменти</b>\n\nІмпорт, довідники, профілі та резервні копії.", reply_markup=await more_markup(callback.from_user.id), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "ℹ️ <b>Як користуватися</b>\n\n"
        "🍽 <b>Раціон</b> — додавання продуктів і контроль БЖВ.\n"
        "⚖️ <b>Вага</b> — щоденні вимірювання та темп змін.\n"
        "🩺 <b>Здоровʼя</b> — активність і сон з Health Export Kit.\n"
        "📥 <b>Імпорт</b> — надішліть файл документом.\n"
        "💾 <b>Бекап</b> — JSON-копія ваших даних.\n\n"
        "Одиниці «г/мл» рахуються на 100 одиниць, «шт» — на одну штуку.",
        reply_markup=more_nav(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "backup")
async def backup_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    payload = json.dumps(await db.export_all(callback.from_user.id), ensure_ascii=False, indent=2).encode()
    await callback.message.answer_document(BufferedInputFile(payload, filename="fitness_backup.json"))
    await callback.answer("Бекап підготовлено")


@router.callback_query(F.data == "profiles")
async def profiles_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    profiles = await db.profiles(callback.from_user.id)
    rows = [[(f"✅ {p['name']}" if p['id'] == (await db.active_profile(callback.from_user.id))["id"] else p['name'], f"switch:{p['id']}")] for p in profiles]
    rows += [[('➕ Додати профіль', 'profile_add')], [('← До меню «Ще»', 'more')]]
    await callback.message.edit_text("👥 Профілі\nОберіть активний профіль:", reply_markup=buttons(rows))
    await callback.answer()


@router.callback_query(F.data == "admin")
async def admin_callback(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Цей розділ доступний лише адміністратору.", show_alert=True)
        return
    accounts = await db.accounts()
    markup = admin_markup(accounts)
    markup.inline_keyboard.insert(0, [InlineKeyboardButton(text="👤 Мій профіль", callback_data="adm:own")])
    markup.inline_keyboard.insert(1, [InlineKeyboardButton(text="📥 Імпорт FoodDatabase.md", callback_data="adm:foods_import")])
    await callback.message.edit_text("🛡 <b>Адмін-панель</b>\n\nКористувачі, доступи та спільна база продуктів. Оберіть користувача для дії:", reply_markup=markup, parse_mode="HTML")
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
    profile = next((item for item in profiles if item["id"] == account["active_profile_id"]), profiles[0])
    status = {"approved": "схвалений", "pending": "очікує", "rejected": "відхилений", "banned": "заблокований"}.get(account["access_status"], account["access_status"])
    text = f"👤 <b>Користувач {user_id}</b>\n\nСтатус: {status}\nПрофіль: {profile['name']}\nРежим: <b>{profile['goal_mode']}</b>\nСамостійний раціон: {'дозволено' if account['can_manage_diet'] else 'заборонено'}"
    actions = []
    if account["access_status"] == "approved":
        actions.append([("🎯 Набір", f"adm:mode:{user_id}:набір"), ("⚖️ Підтримання", f"adm:mode:{user_id}:підтримання")])
        actions.append([("🔥 Схуднення", f"adm:mode:{user_id}:схуднення")])
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
    await callback.message.edit_text(text, reply_markup=buttons(actions))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:"))
async def admin_action(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Недостатньо прав.", show_alert=True)
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
        account = await db.account(user_id)
        profile = next((item for item in profiles if item["id"] == account["active_profile_id"]), profiles[0])
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
        await db.set_managed_profile(callback.from_user.id, profiles[0]["id"])
        await callback.message.edit_text(await home_text(callback.from_user.id), reply_markup=await home_markup(callback.from_user.id), parse_mode="HTML")
        await callback.answer("Профіль підопічного відкрито")
        return
    await callback.message.edit_text("🛡 Зміни збережено.", reply_markup=buttons([[('← До адмін-панелі', 'admin')]]))
    await callback.answer("Збережено")


@router.callback_query(F.data.startswith("switch:"))
async def switch_profile(callback: CallbackQuery) -> None:
    await db.set_active_profile(callback.from_user.id, int(callback.data.split(":")[1]))
    await callback.message.edit_text(await home_text(callback.from_user.id), reply_markup=await home_markup(callback.from_user.id), parse_mode="HTML")
    await callback.answer("Профіль перемкнено")


@router.callback_query(F.data == "profile_add")
async def profile_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InputFlow.profile_value)
    await state.update_data(field="name")
    await callback.message.edit_text("Введіть імʼя нового профілю:", reply_markup=nav())
    await callback.answer()


@router.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    manual = "🖐️ Ручний режим: УВІМКНЕНО" if profile["manual_mode"] else "📡 Авторежим (з експорту здоровʼя)"
    text = (
        f"👤 <b>Профіль · {profile['name']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📏 Зріст: {profile['height']:g} см  ·  Вік: {profile['age']}  ·  Стать: {profile['sex']}\n"
        f"🎯 Ціль: <b>{profile['goal_mode']}</b>  ·  корекція {profile['surplus_deficit']:g} ккал\n"
        f"⚖️ Цільова вага: {profile['target_weight'] or 'не задана'} кг\n"
        f"🥩 Білок: {profile['protein_per_kg']:g} г/кг  ·  🥑 Жири: {profile['fat_per_kg']:g} г/кг\n"
        f"📊 Джерело даних: {'ручне' if profile['manual_mode'] else 'Health Export'}"
    )
    markup = buttons([[('🎯 Набір', 'mode:набір'), ('⚖️ Підтримання', 'mode:підтримання'), ('🔥 Схуднення', 'mode:схуднення')], [('🔁 ' + manual, 'toggle_manual')], [('✏️ Зріст', 'edit:height'), ('✏️ Вік', 'edit:age'), ('✏️ Стать', 'edit:sex')], [('✏️ Цільова вага', 'edit:target_weight')], [('✏️ Ручна вага', 'edit:manual_weight'), ('✏️ Ручна активність', 'edit:manual_activity')], [('✏️ Білок/кг', 'edit:protein_per_kg'), ('✏️ Жири/кг', 'edit:fat_per_kg')], [('✏️ Профіцит/дефіцит', 'edit:surplus_deficit')], [('← Назад', 'home')]])
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("mode:"))
async def mode_callback(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    mode = callback.data.split(":", 1)[1]
    source = {"набір": "surplus_gain", "підтримання": "surplus_maintenance", "схуднення": "deficit_loss"}[mode]
    await db.update_profile(profile["id"], goal_mode=mode, surplus_deficit=profile[source])
    await callback.message.edit_text(await home_text(callback.from_user.id), reply_markup=await home_markup(callback.from_user.id), parse_mode="HTML")
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
            await db.create_profile(message.from_user.id, value[:40] or "Профіль")
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
        await message.answer(await home_text(message.from_user.id), reply_markup=await home_markup(message.from_user.id), parse_mode="HTML")
    except (ValueError, KeyError):
        await message.answer("Некоректне значення. Перевірте число та діапазон.")


@router.callback_query(F.data == "food")
async def food_callback(callback: CallbackQuery) -> None:
    if await access_denied(callback):
        return
    await callback.message.edit_text(
        "🍽 <b>Оберіть раціон</b>",
        reply_markup=buttons([
            [('🏋️ Тренувальний раціон', f'food:{DIET_TRAINING}')],
            [('🛋 Звичайний раціон', f'food:{DIET_REST}')],
            [('← Назад', 'home')],
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
    today = date.today().isoformat()
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
    log_date = date.today().isoformat()
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
        await db.add_food_log(profile["id"], date.today().isoformat(), data["meal"], data["product_id"], quantity, diet_type)
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
    count = await db.copy_yesterday(profile["id"], date.today().isoformat(), diet_type)
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
    await callback.message.edit_text(text, reply_markup=buttons([[('➕ Записати вагу', 'weight_add')], [('← Назад', 'home')]]))
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
        await db.upsert_weight(profile["id"], date.today().isoformat(), weight)
        await state.clear()
        await message.answer("✅ Вагу збережено.", reply_markup=await home_markup(message.from_user.id))
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
        await message.answer("✅ Продукт додано.", reply_markup=await home_markup(message.from_user.id))
    except sqlite3.IntegrityError:
        await message.answer("Продукт із такою назвою вже існує. Дублікати не створюються.")


@router.callback_query(F.data == "health")
async def health_callback(callback: CallbackQuery) -> None:
    profile = await account_profile(callback.from_user.id)
    if profile["manual_mode"]:
        text = f"🩺 Дані здоровʼя\nРучний режим: вага {profile['manual_weight'] or 71} кг, активність {profile['manual_activity'] or 0} ккал."
    else:
        activities = await db.activities(profile["id"], 7)
        weights = await db.weights(profile["id"])
        averages = weekly_activity_by_day_type(activities)
        text = "🩺 Дані здоровʼя\n" + (f"⚖️ Остання вага: {weights[-1]['weight']} кг\n🔥 Цільова активність у звичайний день: {averages['rest']:.0f} ккал/день\n🏋️ Цільова активність у тренувальний день: {averages['training']:.0f} ккал/день\n👣 Середні кроки: {sum(x['steps'] for x in activities) / len(activities):.0f}" if activities else "Дані ще не імпортовані.")
    await callback.message.edit_text(text, reply_markup=nav())
    await callback.answer()


@router.callback_query(F.data == "import")
async def import_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text("📥 Надішліть JSON-файл документом. Підтримується Health Export Kit. Нерозпізнані формати будуть відхилені.", reply_markup=more_nav())
    await callback.answer()


@router.message(F.document)
async def document_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    if await access_denied(message):
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
        await message.answer(f"✅ Імпортовано продуктів: {len(products)}. Існуючі записи оновлено.", reply_markup=await home_markup(message.from_user.id))
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
            await message.answer(f"✅ Відновлено продуктів: {len(products)}. Існуючі записи оновлено.", reply_markup=await home_markup(message.from_user.id))
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
    today = next((item for item in parsed["activities"] if item["log_date"] == date.today().isoformat()), None)
    if today:
        await state.set_state(InputFlow.import_workout)
        await message.answer(f"✅ Імпортовано дані. Сьогодні кроків: {today['steps']:.0f}, активні калорії: {today['active_calories']:.0f}.\n⚠️ Введіть спалені калорії тренування за сьогодні або 0, якщо тренування не було:")
    else:
        await message.answer("✅ Імпорт завершено. Дані за перекриті дні обʼєднано через максимум.", reply_markup=await home_markup(message.from_user.id))


@router.message(InputFlow.import_workout)
async def import_workout(message: Message, state: FSMContext) -> None:
    try:
        kcal = parse_number(message.text)
        if kcal < 0:
            raise ValueError
        profile = await account_profile(message.from_user.id)
        await db.set_workout_calories(profile["id"], date.today().isoformat(), kcal)
        await state.clear()
        await message.answer("✅ Тренування враховано в цілі на сьогодні.", reply_markup=await home_markup(message.from_user.id))
    except ValueError:
        await message.answer("Введіть невідʼємне число або 0.")


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
    if weights:
        path = weight_chart(weights)
        await callback.message.answer_document(BufferedInputFile(path.read_bytes(), filename="weight.png"), caption="⚖️ Графік ваги")
        path.unlink(missing_ok=True)
    if activities:
        for field, title, unit in (("steps", "Кроки", "кроків"), ("active_calories", "Звичайні активні калорії", "ккал"), ("workout_calories", "Калорії тренувань", "ккал"), ("total_calories", "Загальні активні калорії", "ккал"), ("distance", "Дистанція", "км")):
            path = activity_chart(activities, field, title, unit)
            await callback.message.answer_document(BufferedInputFile(path.read_bytes(), filename=f"{field}.png"), caption=f"🏃 {title}")
            path.unlink(missing_ok=True)
    await callback.answer()


async def main() -> None:
    global db, admin_id
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    db = Database(settings.database_path)
    bot: Bot | None = None
    try:
        await db.connect()
        admin_id = await db.configure_admin(settings.admin_telegram_id)
        bot = Bot(settings.bot_token)
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        await dispatcher.start_polling(bot)
    except TelegramUnauthorizedError as exc:
        logger.critical("Telegram відхилив BOT_TOKEN. Перевірте .env і токен у @BotFather.")
        raise RuntimeError("Telegram не авторизував бота: перевірте BOT_TOKEN у .env") from exc
    except TelegramNetworkError as exc:
        logger.critical("Не вдалося підключитися до Telegram API: %s", exc)
        raise RuntimeError("Немає з'єднання з Telegram API. Перевірте інтернет або DNS.") from exc
    finally:
        await db.close()
        if bot is not None:
            await bot.session.close()
