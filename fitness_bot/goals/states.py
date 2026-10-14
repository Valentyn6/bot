from aiogram.fsm.state import State, StatesGroup


class GoalFlow(StatesGroup):
    title = State()
    goal_type = State()
    target_value = State()
    target_date = State()


class GoalContributionFlow(StatesGroup):
    amount = State()


class HabitFlow(StatesGroup):
    name = State()
    reminder_time = State()
