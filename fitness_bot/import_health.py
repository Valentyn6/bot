from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class ImportErrorMessage(ValueError):
    pass


def parse_food_database(raw: bytes, filename: str = "") -> list[dict[str, Any]]:
    """Parse the bot JSON export or the expected FoodDatabase Markdown table."""
    text = raw.decode("utf-8-sig")
    if filename.lower().endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ImportErrorMessage("База продуктів має бути коректним JSON.") from exc
        if not isinstance(data, list):
            raise ImportErrorMessage("JSON не схожий на експорт бази продуктів.")
        try:
            return [_food_values(item) for item in data]
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ImportErrorMessage("JSON містить некоректний запис продукту.") from exc

    lines = text.splitlines()
    if not any(re.match(r"^#\s*fooddatabase\s*$", line.strip(), re.IGNORECASE) for line in lines):
        raise ImportErrorMessage("Це не FoodDatabase.md: відсутній заголовок # FoodDatabase.")

    expected_headers = (
        "назвапродукту", "білкиг", "жириг", "вуглеводиг",
        "калорії", "одиниця", "вага1одг",
    )
    header_index = None
    for index, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        cells = _table_cells(line)
        if tuple(_header_name(cell) for cell in cells) == expected_headers:
            header_index = index
            break
    if header_index is None or header_index + 1 >= len(lines) or not _is_separator(lines[header_index + 1]):
        raise ImportErrorMessage("Невірна структура FoodDatabase.md: не знайдено очікуваний заголовок таблиці.")

    products = []
    for line in lines[header_index + 2:]:
        if not line.strip():
            if products:
                break
            continue
        if not line.strip().startswith("|"):
            break
        cells = _table_cells(line)
        if len(cells) != 7:
            raise ImportErrorMessage("У таблиці FoodDatabase.md знайдено рядок із неправильною кількістю колонок.")
        try:
            products.append(_food_values({
                "name": cells[0],
                "protein": _number(cells[1]),
                "fat": _number(cells[2]),
                "carbs": _number(cells[3]),
                "calories": _number(cells[4]),
                "unit": cells[5],
                "unit_weight": _number(cells[6]) if cells[6] else None,
            }))
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ImportErrorMessage(f"Не вдалося прочитати рядок продукту: {line}") from exc
    if not products:
        raise ImportErrorMessage("У файлі не знайдено таблицю продуктів.")
    return products


def _number(value: str) -> float:
    return float(value.replace(",", ".").strip())


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _header_name(value: str) -> str:
    return re.sub(r"[^\w]", "", value.casefold(), flags=re.UNICODE)


def _is_separator(line: str) -> bool:
    cells = _table_cells(line)
    return len(cells) == 7 and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _food_values(item: dict[str, Any]) -> dict[str, Any]:
    values = {
        "name": str(item.get("name", "")).strip(),
        "protein": float(item["protein"]),
        "fat": float(item["fat"]),
        "carbs": float(item["carbs"]),
        "calories": float(item["calories"]),
        "unit": str(item.get("unit", "г")).strip().casefold(),
        "unit_weight": float(item["unit_weight"]) if item.get("unit_weight") not in (None, "") else None,
    }
    numeric_values = (values["protein"], values["fat"], values["carbs"], values["calories"])
    if (
        not values["name"]
        or values["unit"] not in {"г", "мл", "шт"}
        or any(not math.isfinite(value) or value < 0 for value in numeric_values)
        or values["protein"] > 1000
        or values["fat"] > 1000
        or values["carbs"] > 1000
        or values["calories"] > 10000
        or (values["unit_weight"] is not None and (not math.isfinite(values["unit_weight"]) or not 0 < values["unit_weight"] <= 10000))
    ):
        raise ImportErrorMessage("Некоректний запис продукту.")
    return values


def _date(value: str) -> str:
    return value[:10]


def parse_health_export(raw: bytes) -> dict[str, list[dict[str, Any]]]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportErrorMessage("Файл не є коректним JSON.") from exc
    activity = data.get("activity", {}).get("daily")
    body = data.get("additional", {}).get("body", {}).get("daily")
    sleep = data.get("sleep", {}).get("sessions")
    if not isinstance(activity, list) and not isinstance(body, list) and not isinstance(sleep, list):
        raise ImportErrorMessage("Нерозпізнаний формат. Потрібен JSON Health Export Kit.")
    activities = []
    for item in activity or []:
        if "date" not in item:
            continue
        activities.append({
            "log_date": _date(item["date"]),
            "steps": float(item.get("steps") or 0),
            "active_calories": float(item.get("activeEnergyKcal") or 0),
            "distance": float(item.get("distanceKm") or 0),
            "floors": float(item.get("flightsClimbed") or 0),
            "workout_count": float(item.get("workoutCount") or 0),
        })
    weights = []
    for item in body or []:
        values = item.get("values") or {}
        if item.get("date") and values.get("bodyMass") is not None:
            weights.append({"log_date": _date(item["date"]), "weight": float(values["bodyMass"])})
    sleeps: dict[str, dict[str, Any]] = {}
    for item in sleep or []:
        if not item.get("start"):
            continue
        key = _date(item["start"])
        vitals = item.get("vitals", {}).get("heartRate", {})
        current = sleeps.setdefault(key, {"log_date": key, "asleep_sec": 0, "awake_sec": 0, "duration_sec": 0, "avg_heart": vitals.get("avg"), "max_heart": vitals.get("max"), "min_heart": vitals.get("min")})
        current["asleep_sec"] += float(item.get("asleepSec") or 0)
        current["awake_sec"] += float(item.get("awakeSec") or 0)
        current["duration_sec"] += float(item.get("durationSec") or 0)
    return {"activities": activities, "weights": weights, "sleeps": list(sleeps.values())}
