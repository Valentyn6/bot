from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from math import ceil, isfinite
from typing import Iterable

from ortools.sat.python import cp_model


NUTRIENTS = ("calories", "protein", "fat", "carbs")
MEAL_LABELS = {"breakfast": "Сніданок", "lunch": "Обід", "dinner": "Вечеря", "snack": "Перекус"}


@dataclass(frozen=True)
class MealFoodRule:
    meal_key: str
    minimum: float
    maximum: float
    step: float
    preferred: float | None = None
    required: bool = False


@dataclass(frozen=True)
class FoodOption:
    product_id: int
    name: str
    unit: str
    calories: float
    protein: float
    fat: float
    carbs: float
    rules: tuple[MealFoodRule, ...]
    weight_basis: str = "database"


@dataclass(frozen=True)
class MealOption:
    key: str
    calorie_share: float
    required: bool = True
    label: str | None = None


@dataclass(frozen=True)
class PlannedFood:
    product_id: int
    name: str
    meal: str
    quantity: float
    unit: str
    weight_basis: str
    meal_label: str


@dataclass(frozen=True)
class PlanResult:
    status: str
    items: tuple[PlannedFood, ...] = ()
    totals: dict[str, float] | None = None
    targets: dict[str, float] | None = None
    message: str = ""


def _number(value: float, field: str) -> float:
    value = float(value)
    if not isfinite(value) or value < 0:
        raise ValueError(f"Некоректне значення {field}.")
    return value


def _food_nutrient(food: FoodOption, nutrient: str, quantity: float) -> float:
    basis_value = _number(getattr(food, nutrient), nutrient)
    return basis_value * (quantity if food.unit == "шт" else quantity / 100)


_GRAIN_NAME_MARKERS = (
    "вівсян", "греч", "рис", "булгур", "макарон", "паста", "перлов", "пшоно",
    "ячн", "кускус", "кус-кус", "кіноа", "злак", "круп",
)


def rotate_grains_by_date(foods: Iterable[FoodOption], plan_date: date) -> tuple[FoodOption, ...]:
    """Keep one enabled grain/starch per meal and rotate it deterministically by date."""
    foods = tuple(foods)
    grain_ids_by_meal: dict[str, list[int]] = {}
    for food in foods:
        if any(marker in food.name.casefold() for marker in _GRAIN_NAME_MARKERS):
            for rule in food.rules:
                grain_ids_by_meal.setdefault(rule.meal_key, []).append(food.product_id)

    offsets = {"breakfast": 0, "lunch": 1, "dinner": 2, "snack": 3}
    selected_by_meal: dict[str, int] = {}
    for meal_key, ids in grain_ids_by_meal.items():
        ordered_ids = sorted(set(ids))
        if ordered_ids:
            selected_by_meal[meal_key] = ordered_ids[(plan_date.toordinal() + offsets.get(meal_key, 0)) % len(ordered_ids)]

    result = []
    for food in foods:
        is_grain = any(marker in food.name.casefold() for marker in _GRAIN_NAME_MARKERS)
        kept_rules = []
        for rule in food.rules:
            if not is_grain:
                kept_rules.append(rule)
            elif selected_by_meal.get(rule.meal_key) == food.product_id:
                # Snacks may contain a grain if the user allowed it, but should
                # not be forced to include one. Main meals use one staple.
                kept_rules.append(replace(rule, required=rule.meal_key != "snack"))
        if kept_rules:
            result.append(replace(food, rules=tuple(kept_rules)))
    return tuple(result)


def generate_day_plan(
    foods: Iterable[FoodOption],
    targets: dict[str, float],
    meals: Iterable[MealOption],
    *,
    allow_food_repeats: bool = False,
    time_limit_seconds: float = 0.75,
) -> PlanResult:
    """Find a one-day menu under explicit meal and portion restrictions.

    Food values use the units already stored by the bot: per 100 g/ml or per
    single item. Quantity steps become integer solver variables.
    """
    foods, meals = tuple(foods), tuple(meals)
    if not foods:
        return PlanResult("infeasible", message="Оберіть продукти для генератора.")
    if not meals or any(not meal.key for meal in meals):
        raise ValueError("Задайте коректні прийоми їжі.")
    if any(not isfinite(meal.calorie_share) or meal.calorie_share < 0 for meal in meals):
        raise ValueError("Частки калорій для прийомів їжі мають бути невід’ємними.")
    share_total = sum(meal.calorie_share for meal in meals)
    if share_total <= 0:
        raise ValueError("Сума часток прийомів їжі має бути більшою за нуль.")
    clean_targets = {key: _number(targets.get(key, 0), key) for key in NUTRIENTS}
    if clean_targets["calories"] <= 0:
        return PlanResult("infeasible", message="Не вдалося визначити денну ціль калорій.")

    for food in foods:
        if food.unit not in {"г", "мл", "шт"}:
            raise ValueError(f"Невідома одиниця продукту «{food.name}».")
        if not food.rules:
            raise ValueError(f"Для «{food.name}» не задано прийомів їжі.")
        for rule in food.rules:
            minimum, maximum, step = float(rule.minimum), float(rule.maximum), float(rule.step)
            if rule.meal_key not in {meal.key for meal in meals}:
                raise ValueError(f"Невідомий прийом їжі для «{food.name}».")
            if not all(isfinite(value) for value in (minimum, maximum, step)) or step <= 0 or minimum < 0 or maximum < minimum:
                raise ValueError(f"Перевірте межі порції для «{food.name}».")
            if food.unit == "шт" and (step != 1 or not minimum.is_integer() or not maximum.is_integer()):
                raise ValueError(f"Для «{food.name}» кількість має задаватися цілими штуками.")

    model = cp_model.CpModel()
    variables: dict[tuple[int, str], cp_model.IntVar] = {}
    used: dict[tuple[int, str], cp_model.IntVar] = {}
    coefficients: dict[tuple[int, str, str], int] = {}
    meals_for_food = {index: {rule.meal_key: rule for rule in food.rules} for index, food in enumerate(foods)}
    meal_terms: dict[str, list[cp_model.LinearExpr]] = {meal.key: [] for meal in meals}
    total_terms: dict[str, list[cp_model.LinearExpr]] = {nutrient: [] for nutrient in NUTRIENTS}

    for food_index, food in enumerate(foods):
        food_used_vars = []
        for meal in meals:
            rule = meals_for_food[food_index].get(meal.key)
            if rule is None:
                continue
            maximum_steps = int(float(rule.maximum) // float(rule.step))
            # A zero minimum means optional; a selected item must still have a
            # positive amount, so it cannot satisfy a required meal at zero.
            minimum_steps = max(1, int(ceil(float(rule.minimum) / float(rule.step) - 1e-9)))
            if maximum_steps < minimum_steps:
                continue
            key = (food_index, meal.key)
            quantity = model.new_int_var(0, maximum_steps, f"quantity_{food_index}_{meal.key}")
            is_used = model.new_bool_var(f"used_{food_index}_{meal.key}")
            model.add(quantity <= maximum_steps * is_used)
            model.add(quantity >= minimum_steps * is_used)
            if rule.required:
                model.add(is_used == 1)
            variables[key] = quantity
            used[key] = is_used
            food_used_vars.append(is_used)
            for nutrient in NUTRIENTS:
                per_step = _food_nutrient(food, nutrient, float(rule.step))
                coefficients[(food_index, meal.key, nutrient)] = round(per_step * 10)
                term = quantity * coefficients[(food_index, meal.key, nutrient)]
                total_terms[nutrient].append(term)
                if nutrient == "calories":
                    meal_terms[meal.key].append(term)
        if food_used_vars and not allow_food_repeats:
            model.add(sum(food_used_vars) <= 1)

    if not variables:
        return PlanResult("infeasible", message="Немає продуктів із допустимими прийомами їжі та порціями.")

    objective_terms: list[cp_model.LinearExpr] = []
    for nutrient in NUTRIENTS:
        target_scaled = round(clean_targets[nutrient] * 10)
        if target_scaled <= 0:
            continue
        expression = sum(total_terms[nutrient])
        upper_bound = 0
        for (food_index, meal_key), _ in variables.items():
            rule = meals_for_food[food_index][meal_key]
            max_steps = int(float(rule.maximum) // float(rule.step))
            upper_bound += coefficients[(food_index, meal_key, nutrient)] * max_steps
        difference = model.new_int_var(0, max(upper_bound + target_scaled, 1), f"difference_{nutrient}")
        model.add(expression - target_scaled <= difference)
        model.add(target_scaled - expression <= difference)
        priority = {"calories": 4, "protein": 3, "fat": 2, "carbs": 1}[nutrient]
        objective_terms.append(difference * max(1, round(100_000 * priority / target_scaled)))

    for meal in meals:
        eligible = [variable for (food_index, meal_key), variable in used.items() if meal_key == meal.key]
        if meal.required and not eligible:
            return PlanResult("infeasible", message=f"Для прийому їжі «{meal.label or MEAL_LABELS.get(meal.key, meal.key)}» не вибрано продуктів.")
        if meal.required:
            model.add(sum(eligible) >= 1)
        meal_target = round(clean_targets["calories"] * meal.calorie_share / share_total * 10)
        if meal_target <= 0 or not meal_terms[meal.key]:
            continue
        max_meal = sum(
            coefficients[(food_index, meal.key, "calories")] * int(float(meals_for_food[food_index][meal.key].maximum) // float(meals_for_food[food_index][meal.key].step))
            for (food_index, meal_key) in variables
            if meal_key == meal.key
        )
        difference = model.new_int_var(0, max(max_meal + meal_target, 1), f"difference_{meal.key}")
        expression = sum(meal_terms[meal.key])
        model.add(expression - meal_target <= difference)
        model.add(meal_target - expression <= difference)
        objective_terms.append(difference * max(1, round(30_000 / meal_target)))

    # Small regularizers prefer fewer items and portions near user-defined
    # preferred amounts, without overriding macro fit.
    objective_terms.extend(variable * 100 for variable in used.values())
    for (food_index, meal_key), quantity in variables.items():
        rule = meals_for_food[food_index][meal_key]
        if rule.preferred is None:
            continue
        preferred_steps = round(float(rule.preferred) / float(rule.step))
        max_steps = int(float(rule.maximum) // float(rule.step))
        distance = model.new_int_var(0, max(max_steps, preferred_steps), f"preferred_{food_index}_{meal_key}")
        model.add(quantity - preferred_steps <= distance)
        model.add(preferred_steps - quantity <= distance)
        objective_terms.append(distance * 25)

    model.minimize(sum(objective_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.05, min(float(time_limit_seconds), 2.0))
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status_code = solver.solve(model)
    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if status_code == cp_model.INFEASIBLE:
            if allow_food_repeats:
                message = "Не вдалося скласти меню з вибраних обов’язкових продуктів. Перевірте дозволені продукти для кожного основного прийому їжі."
            else:
                message = "Не вдалося розподілити продукти між обов’язковими прийомами їжі без повторів. Дозвольте повторення або виберіть окремі продукти для кожного прийому."
            return PlanResult("infeasible", message=message)
        return PlanResult("unknown", message="Оптимізатор не встиг знайти меню. Спробуйте ще раз або спростіть обмеження.")

    items: list[PlannedFood] = []
    totals = {nutrient: 0.0 for nutrient in NUTRIENTS}
    for (food_index, meal_key), quantity_var in variables.items():
        steps_count = solver.value(quantity_var)
        if steps_count <= 0:
            continue
        food = foods[food_index]
        rule = meals_for_food[food_index][meal_key]
        quantity = steps_count * float(rule.step)
        meal = next(meal for meal in meals if meal.key == meal_key)
        items.append(PlannedFood(food.product_id, food.name, meal_key, quantity, food.unit, food.weight_basis, meal.label or MEAL_LABELS.get(meal_key, meal_key)))
        for nutrient in NUTRIENTS:
            totals[nutrient] += _food_nutrient(food, nutrient, quantity)
    order = {meal.key: index for index, meal in enumerate(meals)}
    items.sort(key=lambda item: (order[item.meal], item.name.casefold()))
    return PlanResult(
        "optimal" if status_code == cp_model.OPTIMAL else "feasible",
        tuple(items),
        {key: round(value, 1) for key, value in totals.items()},
        clean_targets,
    )
