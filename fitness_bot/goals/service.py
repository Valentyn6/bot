from __future__ import annotations

from datetime import date
import math

from ..db import Database
from ..time_utils import local_today


class GoalsService:
    def __init__(self, db: Database):
        self.db = db

    async def create_goal(self, telegram_id: int, title: str, target_value: float, target_date: str | None = None, goal_type: str = "life") -> int:
        if not title.strip() or not math.isfinite(target_value) or target_value <= 0:
            raise ValueError("Потрібні назва й додатне числове значення цілі.")
        if goal_type not in {"life", "count"}:
            raise ValueError("Оберіть фінансову або кількісну ціль.")
        if target_date:
            parsed_date = date.fromisoformat(target_date)
            if parsed_date < local_today():
                raise ValueError("Дедлайн не може бути в минулому.")
        return await self.db.create_goal(telegram_id, {"title": title.strip(), "goal_type": goal_type, "target_value": target_value, "target_date": target_date})

    async def create_habit(self, telegram_id: int, name: str) -> int:
        if not name.strip() or len(name.strip()) > 60:
            raise ValueError("Назва звички має містити від 1 до 60 символів.")
        return await self.db.create_habit(telegram_id, {"name": name.strip()})

    async def complete_habit(self, telegram_id: int, habit_id: int) -> tuple[bool, int]:
        return await self.db.log_habit(telegram_id, habit_id, local_today().isoformat())
