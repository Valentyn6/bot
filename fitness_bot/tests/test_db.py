import asyncio

from fitness_bot.db import Database


def test_food_logs_are_isolated_by_diet_type(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "fitness.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        profile_id = await database.create_profile(1)
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
