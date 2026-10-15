from aiogram.fsm.state import State, StatesGroup


class TrainingFlow(StatesGroup):
    program_title = State()
    day_title = State()
    exercise_data = State()
    day_edit = State()
    custom_exercise = State()
    weight = State()
