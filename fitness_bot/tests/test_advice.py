from fitness_bot.bot import advice_candidate


def product(unit: str, **values):
    return {
        "id": 1,
        "name": f"Тестовий продукт {unit}",
        "unit": unit,
        "protein": values.get("protein", 20),
        "fat": values.get("fat", 5),
        "carbs": values.get("carbs", 10),
        "calories": values.get("calories", 150),
    }


def test_advice_supports_grams():
    candidate = advice_candidate(
        product("г", protein=21, fat=2, carbs=0, calories=110),
        {"calories": 500, "protein": 40, "fat": 20, "carbs": 30},
        False,
    )

    assert candidate is not None
    _, row, quantity, nutrients, _ = candidate
    assert row["unit"] == "г"
    assert 10 <= quantity <= 400
    assert quantity % 10 == 0
    assert nutrients["calories"] > 0


def test_advice_supports_milliliters():
    candidate = advice_candidate(
        product("мл", protein=3, fat=2, carbs=5, calories=50),
        {"calories": 300, "protein": 10, "fat": 10, "carbs": 20},
        False,
    )

    assert candidate is not None
    assert candidate[2] % 50 == 0
    assert 50 <= candidate[2] <= 500


def test_advice_supports_pieces():
    candidate = advice_candidate(
        product("шт", protein=13, fat=11, carbs=1, calories=160),
        {"calories": 500, "protein": 30, "fat": 20, "carbs": 10},
        False,
    )

    assert candidate is not None
    assert 1 <= candidate[2] <= 4
    assert candidate[2] == int(candidate[2])


def test_advice_skips_product_when_no_calories_remain():
    assert advice_candidate(
        product("г"),
        {"calories": 0, "protein": 20, "fat": 5, "carbs": 10},
        False,
    ) is None
