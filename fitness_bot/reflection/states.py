from aiogram.fsm.state import State, StatesGroup


class ReflectionFlow(StatesGroup):
    answer = State()
