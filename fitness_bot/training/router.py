from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..calculations import parse_number
from ..charts import exercise_progress_chart
from ..db import Database
from ..time_utils import local_today
from .states import TrainingFlow

router = Router(name="training")
_db: Database | None = None


class TrainingAccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None and not await allowed(user.id):
            account = await db().account(user.id)
            text = "⛔ Доступ заблоковано." if account and account["access_status"] == "banned" else "⏳ Доступ ще не надано."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(text)
            return None
        return await handler(event, data)

BUILT_INS = (
    ("Жим штанги лежачи", "Груди"), ("Жим гантелей лежачи", "Груди"),
    ("Жим гантелей на похилій лаві", "Груди"), ("Батерфляй", "Груди"),
    ("Розгинання рук в блоці", "Трицепс"), ("Французький жим", "Трицепс"),
    ("Розгинання гантелі з-за голови", "Трицепс"),
    ("Підтягування", "Спина"), ("Тяга штанги", "Спина"),
    ("Верхній блок", "Спина"), ("Горизонтальний блок", "Спина"),
    ("Підйом W-подібного грифа на біцепс", "Біцепс"), ("Підйом гантель з супінацією", "Біцепс"),
    ("Підйом штанги на біцепс хватом зверху", "Біцепс"), ("Гіперекстензія без ваги", "Спина"),
    ("Жим ногами", "Ноги"), ("Розгинання ніг", "Ноги"),
    ("Згинання ніг", "Ноги"), ("Випади з гантелями", "Ноги"), ("Підйоми на литки", "Ноги"),
    ("Жим гантелей вертикально", "Плечі"), ("Протяжка", "Плечі"),
    ("Підйом в сторони", "Плечі"), ("Розведення гантелей у нахилі", "Плечі"),
    ("Задня дельта батерфлай", "Плечі"), ("Шраги на трапецію", "Плечі"),
    ("Планка", "Кор"), ("Скручування", "Кор"),
)


def configure(db: Database) -> None:
    global _db
    _db = db
    router.message.outer_middleware(TrainingAccessMiddleware())
    router.callback_query.outer_middleware(TrainingAccessMiddleware())


def db() -> Database:
    if _db is None:
        raise RuntimeError("Training router is not configured")
    return _db


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=data) for label, data in row
    ] for row in rows])


async def profile_id(user_id: int) -> int:
    profile = await db().active_profile(user_id)
    if profile is None:
        profile_id = await db().create_profile(user_id)
        return profile_id
    return int(profile["id"])


async def allowed(user_id: int) -> bool:
    account = await db().account(user_id)
    return bool(account and (account["access_status"] == "approved" or account["is_admin"]))


async def seed_catalog() -> None:
    conn = db()._db()
    for old, canonical in (
        ("Підйом гантелі з супінацією", "Підйом гантель з супінацією"),
        ("Розгинання рук у блоці", "Розгинання рук в блоці"),
    ):
        await conn.execute(
            "UPDATE training_exercises SET name=? WHERE owner_telegram_id IS NULL AND name=? COLLATE NOCASE "
            "AND NOT EXISTS (SELECT 1 FROM training_exercises e WHERE e.owner_telegram_id IS NULL AND e.name=? COLLATE NOCASE)",
            (canonical, old, canonical),
        )
    for name, group in BUILT_INS:
        await conn.execute(
            "INSERT INTO training_exercises(owner_telegram_id,name,muscle_group,created_at) "
            "SELECT NULL,?,?,? WHERE NOT EXISTS "
            "(SELECT 1 FROM training_exercises WHERE owner_telegram_id IS NULL AND name=? COLLATE NOCASE)",
            (name, group, datetime.now().isoformat(timespec="seconds"), name),
        )
        await conn.execute(
            "UPDATE training_exercises SET muscle_group=? WHERE owner_telegram_id IS NULL AND name=? COLLATE NOCASE",
            (group, name),
        )
    await conn.commit()


async def program_exists(pid: int) -> bool:
    return bool(await (await db()._db().execute("SELECT 1 FROM training_programs WHERE profile_id=?", (pid,))).fetchone())


async def render_program(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    await seed_catalog()
    pid = await profile_id(user_id)
    program = await (await db()._db().execute("SELECT * FROM training_programs WHERE profile_id=?", (pid,))).fetchone()
    if not program:
        return (
            "🏋️ <b>Тренувальна програма</b>\n\nПрограми ще немає. Створіть її, додавши тренувальні дні та вправи.\n\n"
            "Поточна вага зберігається біля вправи. Щоб побачити прогрес на графіку, позначте тренування виконаним.",
            kb([[('➕ Створити програму', 'training:new-day')], [('📚 База вправ', 'training:catalog'), ('➕ Власна вправа', 'training:custom-new')], [('📚 Історія прогресу', 'training:history')], [('← До спорту і раціону', 'sport:menu')]]),
        )
    days = await (await db()._db().execute(
        "SELECT * FROM training_days WHERE program_id=? ORDER BY order_index,id", (program["id"],)
    )).fetchall()
    lines = [f"🏋️ <b>{escape(program['name'])}</b>", ""]
    rows: list[list[tuple[str, str]]] = []
    for day in days:
        lines.append(f"<b>{escape(day['title'])}</b>")
        exercises = await (await db()._db().execute(
            "SELECT * FROM training_program_exercises WHERE day_id=? ORDER BY order_index,id", (day["id"],)
        )).fetchall()
        if not exercises:
            lines.append("  Ще немає вправ")
        previous_group = None
        for exercise in exercises:
            group = exercise["muscle_group"]
            if group != previous_group:
                lines.append(f"  <b>{escape(group)}</b>")
                previous_group = group
            current_weight = (
                f"{exercise['current_weight']:g} кг"
                if exercise["current_weight"] is not None
                else "вагу не вказано"
            )
            lines.append(f"  • {escape(exercise['name_snapshot'])} — <b>{current_weight}</b>")
            rows.append([
                (f"📈 {exercise['name_snapshot'][:22]}", f"training:history-chart-by-name:{exercise['id']}"),
                (f"⚖️ {exercise['current_weight']:g} кг" if exercise["current_weight"] is not None else "⚖️ Вага —", f"training:weight:{exercise['id']}"),
            ])
        rows.append([
            (f"✅ Виконано: {day['title'][:20]}", f"training:done:{day['id']}"),
            ("➕ Вправу", f"training:add-ex:{day['id']}"),
        ])
        rows.append([
            ("✏️ Назва дня", f"training:edit-day:{day['id']}"),
            ("🗑 Вправу", f"training:manage-ex:{day['id']}"),
            ("🗑 День", f"training:remove-day:{day['id']}"),
        ])
        lines.append("")
    if not days:
        lines.append("Додайте перший тренувальний день.")
    rows.extend([
        [("✏️ Назва програми", "training:edit-title")],
        [("📚 Історія прогресу", "training:history")],
        [("📚 База вправ", "training:catalog"), ("➕ Власна вправа", "training:custom-new")],
        [("➕ Додати тренувальний день", "training:new-day")],
        [("🗑 Видалити програму", "training:delete")],
        [("← До спорту і раціону", "sport:menu")],
    ])
    return "\n".join(lines).strip(), kb(rows)


async def show(callback: CallbackQuery) -> None:
    text, markup = await render_program(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


@router.callback_query(F.data == "training:menu")
async def menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback.from_user.id):
        await callback.answer("Доступ ще не надано.", show_alert=True)
        return
    await state.clear()
    await show(callback)
    await callback.answer()


@router.callback_query(F.data == "training:new-day")
async def new_day(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback.from_user.id):
        await callback.answer("Доступ ще не надано.", show_alert=True)
        return
    await state.set_state(TrainingFlow.day_title)
    await callback.message.edit_text("Введіть назву тренувального дня, наприклад: <b>Понеділок — груди й трицепс</b>.", parse_mode="HTML", reply_markup=kb([[('✖️ Скасувати', 'training:cancel')]]))
    await callback.answer()


@router.callback_query(F.data == "training:edit-title")
async def edit_program_title(callback: CallbackQuery, state: FSMContext) -> None:
    if not await program_exists(await profile_id(callback.from_user.id)):
        await callback.answer("Спочатку створіть програму.", show_alert=True)
        return
    await state.set_state(TrainingFlow.program_title)
    await callback.message.edit_text("Введіть назву програми (до 60 символів):", reply_markup=kb([[('✖️ Скасувати', 'training:cancel')]]))
    await callback.answer()


@router.message(TrainingFlow.program_title)
async def save_program_title(message: Message, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())
    if not title or len(title) > 60:
        await message.answer("Назва має містити від 1 до 60 символів.")
        return
    await db()._db().execute("UPDATE training_programs SET name=?,updated_at=? WHERE profile_id=?", (title, datetime.now().isoformat(timespec="seconds"), await profile_id(message.from_user.id)))
    await db()._db().commit()
    await state.clear()
    text, markup = await render_program(message.from_user.id)
    await message.answer("✅ Назву змінено.\n\n" + text, reply_markup=markup, parse_mode="HTML")


@router.message(TrainingFlow.day_title)
async def save_day(message: Message, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())
    if not title or len(title) > 70:
        await message.answer("Назва має містити від 1 до 70 символів. Спробуйте ще раз.")
        return
    pid = await profile_id(message.from_user.id)
    conn = db()._db()
    now = datetime.now().isoformat(timespec="seconds")
    await conn.execute("INSERT OR IGNORE INTO training_programs(profile_id,name,created_at,updated_at) VALUES (?,?,?,?)", (pid, "Моя програма", now, now))
    program = await (await conn.execute("SELECT id FROM training_programs WHERE profile_id=?", (pid,))).fetchone()
    order = await (await conn.execute("SELECT COALESCE(MAX(order_index),-1)+1 AS n FROM training_days WHERE program_id=?", (program["id"],))).fetchone()
    await conn.execute("INSERT INTO training_days(program_id,title,order_index) VALUES (?,?,?)", (program["id"], title, order["n"]))
    await conn.execute("UPDATE training_programs SET updated_at=? WHERE id=?", (now, program["id"]))
    await conn.commit()
    await state.clear()
    await message.answer("✅ День додано.")
    text, markup = await render_program(message.from_user.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("training:edit-day:"))
async def edit_day_start(callback: CallbackQuery, state: FSMContext) -> None:
    day_id = int(callback.data.rsplit(":", 1)[1])
    pid = await profile_id(callback.from_user.id)
    day = await (await db()._db().execute("SELECT d.title FROM training_days d JOIN training_programs p ON p.id=d.program_id WHERE d.id=? AND p.profile_id=?", (day_id, pid))).fetchone()
    if not day:
        await callback.answer("День не знайдено.", show_alert=True)
        return
    await state.set_state(TrainingFlow.day_edit)
    await state.update_data(day_id=day_id)
    await callback.message.edit_text(f"Нова назва для <b>{escape(day['title'])}</b> (до 70 символів):", parse_mode="HTML", reply_markup=kb([[('✖️ Скасувати', 'training:cancel')]]))
    await callback.answer()


@router.message(TrainingFlow.day_edit)
async def save_day_title(message: Message, state: FSMContext) -> None:
    title = " ".join((message.text or "").split())
    if not title or len(title) > 70:
        await message.answer("Назва має містити від 1 до 70 символів.")
        return
    data = await state.get_data()
    pid = await profile_id(message.from_user.id)
    await db()._db().execute("UPDATE training_days SET title=? WHERE id=? AND program_id IN (SELECT id FROM training_programs WHERE profile_id=?)", (title, data["day_id"], pid))
    await db()._db().commit()
    await state.clear()
    text, markup = await render_program(message.from_user.id)
    await message.answer("✅ Назву дня змінено.\n\n" + text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("training:add-ex:"))
async def choose_exercise(callback: CallbackQuery) -> None:
    if not await allowed(callback.from_user.id):
        await callback.answer("Доступ ще не надано.", show_alert=True)
        return
    day_id = int(callback.data.rsplit(":", 1)[1])
    pid = await profile_id(callback.from_user.id)
    day = await (await db()._db().execute("SELECT d.id FROM training_days d JOIN training_programs p ON p.id=d.program_id WHERE d.id=? AND p.profile_id=?", (day_id, pid))).fetchone()
    if not day:
        await callback.answer("Цей день уже недоступний.", show_alert=True)
        return
    exercises = await (await db()._db().execute("SELECT * FROM training_exercises WHERE owner_telegram_id IS NULL OR owner_telegram_id=? ORDER BY muscle_group,name", (callback.from_user.id,))).fetchall()
    rows = [[(f"{e['muscle_group']} · {e['name'][:30]}", f"training:pick:{day_id}:{e['id']}")] for e in exercises]
    rows += [[("➕ Додати власну вправу", f"training:custom:{day_id}")], [("← Назад до програми", "training:menu")]]
    await callback.message.edit_text("Оберіть вправу з бази або додайте власну:", reply_markup=kb(rows))
    await callback.answer()


@router.callback_query(F.data == "training:catalog")
async def exercise_catalog(callback: CallbackQuery) -> None:
    await seed_catalog()
    exercises = await (await db()._db().execute(
        "SELECT muscle_group,name FROM training_exercises WHERE owner_telegram_id IS NULL OR owner_telegram_id=? ORDER BY muscle_group,name",
        (callback.from_user.id,),
    )).fetchall()
    sections: list[str] = ["📚 <b>База вправ</b>", ""]
    current_group = None
    for exercise in exercises:
        if exercise["muscle_group"] != current_group:
            current_group = exercise["muscle_group"]
            sections.append(f"<b>{escape(current_group)}</b>")
        sections.append(f"• {escape(exercise['name'])}")
    sections.extend(["", "Власні вправи доступні лише у вашому каталозі."])
    await callback.message.edit_text(
        "\n".join(sections), parse_mode="HTML",
        reply_markup=kb([[('➕ Додати власну вправу', 'training:custom-new')], [('← До програми', 'training:menu')]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("training:pick:"))
async def pick_exercise(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, day_raw, exercise_raw = callback.data.split(":")
    day_id, exercise_id = int(day_raw), int(exercise_raw)
    pid = await profile_id(callback.from_user.id)
    conn = db()._db()
    row = await (await conn.execute("SELECT e.* FROM training_exercises e WHERE e.id=? AND (e.owner_telegram_id IS NULL OR e.owner_telegram_id=?) AND EXISTS (SELECT 1 FROM training_days d JOIN training_programs p ON p.id=d.program_id WHERE d.id=? AND p.profile_id=?)", (exercise_id, callback.from_user.id, day_id, pid))).fetchone()
    if not row:
        await callback.answer("Вправу або день не знайдено.", show_alert=True)
        return
    await state.set_state(TrainingFlow.exercise_data)
    await state.update_data(day_id=day_id, exercise_id=exercise_id, exercise_name=row["name"], muscle_group=row["muscle_group"])
    await callback.message.edit_text(
        f"Вкажіть поточну вагу для <b>{escape(row['name'])}</b> у кілограмах.\n"
        "Наприклад: <code>62.5</code>. Якщо вправу виконуєте без додаткової ваги, надішліть тире.",
        parse_mode="HTML", reply_markup=kb([[('✖️ Скасувати', 'training:cancel')]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("training:custom:"))
async def custom_exercise_start(callback: CallbackQuery, state: FSMContext) -> None:
    day_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(TrainingFlow.custom_exercise)
    await state.update_data(day_id=day_id)
    await callback.message.edit_text("Введіть власну вправу у форматі <code>Назва | Група м’язів</code>. Наприклад: <code>Тяга канату до обличчя | Плечі</code>.", parse_mode="HTML", reply_markup=kb([[('✖️ Скасувати', 'training:cancel')]]))
    await callback.answer()


@router.callback_query(F.data == "training:custom-new")
async def custom_exercise_standalone(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TrainingFlow.custom_exercise)
    await state.set_data({})
    await callback.message.edit_text(
        "Введіть назву власної вправи та групу м’язів через |. Наприклад: <code>Тяга канату до обличчя | Плечі</code>.",
        parse_mode="HTML", reply_markup=kb([[('✖️ Скасувати', 'training:cancel')]]),
    )
    await callback.answer()


@router.message(TrainingFlow.custom_exercise)
async def save_custom_exercise(message: Message, state: FSMContext) -> None:
    parts = [part.strip() for part in (message.text or "").split("|", 1)]
    name = " ".join(parts[0].split()) if parts else ""
    group = " ".join(parts[1].split()) if len(parts) > 1 else "Інше"
    if not name or len(name) > 70 or len(group) > 35:
        await message.answer("Перевірте назву (до 70 символів) і групу м’язів (до 35 символів).")
        return
    conn = db()._db()
    await conn.execute("INSERT OR IGNORE INTO training_exercises(owner_telegram_id,name,muscle_group,created_at) VALUES (?,?,?,?)", (message.from_user.id, name, group or "Інше", datetime.now().isoformat(timespec="seconds")))
    row = await (await conn.execute("SELECT id,name,muscle_group FROM training_exercises WHERE owner_telegram_id=? AND name=? COLLATE NOCASE", (message.from_user.id, name))).fetchone()
    await conn.commit()
    data = await state.get_data()
    if "day_id" not in data:
        await state.clear()
        await message.answer(f"✅ Вправу «{escape(row['name'])}» додано до вашого каталогу.")
        text, markup = await render_program(message.from_user.id)
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
        return
    await state.set_state(TrainingFlow.exercise_data)
    await state.update_data(day_id=data["day_id"], exercise_id=row["id"], exercise_name=row["name"], muscle_group=row["muscle_group"])
    await message.answer(f"Вкажіть поточну вагу для <b>{escape(row['name'])}</b> у кг (наприклад <code>12.5</code>) або надішліть тире, якщо додаткової ваги немає.", parse_mode="HTML", reply_markup=kb([[('✖️ Скасувати', 'training:cancel')]]))


@router.message(TrainingFlow.exercise_data)
async def save_program_exercise(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    weight_text = (message.text or "").strip().replace(",", ".")
    weight = None
    if weight_text not in {"", "-", "—"}:
        try:
            weight = parse_number(weight_text)
        except (ValueError, TypeError):
            await message.answer("Не розпізнав вагу. Введіть число в кг, наприклад <code>62.5</code>, або тире для вправи без додаткової ваги.", parse_mode="HTML")
            return
        if weight < 0 or weight > 2000:
            await message.answer("Вага має бути від 0 до 2000 кг.")
            return
    pid = await profile_id(message.from_user.id)
    conn = db()._db()
    day = await (await conn.execute("SELECT d.id FROM training_days d JOIN training_programs p ON p.id=d.program_id WHERE d.id=? AND p.profile_id=?", (data["day_id"], pid))).fetchone()
    if not day:
        await state.clear()
        await message.answer("Тренувальний день уже видалено. Відкрийте програму ще раз.")
        return
    order = await (await conn.execute("SELECT COALESCE(MAX(order_index),-1)+1 AS n FROM training_program_exercises WHERE day_id=?", (data["day_id"],))).fetchone()
    await conn.execute("INSERT INTO training_program_exercises(day_id,exercise_id,name_snapshot,muscle_group,current_weight,order_index) VALUES (?,?,?,?,?,?)", (data["day_id"], data["exercise_id"], data["exercise_name"], data["muscle_group"], weight, order["n"]))
    await conn.commit()
    await state.clear()
    await message.answer("✅ Вправу додано до програми.")
    text, markup = await render_program(message.from_user.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("training:weight:"))
async def weight_start(callback: CallbackQuery, state: FSMContext) -> None:
    exercise_id = int(callback.data.rsplit(":", 1)[1])
    pid = await profile_id(callback.from_user.id)
    row = await (await db()._db().execute("SELECT e.* FROM training_program_exercises e JOIN training_days d ON d.id=e.day_id JOIN training_programs p ON p.id=d.program_id WHERE e.id=? AND p.profile_id=?", (exercise_id, pid))).fetchone()
    if not row:
        await callback.answer("Вправу не знайдено.", show_alert=True)
        return
    await state.set_state(TrainingFlow.weight)
    await state.update_data(program_exercise_id=exercise_id)
    await callback.message.edit_text(f"Нова вага для <b>{escape(row['name_snapshot'])}</b> у кг. Надішліть число; щоб очистити вагу, надішліть тире.", parse_mode="HTML", reply_markup=kb([[('✖️ Скасувати', 'training:cancel')]]))
    await callback.answer()


@router.message(TrainingFlow.weight)
async def save_weight(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    weight = None
    if raw not in {"-", "—"}:
        try:
            weight = parse_number(raw)
        except (ValueError, TypeError):
            await message.answer("Введіть число у кілограмах, наприклад <code>62.5</code>, або тире.", parse_mode="HTML")
            return
        if weight < 0 or weight > 2000:
            await message.answer("Вага має бути від 0 до 2000 кг.")
            return
    pid = await profile_id(message.from_user.id)
    data = await state.get_data()
    cur = await db()._db().execute("UPDATE training_program_exercises SET current_weight=? WHERE id=? AND day_id IN (SELECT d.id FROM training_days d JOIN training_programs p ON p.id=d.program_id WHERE p.profile_id=?)", (weight, data["program_exercise_id"], pid))
    await db()._db().commit()
    await state.clear()
    await message.answer("✅ Вагу оновлено." if cur.rowcount else "Вправу вже видалено.")
    text, markup = await render_program(message.from_user.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("training:done:"))
async def finish_day(callback: CallbackQuery) -> None:
    day_id = int(callback.data.rsplit(":", 1)[1])
    pid = await profile_id(callback.from_user.id)
    conn = db()._db()
    day = await (await conn.execute("SELECT d.* FROM training_days d JOIN training_programs p ON p.id=d.program_id WHERE d.id=? AND p.profile_id=?", (day_id, pid))).fetchone()
    if not day:
        await callback.answer("День не знайдено.", show_alert=True)
        return
    today = local_today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    await conn.execute("INSERT OR IGNORE INTO training_sessions(profile_id,day_title,session_date,created_at) VALUES (?,?,?,?)", (pid, day["title"], today, now))
    session = await (await conn.execute("SELECT id FROM training_sessions WHERE profile_id=? AND day_title=? AND session_date=?", (pid, day["title"], today))).fetchone()
    await conn.execute("DELETE FROM training_exercise_logs WHERE session_id=?", (session["id"],))
    exercises = await (await conn.execute("SELECT name_snapshot,current_weight FROM training_program_exercises WHERE day_id=? ORDER BY order_index,id", (day_id,))).fetchall()
    for exercise in exercises:
        await conn.execute("INSERT INTO training_exercise_logs(session_id,exercise_name,weight) VALUES (?,?,?)", (session["id"], exercise["name_snapshot"], exercise["current_weight"]))
    await conn.commit()
    await callback.answer("Тренування записано ✅")
    await show(callback)


async def send_progress_chart(callback: CallbackQuery, pid: int, exercise_name: str) -> None:
    conn = db()._db()
    rows = await (await conn.execute("SELECT s.session_date AS log_date,l.weight FROM training_exercise_logs l JOIN training_sessions s ON s.id=l.session_id WHERE s.profile_id=? AND l.exercise_name=? AND l.weight IS NOT NULL ORDER BY s.session_date", (pid, exercise_name))).fetchall()
    if not rows:
        await callback.answer("Історії поки немає. Позначте тренувальний день виконаним.", show_alert=True)
        return
    path = exercise_progress_chart(rows, exercise_name)
    try:
        await callback.message.answer_photo(BufferedInputFile(path.read_bytes(), filename="exercise-progress.png"), caption=f"📈 Прогрес: {exercise_name}")
    finally:
        path.unlink(missing_ok=True)
    await callback.answer()


@router.callback_query(F.data == "training:history")
async def progress_history(callback: CallbackQuery) -> None:
    pid = await profile_id(callback.from_user.id)
    exercises = await (await db()._db().execute(
        "SELECT e.id,e.name FROM training_exercises e WHERE (e.owner_telegram_id IS NULL OR e.owner_telegram_id=?) "
        "AND EXISTS (SELECT 1 FROM training_exercise_logs l JOIN training_sessions s ON s.id=l.session_id WHERE s.profile_id=? AND l.exercise_name=e.name AND l.weight IS NOT NULL) "
        "ORDER BY e.muscle_group,e.name",
        (callback.from_user.id, pid),
    )).fetchall()
    if not exercises:
        await callback.answer("Поки що немає записаних тренувань.", show_alert=True)
        return
    rows = [[(f"📈 {e['name'][:48]}", f"training:history-chart:{e['id']}")] for e in exercises]
    rows.append([("← До програми", "training:menu")])
    await callback.message.edit_text("📚 <b>Історія силового прогресу</b>\n\nОберіть вправу:", reply_markup=kb(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("training:history-chart:"))
async def history_chart(callback: CallbackQuery) -> None:
    exercise_id = int(callback.data.rsplit(":", 1)[1])
    row = await (await db()._db().execute("SELECT name FROM training_exercises WHERE id=? AND (owner_telegram_id IS NULL OR owner_telegram_id=?)", (exercise_id, callback.from_user.id))).fetchone()
    if not row:
        await callback.answer("Вправу не знайдено.", show_alert=True)
        return
    await send_progress_chart(callback, await profile_id(callback.from_user.id), row["name"])


@router.callback_query(F.data.startswith("training:history-chart-by-name:"))
async def current_exercise_chart(callback: CallbackQuery) -> None:
    exercise_id = int(callback.data.rsplit(":", 1)[1])
    pid = await profile_id(callback.from_user.id)
    exercise = await (await db()._db().execute(
        "SELECT e.name_snapshot FROM training_program_exercises e "
        "JOIN training_days d ON d.id=e.day_id JOIN training_programs p ON p.id=d.program_id "
        "WHERE e.id=? AND p.profile_id=?", (exercise_id, pid),
    )).fetchone()
    if not exercise:
        await callback.answer("Вправу не знайдено.", show_alert=True)
        return
    await send_progress_chart(callback, pid, exercise["name_snapshot"])


@router.callback_query(F.data.startswith("training:remove-ex:"))
async def remove_exercise(callback: CallbackQuery) -> None:
    exercise_id = int(callback.data.rsplit(":", 1)[1])
    pid = await profile_id(callback.from_user.id)
    await db()._db().execute("DELETE FROM training_program_exercises WHERE id=? AND day_id IN (SELECT d.id FROM training_days d JOIN training_programs p ON p.id=d.program_id WHERE p.profile_id=?)", (exercise_id, pid))
    await db()._db().commit()
    await show(callback)
    await callback.answer("Вправу видалено")


@router.callback_query(F.data.startswith("training:manage-ex:"))
async def manage_exercises(callback: CallbackQuery) -> None:
    day_id = int(callback.data.rsplit(":", 1)[1])
    pid = await profile_id(callback.from_user.id)
    day = await (await db()._db().execute(
        "SELECT d.title FROM training_days d JOIN training_programs p ON p.id=d.program_id WHERE d.id=? AND p.profile_id=?",
        (day_id, pid),
    )).fetchone()
    if not day:
        await callback.answer("День не знайдено.", show_alert=True)
        return
    exercises = await (await db()._db().execute(
        "SELECT id,name_snapshot FROM training_program_exercises WHERE day_id=? ORDER BY order_index,id", (day_id,)
    )).fetchall()
    rows = [[(f"🗑 {exercise['name_snapshot'][:45]}", f"training:remove-ex:{exercise['id']}")] for exercise in exercises]
    rows.append([("← До програми", "training:menu")])
    await callback.message.edit_text(f"Оберіть вправу для видалення з дня <b>{escape(day['title'])}</b>:", reply_markup=kb(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("training:remove-day:"))
async def remove_day(callback: CallbackQuery) -> None:
    day_id = int(callback.data.rsplit(":", 1)[1])
    pid = await profile_id(callback.from_user.id)
    await db()._db().execute("DELETE FROM training_days WHERE id=? AND program_id IN (SELECT id FROM training_programs WHERE profile_id=?)", (day_id, pid))
    await db()._db().commit()
    await show(callback)
    await callback.answer("День видалено; журнал тренувань збережено")


@router.callback_query(F.data == "training:delete")
async def delete_prompt(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Видалити поточну програму? Журнал уже записаних тренувань залишиться.", reply_markup=kb([[('Так, видалити', 'training:delete:yes')], [('← Назад', 'training:menu')]]))
    await callback.answer()


@router.callback_query(F.data == "training:delete:yes")
async def delete_program(callback: CallbackQuery) -> None:
    pid = await profile_id(callback.from_user.id)
    await db()._db().execute("DELETE FROM training_programs WHERE profile_id=?", (pid,))
    await db()._db().commit()
    await callback.answer("Програму видалено")
    await show(callback)


@router.callback_query(F.data == "training:cancel")
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Скасовано")
    await show(callback)
