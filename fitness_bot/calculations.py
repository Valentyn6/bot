from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .constants import (
    MAX_AGE,
    MAX_HEIGHT_CM,
    MAX_WEIGHT_KG,
    MIN_AGE,
    MIN_HEIGHT_CM,
    MIN_WEIGHT_KG,
)


class ValidationError(ValueError):
    """Помилка вхідних даних користувача."""


def _validate_range(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} має бути числом") from exc
    if not minimum <= number <= maximum:
        raise ValidationError(f"{name} має бути від {minimum:g} до {maximum:g}")
    return number


def validate_age(value: Any) -> int:
    return int(_validate_range(value, "Вік", MIN_AGE, MAX_AGE))


def validate_height(value: Any) -> float:
    return _validate_range(value, "Зріст", MIN_HEIGHT_CM, MAX_HEIGHT_CM)


def validate_weight(value: Any) -> float:
    return _validate_range(value, "Вага", MIN_WEIGHT_KG, MAX_WEIGHT_KG)

def parse_number(value: str) -> float:
    try:
        number = float(value.strip().replace(",", "."))
    except (AttributeError, ValueError) as exc:
        raise ValidationError("Введіть коректне число") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationError("Введіть кінцеве число")
    return number


def calculate_targets(profile: Any, weight: float, activity: float) -> dict[str, float | int]:
    weight = validate_weight(weight)
    height = validate_height(profile["height"])
    age = validate_age(profile["age"])
    try:
        activity = max(0.0, float(activity))
    except (TypeError, ValueError) as exc:
        raise ValidationError("Активність має бути числом") from exc
    sex_const = -161 if profile["sex"] == "жіноча" else 5
    bmr = round(10 * weight + 6.25 * height - 5 * age + sex_const)
    mode = profile["goal_mode"]
    signed = 0 if mode == "підтримання" else (profile["surplus_deficit"] if mode == "набір" else -profile["surplus_deficit"])
    total = round(bmr + activity + signed)
    protein = round(weight * float(profile["protein_per_kg"]))
    fat = round(weight * float(profile["fat_per_kg"]))
    carbs = max(0, round((total - protein * 4 - fat * 9) / 4))
    bmi = round(weight / (height / 100) ** 2, 1)
    return {"bmr": bmr, "total": total, "protein": protein, "fat": fat, "carbs": carbs, "bmi": bmi, "weight": weight, "activity": round(activity)}


def food_totals(rows: list[Any]) -> dict[str, float]:
    totals = {"protein": 0.0, "fat": 0.0, "carbs": 0.0, "calories": 0.0}
    for row in rows:
        multiplier = row["quantity"] if row["unit"] == "шт" else row["quantity"] / 100
        for key in totals:
            totals[key] += row[key] * multiplier
    return {key: round(value, 1) for key, value in totals.items()}


def weekly_activity(rows: list[Any], include_workouts: bool = False) -> float:
    total = sum(row["active_calories"] for row in rows)
    if include_workouts:
        total += sum(row["workout_calories"] for row in rows)
    return total / len(rows) if rows else 0


def is_training_day(row: Any) -> bool:
    return float(row["workout_calories"] or 0) > 0


def weekly_activity_by_day_type(rows: list[Any]) -> dict[str, float]:
    training_days = [row for row in rows if is_training_day(row)]
    rest_days = [row for row in rows if not is_training_day(row)]
    return {
        "training": weekly_activity(training_days, include_workouts=True),
        "rest": weekly_activity(rest_days),
    }


def weight_stats(rows: list[Any], target: float | None) -> dict[str, Any]:
    if not rows:
        return {"count": 0}
    first, current = rows[0], rows[-1]
    elapsed = max((date.fromisoformat(current["log_date"]) - date.fromisoformat(first["log_date"])).days, 1)
    change = current["weight"] - first["weight"]
    pace = change / elapsed * 7
    eta = "н/д"
    if abs(pace) >= 0.01 and target is not None:
        weeks = (target - current["weight"]) / pace
        eta = "ціль вже досягнута" if weeks < 0 else f"{weeks:.1f} тиж."
    return {"count": len(rows), "first": first["weight"], "current": current["weight"], "change": change, "pace": pace, "eta": eta, "last_date": current["log_date"]}


def normalize_date(value: str | None) -> str:
    if not value:
        return date.today().isoformat()
    return date.fromisoformat(value.strip()).isoformat()
