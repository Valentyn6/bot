from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS telegram_accounts (
    telegram_id INTEGER PRIMARY KEY,
    active_profile_id INTEGER,
    managed_profile_id INTEGER,
    access_status TEXT NOT NULL DEFAULT 'pending',
    is_admin INTEGER NOT NULL DEFAULT 0,
    can_manage_diet INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
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
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self._migrate_accounts()
        await self._migrate_activity_log()
        await self._migrate_food_log()
        await self.conn.commit()

    async def _migrate_accounts(self) -> None:
        columns = {row[1] for row in await (await self._db().execute("PRAGMA table_info(telegram_accounts)")).fetchall()}
        migrations = {
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

    async def configure_admin(self, telegram_id: int | None) -> int:
        db = self._db()
        if telegram_id is not None:
            await db.execute("INSERT OR IGNORE INTO telegram_accounts(telegram_id, access_status, created_at) VALUES (?, 'approved', ?)", (telegram_id, datetime.now().isoformat(timespec="seconds")))
            admin_id = telegram_id
        else:
            row = await (await db.execute("SELECT telegram_id FROM telegram_accounts WHERE is_admin=1 ORDER BY telegram_id LIMIT 1")).fetchone()
            if row:
                return row["telegram_id"]
            row = await (await db.execute("SELECT telegram_id FROM telegram_accounts ORDER BY created_at, telegram_id LIMIT 1")).fetchone()
            if row:
                admin_id = row["telegram_id"]
            else:
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
        allowed = {"name", "height", "age", "sex", "target_weight", "goal_mode", "surplus_deficit", "surplus_gain", "surplus_maintenance", "deficit_loss", "protein_per_kg", "fat_per_kg", "manual_mode", "manual_weight", "manual_activity", "workout_kcal_today"}
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
                "INSERT INTO food_log(profile_id, log_date, meal, product_id, quantity, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (profile_id, log_date, meal, product_id, quantity, datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()
        except aiosqlite.IntegrityError as exc:
            await db.rollback()
            raise ValueError("Не вдалося додати продукт: запис або продукт уже не актуальні. Оновіть раціон.") from exc

    async def food_day(self, profile_id: int, log_date: str, diet_type: str = "rest") -> list[aiosqlite.Row]:
        return await (await self._db().execute("SELECT l.*, f.name, f.protein, f.fat, f.carbs, f.calories, f.unit FROM food_log l JOIN food_database f ON f.id=l.product_id WHERE l.profile_id=? AND l.log_date=? AND l.diet_type=? ORDER BY l.id", (profile_id, log_date, diet_type))).fetchall()

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
            args.append((date.today() - timedelta(days=days - 1)).isoformat())
        query += " ORDER BY log_date"
        return await (await self._db().execute(query, args)).fetchall()

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
        await db.execute("INSERT INTO sleep_log(profile_id, log_date, asleep_sec, awake_sec, duration_sec, avg_heart, max_heart, min_heart) VALUES (:profile_id, :log_date, :asleep_sec, :awake_sec, :duration_sec, :avg_heart, :max_heart, :min_heart) ON CONFLICT(profile_id, log_date) DO UPDATE SET asleep_sec=max(sleep_log.asleep_sec, excluded.asleep_sec), awake_sec=max(sleep_log.awake_sec, excluded.awake_sec), duration_sec=max(sleep_log.duration_sec, excluded.duration_sec)", {"profile_id": profile_id, **values})
        await db.commit()

    async def export_all(self, owner_id: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for table, query in [("profiles", "SELECT * FROM profiles WHERE owner_telegram_id=?"), ("foods", "SELECT * FROM food_database WHERE owner_telegram_id=?")]:
            rows = await (await self._db().execute(query, (owner_id,))).fetchall()
            result[table] = [dict(row) for row in rows]
        profile_ids = [row["id"] for row in await (await self._db().execute("SELECT id FROM profiles WHERE owner_telegram_id=?", (owner_id,))).fetchall()]
        for table in ("food_log", "weight_log", "activity_log", "sleep_log", "meal_templates"):
            result[table] = []
            for profile_id in profile_ids:
                rows = await (await self._db().execute(f"SELECT * FROM {table} WHERE profile_id=?", (profile_id,))).fetchall()
                result[table].extend(dict(row) for row in rows)
        return result
