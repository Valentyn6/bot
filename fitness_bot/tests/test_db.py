import asyncio
import sqlite3
from datetime import date, timedelta

from fitness_bot.db import Database


def test_food_logs_are_isolated_by_diet_type(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "fitness.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        profile_id = await database.create_profile(1)
        profile = await database.active_profile(1)
        assert profile["diet_mode"] == "single"
        assert profile["training_weekdays"] is None
        await database.update_profile(profile_id, diet_mode="split")
        await database.update_profile(profile_id, training_weekdays="[0, 3]")
        profile = await database.active_profile(1)
        assert profile["diet_mode"] == "split"
        assert profile["training_weekdays"] == "[0, 3]"
        product_id = await database.add_food(
            1,
            {
                "name": "Тестовий продукт",
                "protein": 10,
                "fat": 5,
                "carbs": 20,
                "calories": 165,
                "unit": "г",
                "unit_weight": None,
            },
        )

        await database.add_food_log(profile_id, "2026-01-01", "Сніданок", product_id, 100, "rest")
        await database.add_food_log(profile_id, "2026-01-01", "Сніданок", product_id, 150, "training")

        rest_rows = await database.food_day(profile_id, "2026-01-01", "rest")
        training_rows = await database.food_day(profile_id, "2026-01-01", "training")

        assert len(rest_rows) == 1
        assert rest_rows[0]["quantity"] == 100
        assert len(training_rows) == 1
        assert training_rows[0]["quantity"] == 150

        await database.close()

    asyncio.run(scenario())


def test_recent_meals_can_be_repeated_without_copying_other_meals(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "recent-meals.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        profile_id = await database.create_profile(1)
        first_product = await database.add_food(1, {
            "name": "Йогурт", "protein": 5, "fat": 3, "carbs": 8,
            "calories": 79, "unit": "г", "unit_weight": None,
        })
        second_product = await database.add_food(1, {
            "name": "Банан", "protein": 1, "fat": 0, "carbs": 23,
            "calories": 96, "unit": "г", "unit_weight": None,
        })
        source_date = (date.today() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        await database.add_food_log(profile_id, source_date, "Сніданок", first_product, 150)
        await database.add_food_log(profile_id, source_date, "Сніданок", second_product, 80)
        await database.add_food_log(profile_id, source_date, "Обід", first_product, 250)

        meals = await database.recent_food_meals(profile_id, "rest")
        breakfast = next(meal for meal in meals if meal["meal"] == "Сніданок")
        assert await database.repeat_food_meal(profile_id, breakfast["seed_id"], today) == 2
        today_rows = await database.food_day(profile_id, today)
        assert [(row["name"], row["quantity"]) for row in today_rows] == [
            ("Йогурт", 150), ("Банан", 80),
        ]
        await database.close()

    asyncio.run(scenario())


def test_habit_reminders_are_private_once_per_day_and_database_snapshot_is_valid(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "reminders.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        await database.set_access(1, "approved")
        await database.ensure_account(2)
        await database.set_access(2, "approved")
        habit_id = await database.create_habit(1, {"name": "Випити воду"})
        assert await database.set_habit_reminder(1, habit_id, "09:30")
        due = await database.due_habit_reminders("09:30", "2026-10-14")
        assert [(row["telegram_id"], row["name"]) for row in due] == [(1, "Випити воду")]
        assert await database.claim_habit_reminder(habit_id, 1, "09:30", "2026-10-14")
        assert await database.due_habit_reminders("09:30", "2026-10-14") == []
        assert not await database.set_habit_reminder(2, habit_id, "10:00")
        snapshot = await database.sqlite_snapshot()
        assert snapshot.startswith(b"SQLite format 3\x00")
        copied = sqlite3.connect(":memory:")
        copied.deserialize(snapshot)
        assert copied.execute("SELECT name FROM habits WHERE id=?", (habit_id,)).fetchone() == ("Випити воду",)
        copied.close()
        await database.close()

    asyncio.run(scenario())
