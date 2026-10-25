from __future__ import annotations

import json
import logging
import re
import sqlite3
import tempfile
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
from .time_utils import local_date_range_utc_naive, local_today


logger = logging.getLogger(__name__)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS telegram_accounts (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    active_profile_id INTEGER,
    managed_profile_id INTEGER,
    access_status TEXT NOT NULL DEFAULT 'pending',
    is_admin INTEGER NOT NULL DEFAULT 0,
    can_manage_diet INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_preferences (
    telegram_id INTEGER PRIMARY KEY,
    daily_summary_time TEXT,
    last_daily_summary_date TEXT,
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS bot_backup_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    frequency TEXT NOT NULL DEFAULT 'off',
    send_time TEXT NOT NULL DEFAULT '20:00',
    weekday INTEGER NOT NULL DEFAULT 0,
    monthday INTEGER NOT NULL DEFAULT 1,
    last_attempt_key TEXT,
    last_success_key TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_error TEXT
);
INSERT OR IGNORE INTO bot_backup_settings(id) VALUES (1);
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_telegram_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    height REAL NOT NULL DEFAULT 177,
    age INTEGER NOT NULL DEFAULT 19,
    sex TEXT NOT NULL DEFAULT 'чоловіча',
    target_weight REAL,
    goal_mode TEXT NOT NULL DEFAULT 'підтримання',
    surplus_deficit REAL NOT NULL DEFAULT 0,
    surplus_gain REAL NOT NULL DEFAULT 300,
    surplus_maintenance REAL NOT NULL DEFAULT 0,
    deficit_loss REAL NOT NULL DEFAULT 400,
    diet_mode TEXT NOT NULL DEFAULT 'single',
    training_weekdays TEXT,
    protein_per_kg REAL NOT NULL DEFAULT 2,
    fat_per_kg REAL NOT NULL DEFAULT 1,
    manual_mode INTEGER NOT NULL DEFAULT 0,
    manual_weight REAL,
    manual_activity REAL,
    workout_kcal_today REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(owner_telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS food_database (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_telegram_id INTEGER NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    protein REAL NOT NULL,
    fat REAL NOT NULL,
    carbs REAL NOT NULL,
    calories REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT 'г',
    unit_weight REAL,
    UNIQUE(owner_telegram_id, name),
    FOREIGN KEY(owner_telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS food_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    diet_type TEXT NOT NULL DEFAULT 'rest',
    meal TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    quantity REAL NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY(product_id) REFERENCES food_database(id)
);
CREATE TABLE IF NOT EXISTS meal_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    items TEXT NOT NULL,
    UNIQUE(profile_id, name),
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS weight_log (
    profile_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    weight REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY(profile_id, log_date),
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS height_log (
    profile_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    height REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'import',
    PRIMARY KEY(profile_id, log_date),
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS activity_log (
    profile_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    steps REAL NOT NULL DEFAULT 0,
    active_calories REAL NOT NULL DEFAULT 0,
    workout_calories REAL NOT NULL DEFAULT 0,
    distance REAL NOT NULL DEFAULT 0,
    floors REAL NOT NULL DEFAULT 0,
    workout_count REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(profile_id, log_date),
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sleep_log (
    profile_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    asleep_sec REAL NOT NULL DEFAULT 0,
    awake_sec REAL NOT NULL DEFAULT 0,
    duration_sec REAL NOT NULL DEFAULT 0,
    avg_heart REAL,
    max_heart REAL,
    min_heart REAL,
    PRIMARY KEY(profile_id, log_date),
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS monobank_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    token_ciphertext TEXT NOT NULL,
    token_hint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(telegram_id),
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finance_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'expense',
    mcc_codes TEXT NOT NULL DEFAULT '',
    UNIQUE(telegram_id, name),
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finance_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    needs_percent REAL NOT NULL,
    wants_percent REAL NOT NULL,
    savings_percent REAL NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    UNIQUE(telegram_id, name),
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finance_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monobank_account_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    default_label TEXT NOT NULL,
    alias TEXT,
    currency_code INTEGER NOT NULL DEFAULT 980,
    balance_minor INTEGER,
    balance_updated_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(monobank_account_id, external_id),
    FOREIGN KEY(monobank_account_id) REFERENCES monobank_accounts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finance_jars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monobank_account_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    balance_minor INTEGER NOT NULL DEFAULT 0,
    goal_minor INTEGER NOT NULL DEFAULT 0,
    currency_code INTEGER NOT NULL DEFAULT 980,
    updated_at TEXT NOT NULL,
    UNIQUE(monobank_account_id, external_id),
    FOREIGN KEY(monobank_account_id) REFERENCES monobank_accounts(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finance_auto_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    keyword TEXT NOT NULL,
    keyword_normalized TEXT NOT NULL,
    mcc INTEGER,
    category_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(telegram_id, keyword_normalized),
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE,
    FOREIGN KEY(category_id) REFERENCES finance_categories(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finance_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    monobank_account_id INTEGER,
    mono_transaction_id TEXT,
    finance_account_id INTEGER,
    amount_minor INTEGER NOT NULL,
    currency_code INTEGER NOT NULL DEFAULT 980,
    description TEXT NOT NULL DEFAULT '',
    account_label TEXT NOT NULL DEFAULT 'Monobank',
    mcc INTEGER,
    category_id INTEGER,
    goal_id INTEGER,
    source TEXT NOT NULL DEFAULT 'manual',
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(monobank_account_id, mono_transaction_id),
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE,
    FOREIGN KEY(monobank_account_id) REFERENCES monobank_accounts(id) ON DELETE CASCADE,
    FOREIGN KEY(finance_account_id) REFERENCES finance_accounts(id) ON DELETE SET NULL,
    FOREIGN KEY(category_id) REFERENCES finance_categories(id) ON DELETE SET NULL
    ,FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_finance_transactions_user_date ON finance_transactions(telegram_id, occurred_at);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goal_type TEXT NOT NULL DEFAULT 'life',
    target_value REAL NOT NULL,
    current_value REAL NOT NULL DEFAULT 0,
    target_date TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_goals_user_status ON goals(telegram_id, status);
CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    frequency TEXT NOT NULL DEFAULT 'daily',
    reminder_time TEXT,
    last_reminder_date TEXT,
    savings_amount_minor INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS habit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(habit_id, log_date),
    FOREIGN KEY(habit_id) REFERENCES habits(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_habit_logs_habit_date ON habit_logs(habit_id, log_date);
CREATE TABLE IF NOT EXISTS reflection_entries (
    telegram_id INTEGER NOT NULL,
    entry_date TEXT NOT NULL,
    good TEXT NOT NULL DEFAULT '',
    challenge TEXT NOT NULL DEFAULT '',
    self_care TEXT NOT NULL DEFAULT '',
    tomorrow TEXT NOT NULL DEFAULT '',
    mood INTEGER,
    energy INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(telegram_id, entry_date),
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reflection_entries_user_date ON reflection_entries(telegram_id, entry_date DESC);
CREATE TABLE IF NOT EXISTS weekly_reflections (
    telegram_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    energy_sources TEXT NOT NULL DEFAULT '',
    patterns TEXT NOT NULL DEFAULT '',
    proud_of TEXT NOT NULL DEFAULT '',
    next_week TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(telegram_id, week_start),
    FOREIGN KEY(telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS training_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_telegram_id INTEGER,
    name TEXT NOT NULL COLLATE NOCASE,
    muscle_group TEXT NOT NULL DEFAULT 'Інше',
    created_at TEXT NOT NULL,
    UNIQUE(owner_telegram_id, name),
    FOREIGN KEY(owner_telegram_id) REFERENCES telegram_accounts(telegram_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS training_programs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT 'Моя програма',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS training_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    order_index INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(program_id) REFERENCES training_programs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS training_program_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_id INTEGER NOT NULL,
    exercise_id INTEGER,
    name_snapshot TEXT NOT NULL,
    muscle_group TEXT NOT NULL DEFAULT 'Інше',
    prescription TEXT NOT NULL DEFAULT '',
    current_weight REAL,
    order_index INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(day_id) REFERENCES training_days(id) ON DELETE CASCADE,
    FOREIGN KEY(exercise_id) REFERENCES training_exercises(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS training_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    day_title TEXT NOT NULL,
    session_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, day_title, session_date),
    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS training_exercise_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    exercise_name TEXT NOT NULL,
    weight REAL,
    FOREIGN KEY(session_id) REFERENCES training_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_training_sessions_profile_date ON training_sessions(profile_id, session_date);
CREATE INDEX IF NOT EXISTS idx_training_logs_name ON training_exercise_logs(exercise_name);
"""

class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        backup_columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(bot_backup_settings)")).fetchall()}
        if "last_success_key" not in backup_columns:
            await self._db().execute("ALTER TABLE bot_backup_settings ADD COLUMN last_success_key TEXT")
        await self._migrate_accounts()
        await self._migrate_activity_log()
        await self._migrate_food_log()
        await self._migrate_profiles()
        await self._migrate_finance_categories()
        await self._migrate_finance_auto_rules()
        await self._migrate_finance_transactions()
        await self._migrate_finance_accounts()
        await self._migrate_reflection_entries()
        habit_columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(habits)")).fetchall()}
        if "last_reminder_date" not in habit_columns:
            await self._db().execute("ALTER TABLE habits ADD COLUMN last_reminder_date TEXT")
        await self.conn.commit()

    async def _migrate_reflection_entries(self) -> None:
        columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(reflection_entries)")).fetchall()}
        for column in ("mood", "energy"):
            if column not in columns:
                await self._db().execute(f"ALTER TABLE reflection_entries ADD COLUMN {column} INTEGER")

    async def _migrate_accounts(self) -> None:
        columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(telegram_accounts)")).fetchall()}
        migrations = {
            "username": "TEXT",
            "first_name": "TEXT",
            "last_name": "TEXT",
            "managed_profile_id": "INTEGER",
            "access_status": "TEXT NOT NULL DEFAULT 'approved'",
            "is_admin": "INTEGER NOT NULL DEFAULT 0",
            "can_manage_diet": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in migrations.items():
            if column not in columns:
                await self._db().execute(f"ALTER TABLE telegram_accounts ADD COLUMN {column} {definition}")


    async def _migrate_activity_log(self) -> None:
        columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(activity_log)")).fetchall()}
        if "workout_calories" not in columns:
            await self._db().execute("ALTER TABLE activity_log ADD COLUMN workout_calories REAL NOT NULL DEFAULT 0")

    async def _migrate_food_log(self) -> None:
        columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(food_log)")).fetchall()}
        if "diet_type" not in columns:
            await self._db().execute("ALTER TABLE food_log ADD COLUMN diet_type TEXT NOT NULL DEFAULT 'rest'")

    async def _migrate_profiles(self) -> None:
        columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(profiles)")).fetchall()}
        if "diet_mode" not in columns:
            await self._db().execute("ALTER TABLE profiles ADD COLUMN diet_mode TEXT NOT NULL DEFAULT 'single'")
        if "training_weekdays" not in columns:
            await self._db().execute("ALTER TABLE profiles ADD COLUMN training_weekdays TEXT")

    async def _migrate_finance_accounts(self) -> None:
        columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(finance_accounts)")).fetchall()}
        migrations = {
            "balance_minor": "INTEGER",
            "balance_updated_at": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in columns:
                await self._db().execute(f"ALTER TABLE finance_accounts ADD COLUMN {column} {definition}")

    async def _migrate_finance_categories(self) -> None:
        db = self._db()
        aliases = {
            "Продукти": "🛒 Продукти",
            "Транспорт": "🚗 Транспорт",
            "Ресторани": "🍽 Кафе та ресторани",
            "Кафе та ресторани": "🍽 Кафе та ресторани",
            "Комунальні": "🏠 Комунальні та зв'язок",
            "Комунальні та зв'язок": "🏠 Комунальні та зв'язок",
            "Здоровʼя": "🩺 Здоровʼя",
            "Розваги": "🎬 Розваги та дозвілля",
            "Розваги та дозвілля": "🎬 Розваги та дозвілля",
            "Покупки": "🛍 Покупки та одяг",
            "Покупки та одяг": "🛍 Покупки та одяг",
            "Інше": "📦 Інше",
        }
        for old_name, new_name in aliases.items():
            if old_name == new_name:
                continue
            old_rows = await (await db.execute("SELECT id FROM finance_categories WHERE telegram_id IS NULL AND name=?", (old_name,))).fetchall()
            target = await (await db.execute("SELECT id FROM finance_categories WHERE telegram_id IS NULL AND name=? ORDER BY id LIMIT 1", (new_name,))).fetchone()
            if target:
                for old_row in old_rows:
                    await db.execute("UPDATE finance_transactions SET category_id=? WHERE category_id=?", (target["id"], old_row["id"]))
                    await db.execute("UPDATE finance_auto_rules SET category_id=? WHERE category_id=?", (target["id"], old_row["id"]))
                    await db.execute("DELETE FROM finance_categories WHERE id=?", (old_row["id"],))
            elif old_rows:
                await db.execute("UPDATE finance_categories SET name=? WHERE id=?", (new_name, old_rows[0]["id"]))
                for old_row in old_rows[1:]:
                    await db.execute("UPDATE finance_transactions SET category_id=? WHERE category_id=?", (old_rows[0]["id"], old_row["id"]))
                    await db.execute("UPDATE finance_auto_rules SET category_id=? WHERE category_id=?", (old_rows[0]["id"], old_row["id"]))
                    await db.execute("DELETE FROM finance_categories WHERE id=?", (old_row["id"],))
        duplicates = await (await db.execute("SELECT name, MIN(id) AS keep_id FROM finance_categories WHERE telegram_id IS NULL GROUP BY name HAVING COUNT(*) > 1")).fetchall()
        for duplicate in duplicates:
            ids = await (await db.execute("SELECT id FROM finance_categories WHERE telegram_id IS NULL AND name=? AND id<>?", (duplicate["name"], duplicate["keep_id"]))).fetchall()
            for row in ids:
                await db.execute("UPDATE finance_transactions SET category_id=? WHERE category_id=?", (duplicate["keep_id"], row["id"]))
                await db.execute("UPDATE finance_auto_rules SET category_id=? WHERE category_id=?", (duplicate["keep_id"], row["id"]))
                await db.execute("DELETE FROM finance_categories WHERE id=?", (row["id"],))
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_finance_categories_global_name ON finance_categories(name) WHERE telegram_id IS NULL")
        defaults = {
            "🛒 Продукти": "5411,5422,5441,5451,5462,5499",
            "🍽 Кафе та ресторани": "5812,5813,5814",
            "🚗 Транспорт": "4011,4111,4121,4131,4511,5541,5542,5571,5599",
            "🏠 Комунальні та зв'язок": "4900",
            "🩺 Здоровʼя": "5912,8011,8021,8031,8041,8042,8043,8050,8062,8099",
            "🎬 Розваги та дозвілля": "7832,7911,7922,7929,7932,7933,7941,7991,7994,7995,7996,7998,7999",
            "🛍 Покупки та одяг": "5311,5331,5399,5611,5621,5631,5641,5651,5661,5691,5699,5712,5732,5734,5942,5999",
            "💸 Перекази": "",
            "💰 Накопичення": "",
            "📦 Інше": "",
        }
        for name, mcc_codes in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO finance_categories(telegram_id, name, kind, mcc_codes) VALUES (NULL, ?, 'expense', ?)",
                (name, mcc_codes),
            )

    async def _migrate_finance_transactions(self) -> None:
        columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(finance_transactions)")).fetchall()}
        if "account_label" not in columns:
            await self._db().execute("ALTER TABLE finance_transactions ADD COLUMN account_label TEXT NOT NULL DEFAULT 'Monobank'")
        if "finance_account_id" not in columns:
            await self._db().execute("ALTER TABLE finance_transactions ADD COLUMN finance_account_id INTEGER")
        if "goal_id" not in columns:
            await self._db().execute("ALTER TABLE finance_transactions ADD COLUMN goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL")
        await self._db().execute("CREATE INDEX IF NOT EXISTS idx_finance_transactions_goal ON finance_transactions(goal_id)")

    async def _migrate_finance_auto_rules(self) -> None:
        columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(finance_auto_rules)")).fetchall()}
        if "mcc" not in columns:
            await self._db().execute("ALTER TABLE finance_auto_rules ADD COLUMN mcc INTEGER")

    async def configure_admin(self, telegram_id: int | None) -> int:
        db = self._db()
        if telegram_id is not None:
            await db.execute("INSERT OR IGNORE INTO telegram_accounts(telegram_id, access_status, created_at) VALUES (?, 'approved', ?)", (telegram_id, datetime.now().isoformat(timespec="seconds")))
            admin_id = telegram_id
        else:
            row = await (await db.execute("SELECT telegram_id FROM telegram_accounts WHERE is_admin=1 ORDER BY telegram_id LIMIT 1")).fetchone()
            if row:
                return row["telegram_id"]
            return 0
        await db.execute("UPDATE telegram_accounts SET is_admin=0, can_manage_diet=0 WHERE telegram_id<>?", (admin_id,))
        await db.execute("UPDATE telegram_accounts SET is_admin=1, can_manage_diet=1, access_status='approved' WHERE telegram_id=?", (admin_id,))
        await db.commit()
        return admin_id
    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    def _db(self) -> aiosqlite.Connection:
        if not self.conn:
            raise RuntimeError("Database is not connected")
        return self.conn

    async def ensure_account(self, telegram_id: int) -> tuple[aiosqlite.Row, bool]:
        db = self._db()
        row = await (await db.execute("SELECT * FROM telegram_accounts WHERE telegram_id=?", (telegram_id,))).fetchone()
        if row:
            return row, False
        now = datetime.now().isoformat(timespec="seconds")
        await db.execute(
            "INSERT INTO telegram_accounts(telegram_id, access_status, is_admin, can_manage_diet, created_at) VALUES (?, 'pending', 0, 0, ?)",
            (telegram_id, now),
        )
        await db.commit()
        return await (await db.execute("SELECT * FROM telegram_accounts WHERE telegram_id=?", (telegram_id,))).fetchone(), True

    async def update_telegram_identity(self, telegram_id: int, username: str | None, first_name: str | None, last_name: str | None) -> None:
        await self._db().execute(
            "UPDATE telegram_accounts SET username=?, first_name=?, last_name=? WHERE telegram_id=?",
            (username, first_name, last_name, telegram_id),
        )
        await self._db().commit()

    async def account(self, telegram_id: int) -> aiosqlite.Row | None:
        return await (await self._db().execute("SELECT * FROM telegram_accounts WHERE telegram_id=?", (telegram_id,))).fetchone()

    async def accounts(self) -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT * FROM telegram_accounts ORDER BY created_at, telegram_id")).fetchall()

    async def set_access(self, telegram_id: int, status: str) -> None:
        await self._db().execute("UPDATE telegram_accounts SET access_status=?, can_manage_diet=0 WHERE telegram_id=?", (status, telegram_id))
        await self._db().commit()

    async def set_diet_permission(self, telegram_id: int, allowed: bool) -> None:
        await self._db().execute("UPDATE telegram_accounts SET can_manage_diet=? WHERE telegram_id=? AND access_status='approved'", (int(allowed), telegram_id))
        await self._db().commit()

    async def set_managed_profile(self, admin_id: int, profile_id: int | None) -> None:
        await self._db().execute("UPDATE telegram_accounts SET managed_profile_id=? WHERE telegram_id=? AND is_admin=1", (profile_id, admin_id))
        await self._db().commit()

    async def food_owner(self, user_id: int) -> int:
        row = await (await self._db().execute("SELECT telegram_id FROM telegram_accounts WHERE is_admin=1 LIMIT 1")).fetchone()
        return row["telegram_id"] if row else user_id

    async def create_profile(self, owner_id: int, name: str = "Я") -> int:
        db = self._db()
        account = await (await db.execute(
            "SELECT active_profile_id FROM telegram_accounts WHERE telegram_id=?",
            (owner_id,),
        )).fetchone()
        existing = await (await db.execute(
            "SELECT id FROM profiles WHERE owner_telegram_id=? ORDER BY id",
            (owner_id,),
        )).fetchall()
        if existing:
            existing_ids = {row["id"] for row in existing}
            active_id = account["active_profile_id"] if account else None
            profile_id = active_id if active_id in existing_ids else existing[0]["id"]
            await db.execute(
                "UPDATE telegram_accounts SET active_profile_id=? WHERE telegram_id=?",
                (profile_id, owner_id),
            )
            await db.commit()
            return int(profile_id)
        cur = await db.execute(
            "INSERT INTO profiles(owner_telegram_id, name, created_at) VALUES (?, ?, ?)",
            (owner_id, name, datetime.now().isoformat(timespec="seconds")),
        )
        profile_id = cur.lastrowid
        await db.execute("UPDATE telegram_accounts SET active_profile_id=? WHERE telegram_id=?", (profile_id, owner_id))
        await db.commit()
        return int(profile_id)

    async def active_profile(self, owner_id: int) -> aiosqlite.Row | None:
        db = self._db()
        return await (await db.execute(
            """
            SELECT p.*
            FROM profiles p
            JOIN telegram_accounts a ON a.telegram_id=?
            WHERE (
                a.is_admin=1
                AND a.managed_profile_id IS NOT NULL
                AND p.id=a.managed_profile_id
            ) OR (
                p.owner_telegram_id=a.telegram_id
                AND p.id=a.active_profile_id
            )
            """,
            (owner_id,),
        )).fetchone()

    async def set_active_profile(self, owner_id: int, profile_id: int) -> None:
        await self._db().execute("UPDATE telegram_accounts SET active_profile_id=? WHERE telegram_id=?", (profile_id, owner_id))
        await self._db().commit()

    async def profiles(self, owner_id: int) -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT * FROM profiles WHERE owner_telegram_id=? ORDER BY id", (owner_id,))).fetchall()

    async def update_profile(self, profile_id: int, **values: Any) -> None:
        allowed = {"name", "height", "age", "sex", "target_weight", "goal_mode", "surplus_deficit", "surplus_gain", "surplus_maintenance", "deficit_loss", "diet_mode", "training_weekdays", "protein_per_kg", "fat_per_kg", "manual_mode", "manual_weight", "manual_activity", "workout_kcal_today"}
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return
        columns = ", ".join(f"{key}=?" for key in values)
        await self._db().execute(f"UPDATE profiles SET {columns} WHERE id=?", (*values.values(), profile_id))
        await self._db().commit()

    async def foods(self, owner_id: int, query: str = "") -> list[aiosqlite.Row]:
        rows = await (await self._db().execute(
            "SELECT * FROM food_database WHERE owner_telegram_id=? ORDER BY name",
            (owner_id,),
        )).fetchall()
        if not query:
            return rows

        normalized_query = self._normalize_food_name(query)
        ranked: list[tuple[float, aiosqlite.Row]] = []
        for row in rows:
            normalized_name = self._normalize_food_name(row["name"])
            if normalized_query in normalized_name:
                score = 1.0
            else:
                score = SequenceMatcher(None, normalized_query, normalized_name).ratio()
            if score >= 0.45:
                ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], item[1]["name"].casefold()))
        return [row for _, row in ranked]

    async def export_foods(self, owner_id: int) -> list[dict[str, Any]]:
        rows = await (await self._db().execute(
            "SELECT name, protein, fat, carbs, calories, unit, unit_weight FROM food_database WHERE owner_telegram_id=? ORDER BY name",
            (owner_id,),
        )).fetchall()
        return [dict(row) for row in rows]

    async def upsert_food(self, owner_id: int, values: dict[str, Any]) -> bool:
        db = self._db()
        existing_rows = await (await db.execute(
            "SELECT id, name FROM food_database WHERE owner_telegram_id=?",
            (owner_id,),
        )).fetchall()
        existing = next(
            (row for row in existing_rows if self._normalize_food_name(row["name"]) == self._normalize_food_name(values["name"])),
            None,
        )
        if existing:
            await db.execute(
                "UPDATE food_database SET protein=?, fat=?, carbs=?, calories=?, unit=?, unit_weight=? WHERE id=?",
                (values["protein"], values["fat"], values["carbs"], values["calories"], values["unit"], values["unit_weight"], existing["id"]),
            )
            await db.commit()
            return False
        await db.execute(
            "INSERT INTO food_database(owner_telegram_id, name, protein, fat, carbs, calories, unit, unit_weight) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, values["name"], values["protein"], values["fat"], values["carbs"], values["calories"], values["unit"], values["unit_weight"]),
        )
        await self._db().commit()
        return True

    @staticmethod
    def _normalize_food_name(value: str) -> str:
        value = value.casefold()
        return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)

    async def add_food(self, owner_id: int, values: dict[str, Any]) -> int:
        db = self._db()
        cur = await db.execute("INSERT INTO food_database(owner_telegram_id, name, protein, fat, carbs, calories, unit, unit_weight) VALUES (:owner, :name, :protein, :fat, :carbs, :calories, :unit, :unit_weight)", {"owner": owner_id, **values})
        await db.commit()
        return int(cur.lastrowid)

    async def add_food_log(self, profile_id: int, log_date: str, meal: str, product_id: int, quantity: float, diet_type: str = "rest") -> None:
        db = self._db()
        profile = await (await db.execute("SELECT id FROM profiles WHERE id=?", (profile_id,))).fetchone()
        product = await (await db.execute("SELECT id FROM food_database WHERE id=?", (product_id,))).fetchone()
        if not profile:
            raise ValueError("Активний профіль більше не існує.")
        if not product:
            raise ValueError("Продукт більше не існує в базі. Оновіть пораду.")
        try:
            await db.execute(
                "INSERT INTO food_log(profile_id, log_date, diet_type, meal, product_id, quantity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (profile_id, log_date, diet_type, meal, product_id, quantity, datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()
        except aiosqlite.IntegrityError as exc:
            await db.rollback()
            raise ValueError("Не вдалося додати продукт: запис або продукт уже не актуальні. Оновіть раціон.") from exc

    async def add_food_log_quantity(self, profile_id: int, log_date: str, diet_type: str, product_id: int, quantity: float) -> bool:
        """Add quantity to an existing matching food entry, or create a recommendation entry."""
        db = self._db()
        cur = await db.execute(
            "UPDATE food_log SET quantity = quantity + ? "
            "WHERE id = (SELECT id FROM food_log WHERE profile_id=? AND log_date=? "
            "AND diet_type=? AND product_id=? ORDER BY id LIMIT 1)",
            (quantity, profile_id, log_date, diet_type, product_id),
        )
        if cur.rowcount:
            await db.commit()
            return True
        await self.add_food_log(profile_id, log_date, "Рекомендація", product_id, quantity, diet_type)
        return False

    async def food_day(self, profile_id: int, log_date: str, diet_type: str = "rest") -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT l.*, f.name, f.protein, f.fat, f.carbs, f.calories, f.unit FROM food_log l JOIN food_database f ON f.id=l.product_id WHERE l.profile_id=? AND l.log_date=? AND l.diet_type=? ORDER BY l.id", (profile_id, log_date, diet_type))).fetchall()

    async def latest_food_plan_day(self, profile_id: int, diet_type: str) -> tuple[str, list[aiosqlite.Row]] | None:
        """Return aggregated products from the latest logged day for one diet type."""
        db = self._db()
        row = await (await db.execute(
            "SELECT MAX(log_date) AS log_date FROM food_log WHERE profile_id=? AND diet_type=? AND log_date<=?",
            (profile_id, diet_type, local_today().isoformat()),
        )).fetchone()
        if not row or not row["log_date"]:
            return None
        log_date = row["log_date"]
        products = await (await db.execute(
            "SELECT f.id AS product_id, f.name, f.unit, SUM(l.quantity) AS quantity "
            "FROM food_log l JOIN food_database f ON f.id=l.product_id "
            "WHERE l.profile_id=? AND l.diet_type=? AND l.log_date=? "
            "GROUP BY f.id, f.name, f.unit ORDER BY f.name COLLATE NOCASE",
            (profile_id, diet_type, log_date),
        )).fetchall()
        return log_date, products

    async def training_session_dates(self, profile_id: int, since_date: str) -> list[str]:
        rows = await (await self._db().execute(
            "SELECT DISTINCT session_date FROM training_sessions WHERE profile_id=? AND session_date>=? ORDER BY session_date",
            (profile_id, since_date),
        )).fetchall()
        return [row["session_date"] for row in rows]

    async def food_period(self, profile_id: int, start_date: str, end_date: str) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT l.*, f.name, f.protein, f.fat, f.carbs, f.calories, f.unit "
            "FROM food_log l JOIN food_database f ON f.id=l.product_id "
            "WHERE l.profile_id=? AND l.log_date BETWEEN ? AND ? ORDER BY l.log_date, l.id",
            (profile_id, start_date, end_date),
        )).fetchall()

    async def recent_food_meals(self, profile_id: int, diet_type: str, limit: int = 8) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT log_date, diet_type, meal, MAX(id) AS seed_id, COUNT(*) AS item_count "
            "FROM food_log WHERE profile_id=? AND diet_type=? AND log_date<? "
            "GROUP BY log_date, diet_type, meal ORDER BY log_date DESC, seed_id DESC LIMIT ?",
            (profile_id, diet_type, local_today().isoformat(), limit),
        )).fetchall()

    async def repeat_food_meal(self, profile_id: int, seed_id: int, target_date: str) -> int:
        db = self._db()
        source = await (await db.execute(
            "SELECT log_date, diet_type, meal FROM food_log WHERE id=? AND profile_id=?",
            (seed_id, profile_id),
        )).fetchone()
        if not source or source["log_date"] == target_date:
            return 0
        cursor = await db.execute(
            "INSERT INTO food_log(profile_id, log_date, diet_type, meal, product_id, quantity, created_at) "
            "SELECT profile_id, ?, diet_type, meal, product_id, quantity, ? FROM food_log "
            "WHERE profile_id=? AND log_date=? AND diet_type=? AND meal=?",
            (target_date, datetime.now().isoformat(timespec="seconds"), profile_id, source["log_date"], source["diet_type"], source["meal"]),
        )
        await db.commit()
        return cursor.rowcount

    async def delete_last_food(self, profile_id: int, diet_type: str = "rest") -> None:
        await self._db().execute("DELETE FROM food_log WHERE id=(SELECT id FROM food_log WHERE profile_id=? AND diet_type=? ORDER BY id DESC LIMIT 1)", (profile_id, diet_type))
        await self._db().commit()

    async def update_food_log(self, profile_id: int, log_id: int, quantity: float) -> bool:
        cursor = await self._db().execute(
            "UPDATE food_log SET quantity=? WHERE id=? AND profile_id=?",
            (quantity, log_id, profile_id),
        )
        await self._db().commit()
        return cursor.rowcount > 0

    async def delete_food_log(self, profile_id: int, log_id: int) -> bool:
        cursor = await self._db().execute(
            "DELETE FROM food_log WHERE id=? AND profile_id=?",
            (log_id, profile_id),
        )
        await self._db().commit()
        return cursor.rowcount > 0

    async def copy_yesterday(self, profile_id: int, log_date: str, diet_type: str = "rest") -> int:
        yesterday = (date.fromisoformat(log_date) - timedelta(days=1)).isoformat()
        db = self._db()
        rows = await (await db.execute("SELECT meal, product_id, quantity FROM food_log WHERE profile_id=? AND log_date=? AND diet_type=?", (profile_id, yesterday, diet_type))).fetchall()
        for row in rows:
            await db.execute("INSERT INTO food_log(profile_id, log_date, diet_type, meal, product_id, quantity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (profile_id, log_date, diet_type, row["meal"], row["product_id"], row["quantity"], datetime.now().isoformat(timespec="seconds")))
        await db.commit()
        return len(rows)

    async def upsert_weight(self, profile_id: int, log_date: str, weight: float, source: str = "manual") -> None:
        await self._db().execute("INSERT INTO weight_log(profile_id, log_date, weight, source) VALUES (?, ?, ?, ?) ON CONFLICT(profile_id, log_date) DO UPDATE SET weight=excluded.weight, source=excluded.source", (profile_id, log_date, weight, source))
        await self._db().commit()

    async def upsert_height(self, profile_id: int, log_date: str, height: float, source: str = "import") -> None:
        await self._db().execute("INSERT INTO height_log(profile_id, log_date, height, source) VALUES (?, ?, ?, ?) ON CONFLICT(profile_id, log_date) DO UPDATE SET height=excluded.height, source=excluded.source", (profile_id, log_date, height, source))
        await self._db().commit()

    async def heights(self, profile_id: int) -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT * FROM height_log WHERE profile_id=? ORDER BY log_date", (profile_id,))).fetchall()

    async def weights(self, profile_id: int) -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT * FROM weight_log WHERE profile_id=? ORDER BY log_date", (profile_id,))).fetchall()

    async def upsert_activity(self, profile_id: int, values: dict[str, Any]) -> None:
        db = self._db()
        await db.execute("INSERT INTO activity_log(profile_id, log_date, steps, active_calories, workout_calories, distance, floors, workout_count) VALUES (:profile_id, :log_date, :steps, :active_calories, :workout_calories, :distance, :floors, :workout_count) ON CONFLICT(profile_id, log_date) DO UPDATE SET steps=max(activity_log.steps, excluded.steps), active_calories=max(activity_log.active_calories, excluded.active_calories), distance=max(activity_log.distance, excluded.distance), floors=max(activity_log.floors, excluded.floors), workout_count=max(activity_log.workout_count, excluded.workout_count)", {"profile_id": profile_id, "workout_calories": 0, **values})
        await db.commit()

    async def set_workout_calories(self, profile_id: int, log_date: str, calories: float) -> None:
        db = self._db()
        await db.execute("INSERT INTO activity_log(profile_id, log_date, workout_calories) VALUES (?, ?, ?) ON CONFLICT(profile_id, log_date) DO UPDATE SET workout_calories=excluded.workout_calories", (profile_id, log_date, calories))
        await db.commit()

    async def activities(self, profile_id: int, days: int | None = None) -> list[aiosqlite.Row]:
        query = "SELECT * FROM activity_log WHERE profile_id=?"
        args: list[Any] = [profile_id]
        if days:
            query += " AND log_date>=?"
            args.append((local_today() - timedelta(days=days - 1)).isoformat())
        query += " ORDER BY log_date"
        return await (await self._db().execute(query, args)).fetchall()

    async def activities_between(self, profile_id: int, start_date: str, end_date: str) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT * FROM activity_log WHERE profile_id=? AND log_date BETWEEN ? AND ? ORDER BY log_date",
            (profile_id, start_date, end_date),
        )).fetchall()

    async def activity_on_date(self, profile_id: int, log_date: str) -> aiosqlite.Row | None:
        return await (await self._db().execute(
            "SELECT * FROM activity_log WHERE profile_id=? AND log_date=?",
            (profile_id, log_date),
        )).fetchone()

    async def activity_pair(self, profile_id: int, log_date: str) -> tuple[aiosqlite.Row | None, aiosqlite.Row | None]:
        current_date = date.fromisoformat(log_date)
        previous_date = (current_date - timedelta(days=7)).isoformat()
        rows = await (await self._db().execute(
            "SELECT * FROM activity_log WHERE profile_id=? AND log_date IN (?, ?)",
            (profile_id, log_date, previous_date),
        )).fetchall()
        by_date = {row["log_date"]: row for row in rows}
        return by_date.get(log_date), by_date.get(previous_date)

    async def upsert_sleep(self, profile_id: int, values: dict[str, Any]) -> None:
        db = self._db()
        await db.execute("INSERT INTO sleep_log(profile_id, log_date, asleep_sec, awake_sec, duration_sec, avg_heart, max_heart, min_heart) VALUES (:profile_id, :log_date, :asleep_sec, :awake_sec, :duration_sec, :avg_heart, :max_heart, :min_heart) ON CONFLICT(profile_id, log_date) DO UPDATE SET asleep_sec=max(sleep_log.asleep_sec, excluded.asleep_sec), awake_sec=max(sleep_log.awake_sec, excluded.awake_sec), duration_sec=max(sleep_log.duration_sec, excluded.duration_sec), avg_heart=COALESCE(excluded.avg_heart, sleep_log.avg_heart), max_heart=COALESCE(excluded.max_heart, sleep_log.max_heart), min_heart=COALESCE(excluded.min_heart, sleep_log.min_heart)", {"profile_id": profile_id, **values})
        await db.commit()

    async def sleeps(self, profile_id: int, days: int | None = None) -> list[aiosqlite.Row]:
        query = "SELECT * FROM sleep_log WHERE profile_id=?"
        args: list[Any] = [profile_id]
        if days:
            query += " AND log_date>=?"
            args.append((local_today() - timedelta(days=days - 1)).isoformat())
        query += " ORDER BY log_date DESC"
        return await (await self._db().execute(query, args)).fetchall()

    async def sleeps_between(self, profile_id: int, start_date: str, end_date: str) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT * FROM sleep_log WHERE profile_id=? AND log_date BETWEEN ? AND ? ORDER BY log_date DESC",
            (profile_id, start_date, end_date),
        )).fetchall()

    async def save_monobank_account(self, telegram_id: int, token_ciphertext: str, token_hint: str) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        db = self._db()
        await db.execute(
            "INSERT INTO monobank_accounts(telegram_id, token_ciphertext, token_hint, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET token_ciphertext=excluded.token_ciphertext, token_hint=excluded.token_hint, updated_at=excluded.updated_at",
            (telegram_id, token_ciphertext, token_hint, now, now),
        )
        await db.commit()
        row = await (await db.execute("SELECT id FROM monobank_accounts WHERE telegram_id=?", (telegram_id,))).fetchone()
        return int(row["id"])

    async def monobank_account(self, telegram_id: int) -> aiosqlite.Row | None:
        return await (await self._db().execute("SELECT * FROM monobank_accounts WHERE telegram_id=?", (telegram_id,))).fetchone()

    async def delete_monobank_account(self, telegram_id: int) -> None:
        await self._db().execute("DELETE FROM monobank_accounts WHERE telegram_id=?", (telegram_id,))
        await self._db().commit()

    async def save_finance_account(
        self,
        telegram_id: int,
        monobank_account_id: int,
        external_id: str,
        default_label: str,
        currency_code: int = 980,
        balance_minor: int | None = None,
    ) -> int:
        ownership = await (await self._db().execute("SELECT id FROM monobank_accounts WHERE id=? AND telegram_id=?", (monobank_account_id, telegram_id))).fetchone()
        if not ownership:
            raise ValueError("Рахунок Monobank не належить користувачу.")
        now = datetime.now().isoformat(timespec="seconds")
        await self._db().execute(
            "INSERT INTO finance_accounts(monobank_account_id, external_id, default_label, currency_code, balance_minor, balance_updated_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(monobank_account_id, external_id) DO UPDATE SET default_label=excluded.default_label, currency_code=excluded.currency_code, "
            "balance_minor=CASE WHEN excluded.balance_minor IS NULL THEN finance_accounts.balance_minor ELSE excluded.balance_minor END, "
            "balance_updated_at=CASE WHEN excluded.balance_minor IS NULL THEN finance_accounts.balance_updated_at ELSE excluded.balance_updated_at END, "
            "updated_at=excluded.updated_at",
            (monobank_account_id, external_id, default_label, currency_code, balance_minor, now if balance_minor is not None else None, now),
        )
        await self._db().commit()
        row = await (await self._db().execute("SELECT * FROM finance_accounts WHERE monobank_account_id=? AND external_id=?", (monobank_account_id, external_id))).fetchone()
        return int(row["id"])

    async def finance_accounts(self, telegram_id: int) -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT fa.*, ma.telegram_id FROM finance_accounts fa JOIN monobank_accounts ma ON ma.id=fa.monobank_account_id WHERE ma.telegram_id=? ORDER BY fa.id", (telegram_id,))).fetchall()

    async def save_finance_jar(self, telegram_id: int, monobank_account_id: int, external_id: str, title: str, balance_minor: int, goal_minor: int, currency_code: int = 980) -> int:
        ownership = await (await self._db().execute("SELECT id FROM monobank_accounts WHERE id=? AND telegram_id=?", (monobank_account_id, telegram_id))).fetchone()
        if not ownership:
            raise ValueError("Банк Monobank не належить користувачу.")
        await self._db().execute(
            "INSERT INTO finance_jars(monobank_account_id, external_id, title, balance_minor, goal_minor, currency_code, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(monobank_account_id, external_id) DO UPDATE SET title=excluded.title, balance_minor=excluded.balance_minor, goal_minor=excluded.goal_minor, currency_code=excluded.currency_code, updated_at=excluded.updated_at",
            (monobank_account_id, external_id, title or "Скарбничка", balance_minor, goal_minor, currency_code, datetime.now().isoformat(timespec="seconds")),
        )
        await self._db().commit()
        row = await (await self._db().execute("SELECT * FROM finance_jars WHERE monobank_account_id=? AND external_id=?", (monobank_account_id, external_id))).fetchone()
        return int(row["id"])

    async def finance_jars(self, telegram_id: int) -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT fj.*, ma.telegram_id FROM finance_jars fj JOIN monobank_accounts ma ON ma.id=fj.monobank_account_id WHERE ma.telegram_id=? ORDER BY fj.title, fj.id", (telegram_id,))).fetchall()

    async def set_finance_account_alias(self, telegram_id: int, account_id: int, alias: str) -> bool:
        cur = await self._db().execute("UPDATE finance_accounts SET alias=? WHERE id=? AND monobank_account_id IN (SELECT id FROM monobank_accounts WHERE telegram_id=?)", (alias.strip() or None, account_id, telegram_id))
        await self._db().commit()
        return cur.rowcount > 0

    async def create_auto_rule_for_transaction(self, telegram_id: int, transaction_id: int, category_id: int) -> int:
        row = await (await self._db().execute("SELECT description FROM finance_transactions WHERE id=? AND telegram_id=?", (transaction_id, telegram_id))).fetchone()
        if not row or not row["description"].strip():
            raise ValueError("Для цієї транзакції немає опису.")
        rule_id = await self.save_finance_auto_rule(telegram_id, row["description"], category_id)
        await self._db().execute(
            "UPDATE finance_transactions SET category_id=? WHERE telegram_id=? AND LOWER(description) LIKE '%' || LOWER(?) || '%'",
            (category_id, telegram_id, row["description"].strip()[:120]),
        )
        await self._db().commit()
        return rule_id

    async def create_mcc_rule_for_transaction(self, telegram_id: int, transaction_id: int, category_id: int) -> int:
        row = await (await self._db().execute("SELECT mcc FROM finance_transactions WHERE id=? AND telegram_id=?", (transaction_id, telegram_id))).fetchone()
        if not row or row["mcc"] is None:
            raise ValueError("Для цієї транзакції немає MCC-коду.")
        return await self.save_finance_mcc_rule(telegram_id, int(row["mcc"]), category_id)

    async def finance_categories(self, telegram_id: int) -> list[aiosqlite.Row]:
        rows = await (await self._db().execute("SELECT * FROM finance_categories WHERE telegram_id IS NULL OR telegram_id=? ORDER BY telegram_id IS NOT NULL, name, id", (telegram_id,))).fetchall()
        unique: dict[str, aiosqlite.Row] = {}
        for row in rows:
            unique.setdefault(row["name"].casefold(), row)
        return list(unique.values())

    async def create_finance_category(self, telegram_id: int, name: str) -> int:
        name = " ".join(name.split())[:80]
        if not name:
            raise ValueError("Назва категорії не може бути порожньою.")
        existing = await (await self._db().execute("SELECT id FROM finance_categories WHERE telegram_id=? AND name=? COLLATE NOCASE", (telegram_id, name))).fetchone()
        if existing:
            return int(existing["id"])
        try:
            cur = await self._db().execute("INSERT INTO finance_categories(telegram_id, name, kind, mcc_codes) VALUES (?, ?, 'expense', '')", (telegram_id, name))
            await self._db().commit()
            return int(cur.lastrowid)
        except aiosqlite.IntegrityError as exc:
            await self._db().rollback()
            raise ValueError("Така категорія вже існує.") from exc

    async def save_finance_auto_rule(self, telegram_id: int, keyword: str, category_id: int) -> int:
        keyword = " ".join(keyword.split())[:120]
        normalized = keyword.casefold()
        if len(normalized) < 2:
            raise ValueError("Ключове слово має містити щонайменше 2 символи.")
        category = await (await self._db().execute("SELECT id FROM finance_categories WHERE id=? AND (telegram_id IS NULL OR telegram_id=?)", (category_id, telegram_id))).fetchone()
        if not category:
            raise ValueError("Категорію не знайдено.")
        now = datetime.now().isoformat(timespec="seconds")
        await self._db().execute(
            "INSERT INTO finance_auto_rules(telegram_id, keyword, keyword_normalized, mcc, category_id, created_at, updated_at) VALUES (?, ?, ?, NULL, ?, ?, ?) ON CONFLICT(telegram_id, keyword_normalized) DO UPDATE SET keyword=excluded.keyword, mcc=NULL, category_id=excluded.category_id, updated_at=excluded.updated_at",
            (telegram_id, keyword, normalized, category_id, now, now),
        )
        await self._db().commit()
        row = await (await self._db().execute("SELECT id FROM finance_auto_rules WHERE telegram_id=? AND keyword_normalized=?", (telegram_id, normalized))).fetchone()
        return int(row["id"])

    async def save_finance_mcc_rule(self, telegram_id: int, mcc: int, category_id: int) -> int:
        if not 0 <= int(mcc) <= 9999:
            raise ValueError("MCC-код має містити від 0 до 9999.")
        category = await (await self._db().execute("SELECT id FROM finance_categories WHERE id=? AND (telegram_id IS NULL OR telegram_id=?)", (category_id, telegram_id))).fetchone()
        if not category:
            raise ValueError("Категорію не знайдено.")
        keyword = f"MCC {int(mcc)}"
        normalized = keyword.casefold()
        now = datetime.now().isoformat(timespec="seconds")
        await self._db().execute(
            "INSERT INTO finance_auto_rules(telegram_id, keyword, keyword_normalized, mcc, category_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(telegram_id, keyword_normalized) DO UPDATE SET keyword=excluded.keyword, mcc=excluded.mcc, category_id=excluded.category_id, updated_at=excluded.updated_at",
            (telegram_id, keyword, normalized, int(mcc), category_id, now, now),
        )
        await self._db().commit()
        row = await (await self._db().execute("SELECT id FROM finance_auto_rules WHERE telegram_id=? AND keyword_normalized=?", (telegram_id, normalized))).fetchone()
        return int(row["id"])

    async def finance_auto_rules(self, telegram_id: int) -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT r.*, c.name AS category_name FROM finance_auto_rules r JOIN finance_categories c ON c.id=r.category_id WHERE r.telegram_id=? ORDER BY r.keyword", (telegram_id,))).fetchall()

    async def matching_finance_auto_rule(self, telegram_id: int, description: str) -> aiosqlite.Row | None:
        normalized_description = description.casefold()
        rules = await self.finance_auto_rules(telegram_id)
        return next((rule for rule in rules if rule["keyword_normalized"] in normalized_description), None)

    async def matching_finance_rule(self, telegram_id: int, description: str, mcc: int | None = None) -> aiosqlite.Row | None:
        rules = await self.finance_auto_rules(telegram_id)
        if mcc is not None:
            mcc_rule = next((rule for rule in rules if rule["mcc"] == int(mcc)), None)
            if mcc_rule:
                return mcc_rule
        normalized_description = (description or "").casefold()
        return next((rule for rule in rules if rule["mcc"] is None and rule["keyword_normalized"] in normalized_description), None)

    async def set_budget(self, telegram_id: int, name: str, needs: float, wants: float, savings: float) -> None:
        if min(needs, wants, savings) < 0 or round(needs + wants + savings, 2) != 100:
            raise ValueError("Частки бюджету мають бути невід’ємними та разом дорівнювати 100%.")
        db = self._db()
        await db.execute("UPDATE finance_budgets SET is_active=0 WHERE telegram_id=?", (telegram_id,))
        await db.execute(
            "INSERT INTO finance_budgets(telegram_id, name, needs_percent, wants_percent, savings_percent, is_active) VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(telegram_id, name) DO UPDATE SET needs_percent=excluded.needs_percent, wants_percent=excluded.wants_percent, savings_percent=excluded.savings_percent, is_active=1",
            (telegram_id, name, needs, wants, savings),
        )
        await db.commit()

    async def active_budget(self, telegram_id: int) -> aiosqlite.Row | None:
        return await (await self._db().execute("SELECT * FROM finance_budgets WHERE telegram_id=? AND is_active=1 LIMIT 1", (telegram_id,))).fetchone()

    async def add_finance_transaction(self, telegram_id: int, values: dict[str, Any]) -> int | None:
        db = self._db()
        try:
            cur = await db.execute(
                "INSERT INTO finance_transactions(telegram_id, monobank_account_id, mono_transaction_id, finance_account_id, amount_minor, currency_code, description, account_label, mcc, category_id, goal_id, source, occurred_at, created_at) "
                "VALUES (:telegram_id, :monobank_account_id, :mono_transaction_id, :finance_account_id, :amount_minor, :currency_code, :description, :account_label, :mcc, :category_id, :goal_id, :source, :occurred_at, :created_at)",
                {"telegram_id": telegram_id, "monobank_account_id": None, "mono_transaction_id": None, "finance_account_id": None, "currency_code": 980, "description": "", "account_label": "Monobank", "mcc": None, "category_id": None, "goal_id": None, "source": "manual", "occurred_at": datetime.now().isoformat(timespec="seconds"), "created_at": datetime.now().isoformat(timespec="seconds"), **values},

            )
            await db.commit()
            return int(cur.lastrowid)
        except aiosqlite.IntegrityError:
            await db.rollback()
            return None

    async def finance_transactions(self, telegram_id: int, limit: int = 20) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT t.*, COALESCE(fa.alias, t.account_label) AS display_account_label, c.name AS category_name FROM finance_transactions t LEFT JOIN finance_categories c ON c.id=t.category_id LEFT JOIN finance_accounts fa ON fa.id=t.finance_account_id WHERE t.telegram_id=? ORDER BY t.occurred_at DESC, t.id DESC LIMIT ?",
            (telegram_id, limit),
        )).fetchall()

    async def recent_cash_expenses(self, telegram_id: int, limit: int = 8) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT id, amount_minor, currency_code, description, occurred_at FROM finance_transactions "
            "WHERE telegram_id=? AND source='cash' AND amount_minor<0 "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (telegram_id, limit),
        )).fetchall()

    async def cash_expense_template(self, telegram_id: int, transaction_id: int) -> aiosqlite.Row | None:
        return await (await self._db().execute(
            "SELECT amount_minor, currency_code, description FROM finance_transactions "
            "WHERE id=? AND telegram_id=? AND source='cash' AND amount_minor<0",
            (transaction_id, telegram_id),
        )).fetchone()

    async def finance_summary(self, telegram_id: int, days: int = 30) -> dict[str, Any]:
        start = (datetime.now() - timedelta(days=max(1, days))).isoformat(timespec="seconds")
        db = self._db()
        totals = await (await db.execute(
            "SELECT COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS income, COALESCE(SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END), 0) AS expenses FROM finance_transactions WHERE telegram_id=? AND occurred_at>=?",
            (telegram_id, start),
        )).fetchone()
        currency_totals = await (await db.execute(
            "SELECT currency_code, COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS income, COALESCE(SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END), 0) AS expenses FROM finance_transactions WHERE telegram_id=? AND occurred_at>=? GROUP BY currency_code ORDER BY currency_code",
            (telegram_id, start),
        )).fetchall()
        categories = await (await db.execute(
            "SELECT c.id AS category_id, c.name AS name, t.currency_code, SUM(-t.amount_minor) AS amount FROM finance_transactions t JOIN finance_categories c ON c.id=t.category_id WHERE t.telegram_id=? AND t.amount_minor<0 AND t.occurred_at>=? GROUP BY c.id, c.name, t.currency_code ORDER BY amount DESC",
            (telegram_id, start),
        )).fetchall()
        uncategorized = await (await db.execute("SELECT COUNT(*) AS count FROM finance_transactions WHERE telegram_id=? AND category_id IS NULL AND occurred_at>=?", (telegram_id, start))).fetchone()
        logger.debug(
            "Finance summary user=%s start=%s categories=%s uncategorized=%s",
            telegram_id,
            start,
            [dict(row) for row in categories],
            uncategorized["count"],
        )
        return {"income": totals["income"], "expenses": totals["expenses"], "currency_totals": currency_totals, "categories": categories, "top_categories": categories[:3], "uncategorized": uncategorized["count"]}

    async def finance_report_summary(self, telegram_id: int, start_date: str, end_date: str) -> list[aiosqlite.Row]:
        start, end = local_date_range_utc_naive(start_date, end_date)
        return await (await self._db().execute(
            "SELECT currency_code, "
            "COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS income, "
            "COALESCE(SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END), 0) AS expenses "
            "FROM finance_transactions WHERE telegram_id=? AND occurred_at>=? AND occurred_at<? "
            "GROUP BY currency_code ORDER BY currency_code",
            (telegram_id, start, end),
        )).fetchall()

    async def finance_period_summary(self, telegram_id: int, start_days: int, end_days: int) -> dict[str, int]:
        now = datetime.now()
        start = (now - timedelta(days=end_days)).isoformat(timespec="seconds")
        end = (now - timedelta(days=start_days)).isoformat(timespec="seconds")
        row = await (await self._db().execute(
            "SELECT COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS income, COALESCE(SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END), 0) AS expenses FROM finance_transactions WHERE telegram_id=? AND occurred_at>=? AND occurred_at<?",
            (telegram_id, start, end),
        )).fetchone()
        currency_rows = await (await self._db().execute(
            "SELECT currency_code, COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS income, COALESCE(SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END), 0) AS expenses FROM finance_transactions WHERE telegram_id=? AND occurred_at>=? AND occurred_at<? GROUP BY currency_code ORDER BY currency_code",
            (telegram_id, start, end),
        )).fetchall()
        return {"income": int(row["income"]), "expenses": int(row["expenses"]), "currency_totals": currency_rows}

    async def uncategorized_transactions(self, telegram_id: int, limit: int = 20) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT t.*, COALESCE(fa.alias, t.account_label) AS display_account_label FROM finance_transactions t LEFT JOIN finance_accounts fa ON fa.id=t.finance_account_id WHERE t.telegram_id=? AND t.category_id IS NULL ORDER BY t.occurred_at DESC, t.id DESC LIMIT ?",
            (telegram_id, limit),
        )).fetchall()

    async def update_transaction_category(self, telegram_id: int, transaction_id: int, category_id: int) -> bool:
        db = self._db()
        transaction = await (await db.execute("SELECT id, category_id FROM finance_transactions WHERE id=? AND telegram_id=?", (transaction_id, telegram_id))).fetchone()
        if not transaction:
            logger.warning("Finance category update rejected: user=%s transaction=%s not found", telegram_id, transaction_id)
            return False
        category = await (await db.execute("SELECT id, name FROM finance_categories WHERE id=? AND (telegram_id IS NULL OR telegram_id=?)", (category_id, telegram_id))).fetchone()
        if not category:
            logger.warning("Finance category update rejected: user=%s category=%s not accessible", telegram_id, category_id)
            return False
        cur = await db.execute("UPDATE finance_transactions SET category_id=? WHERE id=? AND telegram_id=?", (category_id, transaction_id, telegram_id))
        await db.commit()
        updated = cur.rowcount > 0
        transaction = await (await db.execute("SELECT id, category_id, description FROM finance_transactions WHERE id=? AND telegram_id=?", (transaction_id, telegram_id))).fetchone()
        logger.info(
            "Finance category update user=%s transaction=%s category=%s (%s) updated=%s stored=%s",
            telegram_id,
            transaction_id,
            category_id,
            category["name"],
            updated,
            dict(transaction) if transaction else None,
        )
        return updated

    async def active_finance_goals(self, telegram_id: int) -> list[aiosqlite.Row]:
        return await self.goals(telegram_id, active_only=True)

    async def link_finance_transaction_goal(self, telegram_id: int, transaction_id: int, goal_id: int) -> aiosqlite.Row | None:
        db = self._db()
        transaction = await (await db.execute("SELECT * FROM finance_transactions WHERE id=? AND telegram_id=? AND amount_minor>0", (transaction_id, telegram_id))).fetchone()
        goal = await (await db.execute("SELECT * FROM goals WHERE id=? AND telegram_id=? AND status='active'", (goal_id, telegram_id))).fetchone()
        if not transaction or not goal:
            return None
        await db.execute("UPDATE finance_transactions SET goal_id=? WHERE id=? AND telegram_id=?", (goal_id, transaction_id, telegram_id))
        await db.commit()
        return await (await db.execute("SELECT t.*, g.title AS goal_title FROM finance_transactions t JOIN goals g ON g.id=t.goal_id WHERE t.id=? AND t.telegram_id=?", (transaction_id, telegram_id))).fetchone()

    async def goal_progress(self, telegram_id: int, goal_id: int) -> float | None:
        row = next((goal for goal in await self.goals(telegram_id, active_only=False) if goal["id"] == goal_id), None)
        return float(row["calculated_current_value"]) if row else None

    async def create_goal(self, telegram_id: int, values: dict[str, Any]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        cur = await self._db().execute(
            "INSERT INTO goals(telegram_id, title, description, goal_type, target_value, current_value, target_date, status, created_at, updated_at) VALUES (:telegram_id, :title, :description, :goal_type, :target_value, :current_value, :target_date, 'active', :created_at, :updated_at)",
            {"telegram_id": telegram_id, "description": "", "goal_type": "life", "current_value": 0, "target_date": None, "created_at": now, "updated_at": now, **values},
        )
        await self._db().commit()
        return int(cur.lastrowid)

    async def goals(self, telegram_id: int, active_only: bool = True) -> list[aiosqlite.Row]:
        query = "SELECT * FROM goals WHERE telegram_id=?"
        args: list[Any] = [telegram_id]
        if active_only:
            query += " AND status='active'"
        query += " ORDER BY datetime(created_at) DESC, id DESC"
        rows = await (await self._db().execute(query, args)).fetchall()
        if not rows:
            return []

        goal_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in goal_ids)
        explicit_rows = await (await self._db().execute(
            f"SELECT goal_id, COALESCE(SUM(ABS(amount_minor)), 0) / 100.0 AS amount FROM finance_transactions WHERE telegram_id=? AND goal_id IN ({placeholders}) GROUP BY goal_id",
            [telegram_id, *goal_ids],
        )).fetchall()
        explicit = {int(row["goal_id"]): float(row["amount"]) for row in explicit_rows}

        savings_rows = await (await self._db().execute(
            "SELECT t.amount_minor FROM finance_transactions t JOIN finance_categories c ON c.id=t.category_id WHERE t.telegram_id=? AND t.goal_id IS NULL AND c.name LIKE '%Накопичення%' ORDER BY datetime(t.occurred_at) DESC, t.id DESC",
            (telegram_id,),
        )).fetchall()
        allocated = {goal_id: 0.0 for goal_id in goal_ids}
        for transaction in savings_rows:
            remaining_amount = abs(int(transaction["amount_minor"])) / 100.0
            for row in rows:
                goal_id = int(row["id"])
                available = max(0.0, float(row["target_value"]) - float(row["current_value"]) - explicit.get(goal_id, 0.0) - allocated[goal_id])
                if available <= 0:
                    continue
                applied = min(remaining_amount, available)
                allocated[goal_id] += applied
                remaining_amount -= applied
                if remaining_amount <= 0:
                    break

        result = []
        for row in rows:
            goal_id = int(row["id"])
            progress = float(row["current_value"]) + explicit.get(goal_id, 0.0) + allocated[goal_id]
            item = dict(row)
            item["calculated_current_value"] = progress
            item["calculated_status"] = "completed" if progress >= float(row["target_value"]) else row["status"]
            result.append(item)
        return result

    async def update_goal_progress(self, telegram_id: int, goal_id: int, current_value: float) -> bool:
        cur = await self._db().execute("UPDATE goals SET current_value=?, status=CASE WHEN ?>=target_value THEN 'completed' ELSE 'active' END, updated_at=? WHERE id=? AND telegram_id=?", (current_value, current_value, datetime.now().isoformat(timespec="seconds"), goal_id, telegram_id))
        await self._db().commit()
        return cur.rowcount > 0

    async def add_goal_contribution(self, telegram_id: int, goal_id: int, amount: float) -> bool:
        if amount <= 0:
            raise ValueError("Внесок має бути більшим за нуль.")
        goal = await (await self._db().execute("SELECT current_value FROM goals WHERE id=? AND telegram_id=? AND status='active'", (goal_id, telegram_id))).fetchone()
        if not goal:
            return False
        return await self.update_goal_progress(telegram_id, goal_id, float(goal["current_value"]) + amount)

    async def delete_goal(self, telegram_id: int, goal_id: int) -> bool:
        cur = await self._db().execute("DELETE FROM goals WHERE id=? AND telegram_id=?", (goal_id, telegram_id))
        await self._db().commit()
        return cur.rowcount > 0

    async def create_habit(self, telegram_id: int, values: dict[str, Any]) -> int:
        cur = await self._db().execute(
            "INSERT INTO habits(telegram_id, name, frequency, reminder_time, active, created_at) VALUES (:telegram_id, :name, :frequency, :reminder_time, 1, :created_at)",
            {"telegram_id": telegram_id, "frequency": "daily", "reminder_time": None, "created_at": datetime.now().isoformat(timespec="seconds"), **values},
        )
        await self._db().commit()
        return int(cur.lastrowid)

    async def habits(self, telegram_id: int) -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT * FROM habits WHERE telegram_id=? AND active=1 ORDER BY id", (telegram_id,))).fetchall()

    async def set_habit_reminder(self, telegram_id: int, habit_id: int, time_value: str | None) -> bool:
        cursor = await self._db().execute(
            "UPDATE habits SET reminder_time=?, last_reminder_date=NULL WHERE id=? AND telegram_id=? AND active=1",
            (time_value, habit_id, telegram_id),
        )
        await self._db().commit()
        return cursor.rowcount == 1

    async def active_habit_reminder_count(self) -> int:
        row = await (await self._db().execute(
            "SELECT COUNT(*) AS count FROM habits WHERE active=1 AND reminder_time IS NOT NULL",
        )).fetchone()
        return int(row["count"])

    async def due_habit_reminders(self, time_value: str, local_date: str) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT h.id, h.telegram_id, h.name FROM habits h JOIN telegram_accounts a USING(telegram_id) "
            "WHERE h.active=1 AND h.reminder_time=? AND (h.last_reminder_date IS NULL OR h.last_reminder_date<>?) "
            "AND (a.access_status='approved' OR a.is_admin=1) AND a.access_status<>'banned' "
            "AND NOT EXISTS (SELECT 1 FROM habit_logs l WHERE l.habit_id=h.id AND l.log_date=? AND l.completed=1)",
            (time_value, local_date, local_date),
        )).fetchall()

    async def claim_habit_reminder(self, habit_id: int, telegram_id: int, time_value: str, local_date: str) -> bool:
        cursor = await self._db().execute(
            "UPDATE habits SET last_reminder_date=? WHERE id=? AND telegram_id=? AND active=1 AND reminder_time=? "
            "AND (last_reminder_date IS NULL OR last_reminder_date<>?)",
            (local_date, habit_id, telegram_id, time_value, local_date),
        )
        await self._db().commit()
        return cursor.rowcount == 1

    async def sqlite_snapshot(self) -> bytes:
        """Return a transactionally consistent SQLite snapshot including every bot table."""
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as target_file:
            target_path = target_file.name
        try:
            target = sqlite3.connect(target_path)
            try:
                await self._db().backup(target)
            finally:
                target.close()
            with open(target_path, "rb") as snapshot_file:
                return snapshot_file.read()
        finally:
            Path(target_path).unlink(missing_ok=True)

    async def user_preferences(self, telegram_id: int) -> aiosqlite.Row | None:
        return await (await self._db().execute(
            "SELECT * FROM user_preferences WHERE telegram_id=?",
            (telegram_id,),
        )).fetchone()

    async def set_daily_summary_time(self, telegram_id: int, time_value: str | None) -> None:
        await self._db().execute(
            "INSERT INTO user_preferences(telegram_id, daily_summary_time) VALUES (?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET daily_summary_time=excluded.daily_summary_time",
            (telegram_id, time_value),
        )
        await self._db().commit()

    async def due_daily_summary_users(self, time_value: str, local_date: str) -> list[int]:
        rows = await (await self._db().execute(
            "SELECT p.telegram_id FROM user_preferences p JOIN telegram_accounts a USING (telegram_id) "
            "WHERE p.daily_summary_time=? AND (p.last_daily_summary_date IS NULL OR p.last_daily_summary_date<>?) "
            "AND (a.access_status='approved' OR a.is_admin=1) AND a.access_status<>'banned'",
            (time_value, local_date),
        )).fetchall()
        return [int(row["telegram_id"]) for row in rows]

    async def active_daily_summary_count(self) -> int:
        row = await (await self._db().execute(
            "SELECT COUNT(*) AS count FROM user_preferences WHERE daily_summary_time IS NOT NULL",
        )).fetchone()
        return int(row["count"])

    async def backup_schedule(self):
        row = await (await self._db().execute("SELECT * FROM bot_backup_settings WHERE id=1")).fetchone()
        return row

    async def set_backup_schedule(self, frequency: str, send_time: str = "20:00", weekday: int = 0, monthday: int = 1, baseline_key: str | None = None) -> None:
        if frequency not in {"off", "daily", "weekly", "monthly"}:
            raise ValueError("Unsupported backup frequency")
        await self._db().execute(
            "UPDATE bot_backup_settings SET frequency=?, send_time=?, weekday=?, monthday=?, last_attempt_key=?, last_success_key=?, last_attempt_at=?, last_error=NULL WHERE id=1",
            (frequency, send_time, weekday, monthday, baseline_key, baseline_key, datetime.now().astimezone().isoformat(timespec="seconds") if baseline_key else None),
        )
        await self._db().commit()

    async def claim_backup_attempt(self, occurrence_key: str, now_iso: str) -> bool:
        db = self._db()
        row = await (await db.execute("SELECT last_attempt_key, last_attempt_at, last_success_key FROM bot_backup_settings WHERE id=1")).fetchone()
        if row and row["last_success_key"] == occurrence_key:
            return False
        if row and row["last_attempt_key"] == occurrence_key and row["last_attempt_at"]:
            try:
                attempted = datetime.fromisoformat(row["last_attempt_at"])
                if (datetime.fromisoformat(now_iso) - attempted).total_seconds() < 3600:
                    return False
            except ValueError:
                pass
        await db.execute("UPDATE bot_backup_settings SET last_attempt_key=?, last_attempt_at=?, last_error=NULL WHERE id=1", (occurrence_key, now_iso))
        await db.commit()
        return True

    async def record_backup_result(self, success: bool, occurrence_key: str, error: str | None = None) -> None:
        if success:
            await self._db().execute("UPDATE bot_backup_settings SET last_success_key=?, last_success_at=?, last_error=NULL WHERE id=1", (occurrence_key, datetime.now().astimezone().isoformat(timespec="seconds")))
        else:
            await self._db().execute("UPDATE bot_backup_settings SET last_error=? WHERE id=1", ((error or "Помилка відправлення")[:500],))
        await self._db().commit()

    async def claim_daily_summary(self, telegram_id: int, time_value: str, local_date: str) -> bool:
        cursor = await self._db().execute(
            "UPDATE user_preferences SET last_daily_summary_date=? WHERE telegram_id=? "
            "AND daily_summary_time=? AND (last_daily_summary_date IS NULL OR last_daily_summary_date<>?)",
            (local_date, telegram_id, time_value, local_date),
        )
        await self._db().commit()
        return cursor.rowcount == 1

    async def habit_period_summary(self, telegram_id: int, start_date: str, end_date: str) -> dict[str, int]:
        habits = await (await self._db().execute(
            "SELECT id, substr(created_at, 1, 10) AS created_date FROM habits "
            "WHERE telegram_id=? AND active=1 ORDER BY id",
            (telegram_id,),
        )).fetchall()
        if not habits:
            return {"habits": 0, "completed": 0, "opportunities": 0}
        logs = await (await self._db().execute(
            "SELECT hl.habit_id, COUNT(*) AS completed FROM habit_logs hl "
            "JOIN habits h ON h.id=hl.habit_id "
            "WHERE h.telegram_id=? AND h.active=1 AND hl.completed=1 "
            "AND hl.log_date BETWEEN ? AND ? GROUP BY hl.habit_id",
            (telegram_id, start_date, end_date),
        )).fetchall()
        completed_by_habit = {int(row["habit_id"]): int(row["completed"]) for row in logs}
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        opportunities = sum(
            max(0, (end - max(start, date.fromisoformat(row["created_date"]))).days + 1)
            for row in habits
        )
        return {
            "habits": len(habits),
            "completed": sum(completed_by_habit.values()),
            "opportunities": opportunities,
        }

    async def delete_habit(self, telegram_id: int, habit_id: int) -> bool:
        cur = await self._db().execute("DELETE FROM habits WHERE id=? AND telegram_id=?", (habit_id, telegram_id))
        await self._db().commit()
        return cur.rowcount > 0

    async def log_habit(self, telegram_id: int, habit_id: int, log_date: str) -> tuple[bool, int]:
        db = self._db()
        habit = await (await db.execute("SELECT * FROM habits WHERE id=? AND telegram_id=? AND active=1", (habit_id, telegram_id))).fetchone()
        if not habit:
            return False, 0
        cur = await db.execute("INSERT OR IGNORE INTO habit_logs(habit_id, log_date, completed, created_at) VALUES (?, ?, 1, ?)", (habit_id, log_date, datetime.now().isoformat(timespec="seconds")))
        await db.commit()
        if cur.rowcount == 0:
            return False, await self.habit_streak(habit_id, log_date)
        return True, await self.habit_streak(habit_id, log_date)

    async def toggle_habit(self, telegram_id: int, habit_id: int, log_date: str) -> tuple[bool, int]:
        db = self._db()
        habit = await (await db.execute("SELECT id FROM habits WHERE id=? AND telegram_id=? AND active=1", (habit_id, telegram_id))).fetchone()
        if not habit:
            return False, 0
        existing = await (await db.execute("SELECT id FROM habit_logs WHERE habit_id=? AND log_date=?", (habit_id, log_date))).fetchone()
        if existing:
            await db.execute("DELETE FROM habit_logs WHERE id=?", (existing["id"],))
            await db.commit()
            return False, await self.habit_streak(habit_id, log_date)
        await db.execute("INSERT INTO habit_logs(habit_id, log_date, completed, created_at) VALUES (?, ?, 1, ?)", (habit_id, log_date, datetime.now().isoformat(timespec="seconds")))
        await db.commit()
        return True, await self.habit_streak(habit_id, log_date)

    async def habit_completed(self, telegram_id: int, habit_id: int, log_date: str) -> bool:
        row = await (await self._db().execute("SELECT 1 FROM habit_logs hl JOIN habits h ON h.id=hl.habit_id WHERE hl.habit_id=? AND h.telegram_id=? AND hl.log_date=? AND hl.completed=1", (habit_id, telegram_id, log_date))).fetchone()
        return row is not None

    async def habit_streak(self, habit_id: int, ending_date: str) -> int:
        rows = await (await self._db().execute("SELECT log_date FROM habit_logs WHERE habit_id=? AND completed=1 AND log_date<=? ORDER BY log_date DESC", (habit_id, ending_date))).fetchall()
        streak = 0
        expected = date.fromisoformat(ending_date)
        for row in rows:
            if date.fromisoformat(row["log_date"]) != expected:
                break
            streak += 1
            expected -= timedelta(days=1)
        return streak

    async def habit_week_status(self, telegram_id: int, habit_id: int, week_start: str) -> list[bool]:
        week_end = date.fromisoformat(week_start) + timedelta(days=6)
        rows = await (await self._db().execute(
            "SELECT log_date FROM habit_logs hl JOIN habits h ON h.id=hl.habit_id WHERE hl.habit_id=? AND h.telegram_id=? AND hl.completed=1 AND hl.log_date BETWEEN ? AND ?",
            (habit_id, telegram_id, week_start, week_end.isoformat()),
        )).fetchall()
        completed = {row["log_date"] for row in rows}
        return [(date.fromisoformat(week_start) + timedelta(days=offset)).isoformat() in completed for offset in range(7)]

    async def reflection_entry(self, telegram_id: int, entry_date: str) -> aiosqlite.Row | None:
        return await (await self._db().execute(
            "SELECT * FROM reflection_entries WHERE telegram_id=? AND entry_date=?",
            (telegram_id, entry_date),
        )).fetchone()

    async def reflection_entries(self, telegram_id: int, limit: int = 14) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT * FROM reflection_entries WHERE telegram_id=? ORDER BY entry_date DESC LIMIT ?",
            (telegram_id, limit),
        )).fetchall()

    async def reflection_entries_between(self, telegram_id: int, start_date: str, end_date: str) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT * FROM reflection_entries WHERE telegram_id=? AND entry_date BETWEEN ? AND ? ORDER BY entry_date",
            (telegram_id, start_date, end_date),
        )).fetchall()

    async def save_reflection(self, telegram_id: int, entry_date: str, answers: dict[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        await self._db().execute(
            """INSERT INTO reflection_entries(telegram_id, entry_date, good, challenge, self_care, tomorrow, mood, energy, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id, entry_date) DO UPDATE SET
                   good=excluded.good, challenge=excluded.challenge, self_care=excluded.self_care,
                   tomorrow=excluded.tomorrow, mood=excluded.mood, energy=excluded.energy, updated_at=excluded.updated_at""",
            (telegram_id, entry_date, answers.get("good", ""), answers.get("challenge", ""), answers.get("self_care", ""), answers.get("tomorrow", ""), answers.get("mood"), answers.get("energy"), now, now),
        )
        await self._db().commit()

    async def weekly_reflection(self, telegram_id: int, week_start: str) -> aiosqlite.Row | None:
        return await (await self._db().execute(
            "SELECT * FROM weekly_reflections WHERE telegram_id=? AND week_start=?",
            (telegram_id, week_start),
        )).fetchone()

    async def weekly_reflections(self, telegram_id: int, limit: int = 12) -> list[aiosqlite.Row]:
        return await (await self._db().execute(
            "SELECT * FROM weekly_reflections WHERE telegram_id=? ORDER BY week_start DESC LIMIT ?",
            (telegram_id, limit),
        )).fetchall()

    async def save_weekly_reflection(self, telegram_id: int, week_start: str, answers: dict[str, str]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        await self._db().execute(
            """INSERT INTO weekly_reflections(telegram_id, week_start, energy_sources, patterns, proud_of, next_week, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id, week_start) DO UPDATE SET
                   energy_sources=excluded.energy_sources, patterns=excluded.patterns,
                   proud_of=excluded.proud_of, next_week=excluded.next_week, updated_at=excluded.updated_at""",
            (telegram_id, week_start, answers.get("energy_sources", ""), answers.get("patterns", ""), answers.get("proud_of", ""), answers.get("next_week", ""), now, now),
        )
        await self._db().commit()

    async def delete_weekly_reflection(self, telegram_id: int, week_start: str) -> bool:
        cur = await self._db().execute(
            "DELETE FROM weekly_reflections WHERE telegram_id=? AND week_start=?",
            (telegram_id, week_start),
        )
        await self._db().commit()
        return cur.rowcount > 0

    async def delete_reflection_entry(self, telegram_id: int, entry_date: str) -> bool:
        cur = await self._db().execute(
            "DELETE FROM reflection_entries WHERE telegram_id=? AND entry_date=?",
            (telegram_id, entry_date),
        )
        await self._db().commit()
        return cur.rowcount > 0

    async def export_all(self, owner_id: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        own_queries = {
            "profiles": ("SELECT * FROM profiles WHERE owner_telegram_id=?", (owner_id,)),
            "foods": ("SELECT * FROM food_database WHERE owner_telegram_id=?", (owner_id,)),
            "preferences": ("SELECT telegram_id, daily_summary_time, last_daily_summary_date FROM user_preferences WHERE telegram_id=?", (owner_id,)),
            "finance_categories": ("SELECT * FROM finance_categories WHERE telegram_id IS NULL OR telegram_id=?", (owner_id,)),
            "finance_budgets": ("SELECT * FROM finance_budgets WHERE telegram_id=?", (owner_id,)),
            "finance_transactions": ("SELECT t.*, c.name AS category_name FROM finance_transactions t LEFT JOIN finance_categories c ON c.id=t.category_id WHERE t.telegram_id=?", (owner_id,)),
            "finance_goals": ("SELECT * FROM goals WHERE telegram_id=?", (owner_id,)),
            "habits": ("SELECT * FROM habits WHERE telegram_id=?", (owner_id,)),
            "reflections": ("SELECT * FROM reflection_entries WHERE telegram_id=? ORDER BY entry_date", (owner_id,)),
            "weekly_reflections": ("SELECT * FROM weekly_reflections WHERE telegram_id=? ORDER BY week_start", (owner_id,)),
            "monobank_accounts": ("SELECT id, telegram_id, token_hint, created_at, updated_at FROM monobank_accounts WHERE telegram_id=?", (owner_id,)),
            "auto_rules": ("SELECT * FROM finance_auto_rules WHERE telegram_id=?", (owner_id,)),
        }
        for table, (query, params) in own_queries.items():
            rows = await (await self._db().execute(query, params)).fetchall()
            result[table] = [dict(row) for row in rows]
        profile_ids = [row["id"] for row in await (await self._db().execute("SELECT id FROM profiles WHERE owner_telegram_id=?", (owner_id,))).fetchall()]
        for table in ("food_log", "weight_log", "activity_log", "sleep_log", "meal_templates", "training_programs"):
            result[table] = []
            for profile_id in profile_ids:
                rows = await (await self._db().execute(f"SELECT * FROM {table} WHERE profile_id=?", (profile_id,))).fetchall()
                result[table].extend(dict(row) for row in rows)
        result["training_days"] = []
        result["training_program_exercises"] = []
        result["training_sessions"] = []
        result["training_exercise_logs"] = []
        for profile_id in profile_ids:
            program_ids = [r["id"] for r in await (await self._db().execute("SELECT id FROM training_programs WHERE profile_id=?", (profile_id,))).fetchall()]
            for program_id in program_ids:
                days = await (await self._db().execute("SELECT * FROM training_days WHERE program_id=?", (program_id,))).fetchall()
                result["training_days"].extend(dict(r) for r in days)
                for day in days:
                    exercises = await (await self._db().execute("SELECT * FROM training_program_exercises WHERE day_id=?", (day["id"],))).fetchall()
                    result["training_program_exercises"].extend(dict(r) for r in exercises)
            sessions = await (await self._db().execute("SELECT * FROM training_sessions WHERE profile_id=?", (profile_id,))).fetchall()
            result["training_sessions"].extend(dict(r) for r in sessions)
            for session in sessions:
                logs = await (await self._db().execute("SELECT * FROM training_exercise_logs WHERE session_id=?", (session["id"],))).fetchall()
                result["training_exercise_logs"].extend(dict(r) for r in logs)
        result["custom_training_exercises"] = [dict(r) for r in await (await self._db().execute("SELECT * FROM training_exercises WHERE owner_telegram_id=?", (owner_id,))).fetchall()]
        habit_ids = [row["id"] for row in await (await self._db().execute("SELECT id FROM habits WHERE telegram_id=?", (owner_id,))).fetchall()]
        result["habit_logs"] = []
        for habit_id in habit_ids:
            rows = await (await self._db().execute("SELECT * FROM habit_logs WHERE habit_id=?", (habit_id,))).fetchall()
            result["habit_logs"].extend(dict(row) for row in rows)
        mono_ids = [row["id"] for row in await (await self._db().execute("SELECT id FROM monobank_accounts WHERE telegram_id=?", (owner_id,))).fetchall()]
        for table in ("finance_accounts", "finance_jars"):
            result[table] = []
            for mono_id in mono_ids:
                rows = await (await self._db().execute(f"SELECT * FROM {table} WHERE monobank_account_id=?", (mono_id,))).fetchall()
                result[table].extend(dict(row) for row in rows)
        return result
