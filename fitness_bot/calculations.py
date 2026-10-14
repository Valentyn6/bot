from __future__ import annotations

import json
from datetime import date, timedelta
import math
from typing import Any

from .constants import (
    MAX_AGE,
    MAX_HEIGHT_CM,
    MAX_WEIGHT_KG,
    MAX_ACTIVITY_KCAL,
    MIN_AGE,
    MIN_HEIGHT_CM,
    MIN_WEIGHT_KG,
)
from .time_utils import local_today


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
    activity = _validate_range(activity, "Активність", 0, MAX_ACTIVITY_KCAL)
    mode = profile["goal_mode"]
    if mode not in {"підтримання", "набір", "схуднення"}:
        raise ValidationError("Невідомий режим цілі")
    try:
        adjustment = float(profile["surplus_deficit"])
        protein_per_kg = float(profile["protein_per_kg"])
        fat_per_kg = float(profile["fat_per_kg"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("Параметри цілі та макронутрієнтів мають бути числовими") from exc
    if not math.isfinite(adjustment) or not 0 <= adjustment <= 10_000:
        raise ValidationError("Корекція калорій має бути від 0 до 10 000 ккал")
    if not math.isfinite(protein_per_kg) or not 0 <= protein_per_kg <= 10:
        raise ValidationError("Білок має бути від 0 до 10 г/кг")
    if not math.isfinite(fat_per_kg) or not 0 <= fat_per_kg <= 10:
        raise ValidationError("Жири мають бути від 0 до 10 г/кг")
    try:
        sex = profile["sex"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValidationError("Оберіть стать у профілі") from exc
    if sex not in {"чоловіча", "жіноча"}:
        raise ValidationError("Оберіть стать у профілі")
    sex_const = -161 if sex == "жіноча" else 5
    bmr = round(10 * weight + 6.25 * height - 5 * age + sex_const)
    signed = 0 if mode == "підтримання" else (adjustment if mode == "набір" else -adjustment)
    total = round(bmr + activity + signed)
    if total <= 0:
        raise ValidationError("Цільові налаштування дають нульову або від’ємну калорійність")
    protein = round(weight * protein_per_kg)
    fat = round(weight * fat_per_kg)
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
    return sum(row["active_calories"] for row in rows) / len(rows) if rows else 0


def is_training_day(row: Any) -> bool:
    return float(row["workout_calories"] or 0) > 0


def is_profile_training_day(profile: Any, activity: Any, day: date) -> bool:
    """Resolve diet day from a configured weekday schedule or legacy activity data."""
    if profile["diet_mode"] != "split":
        return False
    schedule = profile["training_weekdays"]
    if schedule is not None:
        try:
            weekdays = json.loads(schedule) if isinstance(schedule, str) else schedule
            return day.weekday() in {int(value) for value in weekdays if 0 <= int(value) <= 6}
        except (TypeError, ValueError, json.JSONDecodeError):
            # A malformed saved schedule should not crash reports; keep legacy behavior.
            pass
    return is_training_day(activity) if activity else False


def weekly_activity_by_day_type(rows: list[Any]) -> dict[str, float]:
    training_days = [row for row in rows if is_training_day(row)]
    rest_days = [row for row in rows if not is_training_day(row)]
    return {
        "training": weekly_activity(training_days),
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
        reached = (pace > 0 and current["weight"] >= target) or (pace < 0 and current["weight"] <= target)
        weeks = (target - current["weight"]) / pace
        eta = "ціль уже досягнута" if reached else (f"{weeks:.1f} тиж." if weeks >= 0 else "темп віддаляє від цілі")
    return {"count": len(rows), "first": first["weight"], "current": current["weight"], "change": change, "pace": pace, "eta": eta, "last_date": current["log_date"]}


def normalize_date(value: str | None) -> str:
    if not value:
        return local_today().isoformat()
    return date.fromisoformat(value.strip()).isoformat()
