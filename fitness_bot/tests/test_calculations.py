"""Unit тести для calculations.py"""

import pytest
from fitness_bot.calculations import (
    calculate_targets,
    food_totals,
    normalize_date,
    parse_number,
    validate_age,
    validate_height,
    validate_weight,
    weekly_activity,
    weekly_activity_by_day_type,
    weight_stats,
    ValidationError,
)
from fitness_bot.constants import MIN_AGE, MAX_AGE, MIN_HEIGHT_CM, MAX_HEIGHT_CM, MIN_WEIGHT_KG, MAX_WEIGHT_KG


class TestValidation:
    """Тести валідації параметрів."""

    def test_validate_age_valid(self):
        """Тест валідного віку."""
        validate_age(25)  # Не повинно викидати помилку
        validate_age(MIN_AGE)
        validate_age(MAX_AGE)

    def test_validate_age_too_young(self):
        """Тест мінімального віку."""
        with pytest.raises(ValidationError):
            validate_age(MIN_AGE - 1)

    def test_validate_age_too_old(self):
        """Тест максимального віку."""
        with pytest.raises(ValidationError):
            validate_age(MAX_AGE + 1)

    def test_validate_age_invalid_type(self):
        """Тест невалідного типу віку."""
        with pytest.raises(ValidationError):
            validate_age("not a number")

    def test_validate_height_valid(self):
        """Тест валідного зросту."""
        validate_height(180)
        validate_height(MIN_HEIGHT_CM)
        validate_height(MAX_HEIGHT_CM)

    def test_validate_height_too_small(self):
        """Тест мінімального зросту."""
        with pytest.raises(ValidationError):
            validate_height(MIN_HEIGHT_CM - 1)

    def test_validate_height_too_large(self):
        """Тест максимального зросту."""
        with pytest.raises(ValidationError):
            validate_height(MAX_HEIGHT_CM + 1)

    def test_validate_weight_valid(self):
        """Тест валідної ваги."""
        validate_weight(75)
        validate_weight(MIN_WEIGHT_KG)
        validate_weight(MAX_WEIGHT_KG)

    def test_validate_weight_too_light(self):
        """Тест мінімальної ваги."""
        with pytest.raises(ValidationError):
            validate_weight(MIN_WEIGHT_KG - 1)

    def test_validate_weight_too_heavy(self):
        """Тест максимальної ваги."""
        with pytest.raises(ValidationError):
            validate_weight(MAX_WEIGHT_KG + 1)


class TestParseNumber:
    """Тести парсування чисел."""

    def test_parse_integer(self):
        """Тест парсування цілого числа."""
        assert parse_number("42") == 42.0

    def test_parse_float_dot(self):
        """Тест парсування десяткового числа з крапкою."""
        assert parse_number("3.14") == 3.14

    def test_parse_float_comma(self):
        """Тест парсування десяткового числа з комою."""
        assert parse_number("3,14") == 3.14

    def test_parse_with_spaces(self):
        """Тест парсування з пробілами."""
        assert parse_number("  42.5  ") == 42.5

    def test_parse_invalid(self):
        """Тест невалідного числа."""
        with pytest.raises(ValidationError):
            parse_number("not_a_number")


class TestCalculateTargets:
    """Тести розрахунку калорійних цілей."""

    @pytest.fixture
    def valid_profile(self):
        """Фікстура валідного профіля."""
        return {
            "sex": "чоловіча",
            "height": 180,
            "age": 30,
            "goal_mode": "підтримання",
            "surplus_deficit": 0,
            "protein_per_kg": 2.0,
            "fat_per_kg": 1.0,
        }

    def test_calculate_targets_valid(self, valid_profile):
        """Тест розрахунку валідних цілей."""
        result = calculate_targets(valid_profile, 75, 300)

        assert "bmr" in result
        assert "total" in result
        assert "protein" in result
        assert "fat" in result
        assert "carbs" in result
        assert "bmi" in result

        # Логічні перевірки
        assert result["bmr"] > 0
        assert result["total"] > result["bmr"]
        assert result["protein"] > 0
        assert result["fat"] > 0
        assert result["bmi"] > 0

    def test_calculate_targets_female(self, valid_profile):
        """Тест розрахунку для жінки."""
        valid_profile["sex"] = "жіноча"
        result = calculate_targets(valid_profile, 60, 200)

        assert result["bmr"] > 0
        assert result["total"] > 0

    def test_calculate_targets_bulk_mode(self, valid_profile):
        """Тест режиму набору маси."""
        valid_profile["goal_mode"] = "набір"
        valid_profile["surplus_deficit"] = 500
        result = calculate_targets(valid_profile, 75, 300)

        # У режимі набору total повинен бути більше ніж базовий
        assert result["total"] > result["bmr"]

    def test_calculate_targets_cut_mode(self, valid_profile):
        """Тест режиму схуднення."""
        valid_profile["goal_mode"] = "схуднення"
        valid_profile["surplus_deficit"] = 500
        result = calculate_targets(valid_profile, 75, 300)

        # У режимі схуднення total повинен бути менше ніж базовий
        assert result["total"] < result["bmr"]

    def test_calculate_targets_invalid_age(self, valid_profile):
        """Тест невалідного віку."""
        valid_profile["age"] = 150
        with pytest.raises(ValidationError):
            calculate_targets(valid_profile, 75, 300)

    def test_calculate_targets_invalid_height(self, valid_profile):
        """Тест невалідного зросту."""
        valid_profile["height"] = 500
        with pytest.raises(ValidationError):
            calculate_targets(valid_profile, 75, 300)

    def test_calculate_targets_invalid_weight(self, valid_profile):
        """Тест невалідної ваги."""
        with pytest.raises(ValidationError):
            calculate_targets(valid_profile, 500, 300)


class TestFoodTotals:
    """Тести розрахунку макроелементів."""

    def test_food_totals_empty(self):
        """Тест порожнього списку."""
        result = food_totals([])
        assert result["protein"] == 0
        assert result["fat"] == 0
        assert result["carbs"] == 0
        assert result["calories"] == 0

    def test_food_totals_single_item(self):
        """Тест одного продукту."""
        rows = [
            {
                "protein": 20,
                "fat": 10,
                "carbs": 50,
                "calories": 400,
                "quantity": 100,
                "unit": "г",
            }
        ]
        result = food_totals(rows)
        assert result["protein"] == 20
        assert result["fat"] == 10
        assert result["carbs"] == 50
        assert result["calories"] == 400

    def test_food_totals_multiple_items(self):
        """Тест кількох продуктів."""
        rows = [
            {
                "protein": 20,
                "fat": 10,
                "carbs": 50,
                "calories": 400,
                "quantity": 100,
                "unit": "г",
            },
            {
                "protein": 10,
                "fat": 5,
                "carbs": 25,
                "calories": 200,
                "quantity": 100,
                "unit": "г",
            },
        ]
        result = food_totals(rows)
        assert result["protein"] == 30
        assert result["fat"] == 15
        assert result["carbs"] == 75
        assert result["calories"] == 600

    def test_food_totals_pieces(self):
        """Тест розрахунку в штуках."""
        rows = [
            {
                "protein": 5,
                "fat": 2,
                "carbs": 10,
                "calories": 80,
                "quantity": 2,
                "unit": "шт",
            }
        ]
        result = food_totals(rows)
        assert result["protein"] == 10
        assert result["fat"] == 4
        assert result["carbs"] == 20
        assert result["calories"] == 160


class TestWeeklyActivity:
    """Тести розрахунку активності."""

    def test_weekly_activity_empty(self):
        """Тест порожнього списку."""
        result = weekly_activity([])
        assert result == 0.0

    def test_weekly_activity_single_day(self):
        """Тест одного дня."""
        rows = [{"active_calories": 500, "workout_calories": 100}]
        result = weekly_activity(rows)
        assert result == 500.0

    def test_weekly_activity_multiple_days(self):
        """Тест кількох днів."""
        rows = [
            {"active_calories": 500, "workout_calories": 100},
            {"active_calories": 600, "workout_calories": 50},
        ]
        result = weekly_activity(rows)
        assert result == 550.0

    def test_weekly_activity_with_workouts(self):
        """Тест включення тренувань."""
        rows = [
            {"active_calories": 500, "workout_calories": 100},
            {"active_calories": 600, "workout_calories": 50},
        ]
        result = weekly_activity(rows, include_workouts=True)
        # (500+100 + 600+50) / 2 = 1250 / 2 = 625
        assert result == 625.0

    def test_weekly_activity_by_day_type(self):
        rows = [
            {"active_calories": 500, "workout_calories": 300},
            {"active_calories": 600, "workout_calories": 0},
            {"active_calories": 400, "workout_calories": 100},
            {"active_calories": 700, "workout_calories": 0},
        ]

        assert weekly_activity_by_day_type(rows) == {
            "training": 650.0,
            "rest": 650.0,
        }

    def test_zero_workout_calories_is_rest_day(self):
        rows = [{"active_calories": 500, "workout_calories": 0}]

        assert weekly_activity_by_day_type(rows)["training"] == 0.0
        assert weekly_activity_by_day_type(rows)["rest"] == 500.0


class TestNormalizeDate:
    """Тести нормалізації дати."""

    def test_normalize_date_valid(self):
        """Тест валідної дати."""
        result = normalize_date("2024-01-15")
        assert result == "2024-01-15"

    def test_normalize_date_none(self):
        """Тест None значення."""
        from datetime import date
        result = normalize_date(None)
        assert result == date.today().isoformat()

    def test_normalize_date_empty(self):
        """Тест порожної строки."""
        from datetime import date
        result = normalize_date("")
        assert result == date.today().isoformat()

    def test_normalize_date_with_spaces(self):
        """Тест дати з пробілами."""
        result = normalize_date("  2024-01-15  ")
        assert result == "2024-01-15"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
