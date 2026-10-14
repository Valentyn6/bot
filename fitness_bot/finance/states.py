from aiogram.fsm.state import State, StatesGroup


class FinanceTokenFlow(StatesGroup):
    token = State()


class CashFlow(StatesGroup):
    amount = State()
    description = State()


class BudgetFlow(StatesGroup):
    needs = State()
    wants = State()
    savings = State()


class CardAliasFlow(StatesGroup):
    alias = State()


class CustomCategoryFlow(StatesGroup):
    name = State()
