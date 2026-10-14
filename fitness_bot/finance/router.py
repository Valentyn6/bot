from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..calculations import parse_number
from ..db import Database
from .service import FinanceService
from .states import BudgetFlow, CardAliasFlow, CashFlow, CustomCategoryFlow, FinanceTokenFlow

router = Router(name="finance")
_db: Database | None = None


def configure(db: Database) -> None:
    global _db
    _db = db


def service() -> FinanceService:
    if _db is None:
        raise RuntimeError("Finance router is not configured")
    return FinanceService(_db)


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return keyboard([[('✖️ Скасувати', 'cancel')]])


def back_to_finance_keyboard() -> InlineKeyboardMarkup:
    return keyboard([[('← До фінансів', 'finance:menu')]])


def dashboard_menu(uncategorized: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [("🔄 Синхронізувати Monobank", "finance:sync")],
        [("💳 Рахунки", "finance:accounts"), ("📋 Операції", "finance:transactions")],
        [("📊 Динаміка", "finance:comparison")],
        [("➕ Готівкова витрата", "finance:cash")],
        [("🔁 Повторити витрату", "finance:repeat-cash")],
        [("⚙️ Налаштування фінансів", "finance:settings")],
    ]
    if uncategorized:
        rows.append([(f"🧹 Без категорії: {uncategorized}", "finance:uncategorized")])
    rows.append([("← Головне меню", "home")])
    return keyboard(rows)


def currency_label(currency_code: int | str) -> str:
    code = int(currency_code)
    return {980: "UAH · ₴", 840: "USD · $", 978: "EUR · €", 826: "GBP · £"}.get(code, f"валюта {code}")


def currency_symbol(currency_code: int | str) -> str:
    code = int(currency_code)
    return {980: "₴", 840: "$", 978: "€", 826: "£"}.get(code, f"код {code}")


def row_value(row: object, key: str, default: object = None) -> object:
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return default


def money(amount_minor: int | float, currency_code: int = 980) -> str:
    return f"{amount_minor / 100:,.2f} {currency_symbol(currency_code)}"


def progress_bar(current: int, goal: int, width: int = 10) -> str:
    progress = min(100, round(current / goal * 100)) if goal > 0 else 0
    filled = round(progress / 100 * width)
    return f"[{'▓' * filled}{'░' * (width - filled)}] {progress}%"


def dashboard_text(data: dict) -> str:
    summary = data["summary"]
    currency_totals = summary.get("currency_totals", [{"currency_code": 980, "income": summary["income"], "expenses": summary["expenses"]}])
    income_by_currency = {int(row["currency_code"]): int(row["income"]) for row in currency_totals}
    expenses_by_currency = {int(row["currency_code"]): int(row["expenses"]) for row in currency_totals}
    balance_groups = data.get("balances_by_currency", {980: int(data.get("balance_minor", 0))})
    account_groups = data.get("account_balances_by_currency", {980: int(data.get("balance_minor", 0))})
    jar_groups = data.get("jar_balances_by_currency") or {}
    balance_lines = []
    for code in sorted(set(balance_groups) | set(account_groups) | set(jar_groups)):
        balance_lines.append(
            f"• <b>{currency_label(code)}</b> · рахунки {money(account_groups.get(code, 0), code)}"
            f" · банки {money(jar_groups.get(code, 0), code)}"
            f" · разом {money(balance_groups.get(code, 0), code)}"
        )
    if not balance_lines:
        balance_lines.append("• Залишки з’являться після синхронізації рахунків.")
    balances_text = "\n".join(balance_lines)
    if data.get("info", {}).get("cached"):
        balances_text += "\n<i>Показано останні збережені залишки: Monobank зараз недоступний.</i>"

    needs, wants, savings = data["budget"]
    category_rows = summary["categories"]
    needs_names = ("продукт", "транспорт", "комуналь", "зв'язок", "здоров")
    wants_names = ("кафе", "рестора", "розваг", "дозвіл", "покуп", "одяг")
    uah_income = income_by_currency.get(980, 0)
    uah_expenses = expenses_by_currency.get(980, 0)
    uah_categories = [row for row in category_rows if int(row_value(row, "currency_code", 980)) == 980]
    needs_spent = sum(int(row["amount"]) for row in uah_categories if any(marker in row["name"].casefold() for marker in needs_names))
    wants_spent = sum(int(row["amount"]) for row in uah_categories if any(marker in row["name"].casefold() for marker in wants_names))
    leftover = max(0, uah_income - uah_expenses)
    budget_values = [("Потреби", needs, needs_spent), ("Бажання", wants, wants_spent), ("Залишок доходу", savings, leftover)]
    budget_lines = []
    if uah_income > 0:
        for name, percent, spent in budget_values:
            limit = uah_income * percent / 100
            ratio = spent / limit if limit > 0 else 0
            visual_progress = min(100, round(ratio * 100))
            filled = round(visual_progress / 10)
            state = f"перевищено на {money(spent - limit)}" if limit > 0 and spent > limit else f"залишилось {money(limit - spent)}"
            if name == "Залишок доходу":
                state = f"факт {money(spent)} із плану {money(limit)}"
            budget_lines.append(
                f"• <b>{name}</b> · план {percent:g}% ({money(limit)})\n"
                f"  [{'▓' * filled}{'░' * (10 - filled)}] {ratio * 100:.0f}% · {state}"
            )
        budget_text = "\n".join(budget_lines) + "\n<i>Потреби й бажання — оцінка за категоріями. Залишок доходу не означає, що ці кошти переказані в скарбничку.</i>"
    else:
        budget_text = "Додайте дохід у гривні за цей період, щоб порівняти витрати з бюджетним планом."

    category_lines = []
    for code in sorted({int(row_value(row, "currency_code", 980)) for row in category_rows}):
        top = [row for row in category_rows if int(row_value(row, "currency_code", 980)) == code][:3]
        category_lines.extend(f"• {escape(row['name'])} — {money(row['amount'], code)}" for row in top)
    categories = "\n".join(category_lines) if category_lines else "За останні 30 днів витрат ще немає."
    jars = data.get("jars", [])
    jar_lines = []
    for jar in jars[:5]:
        balance = int(jar["balance_minor"])
        goal = int(jar["goal_minor"])
        code = int(jar["currency_code"] or 980)
        if goal > 0:
            jar_lines.append(f"• {escape(jar['title'])} · {money(balance, code)} із {money(goal, code)} · {progress_bar(balance, goal)}")
        else:
            jar_lines.append(f"• {escape(jar['title'])} · {money(balance, code)}")
    jars_text = "\n".join(jar_lines) if jar_lines else "Скарбничок поки немає."
    uncategorized = f"\n\n⚠️ <b>Без категорії:</b> {summary['uncategorized']} операцій" if summary["uncategorized"] else ""

    flow_lines = []
    for code in sorted(set(income_by_currency) | set(expenses_by_currency)):
        income = income_by_currency.get(code, 0)
        expenses = expenses_by_currency.get(code, 0)
        net = income - expenses
        flow_lines.append(
            f"• <b>{currency_label(code)}</b>\n"
            f"  Надійшло {money(income, code)} · витрачено {money(expenses, code)}\n"
            f"  Різниця: <b>{'+' if net >= 0 else '−'}{money(abs(net), code)}</b>"
        )
    flow_text = "\n".join(flow_lines) if flow_lines else "Операцій за останні 30 днів немає."
    return (
        "💳 <b>Фінанси</b>\n<i>Операції за останні 30 днів</i>\n\n"
        "💼 <b>Залишки зараз</b>\n" + balances_text + "\n\n"
        "📊 <b>Рух коштів</b>\n" + flow_text + "\n\n"
        "🧾 <b>Найбільші категорії витрат</b>\n" + categories + "\n\n"
        "🎯 <b>Бюджетний орієнтир · гривня</b>\n" + budget_text + "\n\n"
        "🐷 <b>Скарбнички</b>\n" + jars_text + uncategorized
    )


def comparison_text(data: dict) -> str:
    current = data["current"]
    previous = data["previous"]

    def change(value: float | None, label: str) -> str:
        if value is None:
            return f"{label}: немає бази для порівняння"
        marker = "▲" if value >= 0 else "▼"
        return f"{label}: {marker} {value:+.1f}%"

    current_by_currency = current.get("currency_totals", [{"currency_code": 980, "income": current["income"], "expenses": current["expenses"]}])
    previous_by_currency = {int(row["currency_code"]): row for row in (previous.get("currency_totals") or [])}
    lines = ["📊 <b>Порівняння періодів</b>", "<i>Останні 30 днів проти попередніх 30 днів</i>"]
    for row in current_by_currency:
        code = int(row["currency_code"])
        prior = previous_by_currency.get(code, {"income": 0, "expenses": 0})
        income_delta = ((row["income"] - prior["income"]) / prior["income"] * 100) if prior["income"] else None
        expenses_delta = ((row["expenses"] - prior["expenses"]) / prior["expenses"] * 100) if prior["expenses"] else None
        lines.extend([
            "",
            f"💱 <b>{currency_label(code)}</b>",
            f"Надходження: {money(row['income'], code)} · раніше {money(prior['income'], code)}",
            f"Витрати: {money(row['expenses'], code)} · раніше {money(prior['expenses'], code)}",
            change(income_delta, "Зміна надходжень") + " · " + change(expenses_delta, "зміна витрат"),
        ])
    if not current_by_currency:
        lines.append("\nЗа ці періоди операцій немає.")
    lines.append("\n<i>Валюти показані окремо, без конвертації.</i>")
    return "\n".join(lines)


def settings_menu() -> InlineKeyboardMarkup:
    return keyboard([
        [("🔑 Змінити токен Monobank", "finance:settings:token")],
        [("🏷 Назви карток", "finance:settings:accounts")],
        [("🏷 Категорії витрат", "finance:settings:categories")],
        [("📊 Вибрати пресет бюджету", "finance:settings:budget")],
        [("🗑 Видалити токен", "finance:settings:remove")],
        [("← До фінансів", "finance:menu")],
    ])


async def allowed(event: Message | CallbackQuery) -> bool:
    if _db is None:
        return False
    account, _ = await _db.ensure_account(event.from_user.id)
    if account["access_status"] == "approved" or (account["is_admin"] and account["access_status"] != "banned"):
        return True
    text = "⛔ Доступ заблоковано." if account["access_status"] == "banned" else "⏳ Дочекайтеся підтвердження адміністратора."
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


@router.callback_query(F.data == "finance:menu")
async def finance_menu(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    stored = await _db.monobank_account(callback.from_user.id)  # type: ignore[union-attr]
    if not stored:
        await callback.message.edit_text(
            "💳 <b>Фінанси</b>\n\nMonobank ще не підключено. Підключіть рахунок, щоб бачити баланс і синхронізувати операції. Готівкові витрати можна записувати без підключення.",
            reply_markup=keyboard([
                [('🔑 Підключити Monobank', 'finance:settings:token')],
                [('➕ Готівкова витрата', 'finance:cash')],
                [('🔁 Повторити витрату', 'finance:repeat-cash')],
                [('← Головне меню', 'home')],
            ]),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    try:
        data = await service().dashboard(callback.from_user.id)
    except Exception as exc:
        await callback.answer(f"Не вдалося завантажити dashboard: {exc}", show_alert=True)
        return
    await callback.message.edit_text(dashboard_text(data), reply_markup=dashboard_menu(data["summary"]["uncategorized"]), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "finance:repeat-cash")
async def repeat_cash_menu(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    expenses = await _db.recent_cash_expenses(callback.from_user.id)  # type: ignore[union-attr]
    if not expenses:
        await callback.message.edit_text(
            "🔁 <b>Повторити готівкову витрату</b>\n\nПоки немає збережених готівкових витрат для повторення.",
            reply_markup=keyboard([[('← До фінансів', 'finance:menu')]]),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    rows = []
    for expense in expenses:
        description = (expense["description"] or "Готівкова витрата").replace("\n", " ")
        if len(description) > 32:
            description = description[:29].rstrip() + "..."
        rows.append([(
            f"{money(abs(int(expense['amount_minor'])), int(expense['currency_code'] or 980))} · {description}",
            f"finance:repeat-cash:confirm:{expense['id']}",
        )])
    rows.append([('← До фінансів', 'finance:menu')])
    await callback.message.edit_text(
        "🔁 <b>Повторити готівкову витрату</b>\n\nОберіть витрату. Перед збереженням бот попросить підтвердження.",
        reply_markup=keyboard(rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("finance:repeat-cash:confirm:"))
async def repeat_cash_confirm(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        transaction_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Операцію не знайдено.", show_alert=True)
        return
    template = await _db.cash_expense_template(callback.from_user.id, transaction_id)  # type: ignore[union-attr]
    if not template:
        await callback.answer("Цю витрату вже не знайдено.", show_alert=True)
        return
    description = escape(template["description"] or "Готівкова витрата")
    await callback.message.edit_text(
        f"Повторити витрату <b>{money(abs(int(template['amount_minor'])), int(template['currency_code'] or 980))}</b>\n"
        f"{description}?",
        reply_markup=keyboard([
            [("✅ Так, записати", f"finance:repeat-cash:do:{transaction_id}")],
            [("← До списку витрат", "finance:repeat-cash")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("finance:repeat-cash:do:"))
async def repeat_cash_do(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        transaction_id = int(callback.data.rsplit(":", 1)[1])
        await service().repeat_cash(callback.from_user.id, transaction_id)
    except (ValueError, TypeError) as exc:
        await callback.answer(str(exc) or "Не вдалося повторити витрату.", show_alert=True)
        return
    await callback.message.edit_text(
        "✅ Готівкову витрату записано ще раз.",
        reply_markup=keyboard([[('← До фінансів', 'finance:menu')]]),
    )
    await callback.answer()


@router.callback_query(F.data == "finance:comparison")
async def comparison(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        data = await service().comparison(callback.from_user.id)
    except Exception as exc:
        await callback.answer(f"Не вдалося завантажити динаміку: {exc}", show_alert=True)
        return
    await callback.message.edit_text(comparison_text(data), reply_markup=keyboard([[('← До dashboard', 'finance:menu')]]), parse_mode="HTML")
    await callback.answer()


async def show_uncategorized(callback: CallbackQuery) -> None:
    rows = await _db.uncategorized_transactions(callback.from_user.id, 20)  # type: ignore[union-attr]
    if not rows:
        await callback.message.edit_text("✅ Усі транзакції мають категорії.", reply_markup=dashboard_menu())
        return
    lines = ["🧹 <b>Операції без категорії</b>", "<i>Виберіть операцію, щоб призначити їй категорію.</i>"]
    buttons = []
    for row in rows:
        source = row["display_account_label"] or ("Готівка" if row["source"] == "cash" else "Monobank")
        code = int(row["currency_code"] or 980)
        signed_amount = ("+" if row["amount_minor"] >= 0 else "−") + money(abs(row["amount_minor"]), code)
        lines.append(f"\n<b>{row['occurred_at'][:10]} · {signed_amount}</b>\n{escape(str(source))} · {escape(row['description'] or 'Без опису')}")
        description = " ".join((row["description"] or "Без опису").split())
        max_description_length = 30
        if len(description) > max_description_length:
            description = description[:max_description_length - 1] + "…"
        buttons.append([(f"🏷 {money(row['amount_minor'], int(row['currency_code'] or 980))} · {description}", f"finance:uncategorized:tx:{row['id']}")])
    buttons.append([("← До dashboard", "finance:menu")])
    await callback.message.edit_text("\n".join(lines), reply_markup=keyboard(buttons), parse_mode="HTML")


@router.callback_query(F.data == "finance:uncategorized")
async def uncategorized(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    await show_uncategorized(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("finance:uncategorized:tx:"))
async def uncategorized_categories(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    transaction_id = int(callback.data.rsplit(":", 1)[1])
    categories = await _db.finance_categories(callback.from_user.id)  # type: ignore[union-attr]
    rows = [[(category["name"], f"finance:uncategorized:set:{transaction_id}:{category['id']}")] for category in categories]
    rows.append([("➕ Додати категорію", f"finance:add-category:{transaction_id}")])
    rows.append([("← До нерозподілених", "finance:uncategorized")])
    transaction = next((row for row in await _db.uncategorized_transactions(callback.from_user.id, 100) if row["id"] == transaction_id), None)  # type: ignore[union-attr]
    if not transaction:
        await callback.answer("Операцію не знайдено.", show_alert=True)
        return
    amount = int(transaction["amount_minor"])
    currency_code = int(transaction["currency_code"] or 980)
    signed_amount = ("+" if amount >= 0 else "−") + money(abs(amount), currency_code)
    text = f"🏷 <b>Категорія операції</b>\n{transaction['occurred_at'][:10]} · {signed_amount}\n{escape(transaction['description'] or 'Без опису')}\n\nОберіть категорію:"
    await callback.message.edit_text(text, reply_markup=keyboard(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("finance:uncategorized:set:"))
async def categorize_uncategorized(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    _, _, _, transaction_id, category_id = callback.data.split(":")
    updated = await _db.update_transaction_category(callback.from_user.id, int(transaction_id), int(category_id))  # type: ignore[union-attr]
    await callback.answer("✅ Категорію оновлено." if updated else "Транзакцію не знайдено.", show_alert=not updated)
    if updated:
        refreshed = await _db.finance_transactions(callback.from_user.id, 20)  # type: ignore[union-attr]
        refreshed_row = next((row for row in refreshed if row["id"] == transaction_id), None)
        if not refreshed_row or refreshed_row["category_id"] != category_id:
            await callback.answer("Категорію не вдалося підтвердити після збереження.", show_alert=True)
            return
        data = await service().dashboard(callback.from_user.id)
        rule_button = keyboard([
            [("🔗 Завжди застосовувати для таких платежів", f"finance:auto-rule:{transaction_id}:{category_id}")],
            [("← До нерозподілених", "finance:uncategorized")],
            [("📊 До dashboard", "finance:menu")],
        ])
        transaction = next((row for row in await _db.finance_transactions(callback.from_user.id, 100) if row["id"] == transaction_id), None)  # type: ignore[union-attr]
        if transaction and transaction["mcc"] is not None:
            rule_button.inline_keyboard.insert(1, [InlineKeyboardButton(text=f"🔢 Автокатегоризація для MCC {transaction['mcc']}", callback_data=f"finance:auto-rule-mcc:{transaction_id}:{category_id}")])
        await callback.message.edit_text("✅ Категорію оновлено.\n\n" + dashboard_text(data) + "\n\nХочете автоматично категоризувати майбутні платежі з таким самим описом?", reply_markup=rule_button, parse_mode="HTML")


@router.callback_query(F.data.startswith("finance:auto-rule:"))
async def create_auto_rule(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        _, transaction_id, category_id = callback.data.rsplit(":", 2)
        transaction_id = int(transaction_id)
        category_id = int(category_id)
    except (AttributeError, ValueError):
        await callback.answer("Некоректні дані правила автокатегоризації.", show_alert=True)
        return
    try:
        await _db.create_auto_rule_for_transaction(callback.from_user.id, transaction_id, category_id)  # type: ignore[union-attr]
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("🔗 Правило збережено")
    data = await service().dashboard(callback.from_user.id)
    await callback.message.edit_text("✅ Правило збережено.\n\n" + dashboard_text(data), reply_markup=dashboard_menu(data["summary"]["uncategorized"]), parse_mode="HTML")


@router.callback_query(F.data.startswith("finance:auto-rule-history:"))
async def create_history_auto_rule(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        prefix, transaction_id_text, category_id_text = callback.data.rsplit(":", 2)
        if prefix != "finance:auto-rule-history":
            raise ValueError("Некоректний формат правила.")
        transaction_id = int(transaction_id_text)
        category_id = int(category_id_text)
        await _db.create_auto_rule_for_transaction(callback.from_user.id, transaction_id, category_id)  # type: ignore[union-attr]
    except (ValueError, TypeError) as exc:
        await callback.answer(str(exc) or "Некоректні дані правила.", show_alert=True)
        return
    await callback.answer("🔗 Правило збережено")
    data = await service().dashboard(callback.from_user.id)
    await callback.message.edit_text("✅ Правило автокатегоризації збережено.\n\n" + dashboard_text(data), reply_markup=dashboard_menu(data["summary"]["uncategorized"]), parse_mode="HTML")


@router.callback_query(F.data.startswith("finance:auto-rule-mcc:"))
async def create_mcc_auto_rule(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        _, transaction_id, category_id = callback.data.rsplit(":", 2)
        await _db.create_mcc_rule_for_transaction(callback.from_user.id, int(transaction_id), int(category_id))  # type: ignore[union-attr]
    except (ValueError, TypeError) as exc:
        await callback.answer(str(exc) or "Некоректні дані правила.", show_alert=True)
        return
    await callback.answer("🔢 MCC-правило збережено")
    data = await service().dashboard(callback.from_user.id)
    await callback.message.edit_text("✅ MCC-правило автокатегоризації збережено.\n\n" + dashboard_text(data), reply_markup=dashboard_menu(data["summary"]["uncategorized"]), parse_mode="HTML")


@router.callback_query(F.data == "finance:settings:accounts")
async def card_aliases(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    accounts = await _db.finance_accounts(callback.from_user.id)  # type: ignore[union-attr]
    rows = [[(f"🏷 {account['alias'] or account['default_label']}", f"finance:settings:alias:{account['id']}")] for account in accounts]
    rows.append([("← До налаштувань", "finance:settings")])
    text = "🏷 <b>Назви карток</b>\nОберіть картку, щоб задати власний псевдонім." if accounts else "Ще немає синхронізованих карток. Спочатку виконайте синхронізацію."
    await callback.message.edit_text(text, reply_markup=keyboard(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("finance:settings:alias:"))
async def card_alias_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    account_id = int(callback.data.rsplit(":", 1)[1])
    await state.clear()
    await state.update_data(account_id=account_id)
    await state.set_state(CardAliasFlow.alias)
    await callback.message.edit_text("Введіть нову назву картки, наприклад: Mono Black, Готівка або Скарбничка.", reply_markup=cancel_keyboard())
    await callback.answer()


@router.message(CardAliasFlow.alias)
async def card_alias_received(message: Message, state: FSMContext) -> None:
    if not await allowed(message):
        return
    alias = (message.text or "").strip()
    if not alias:
        await message.answer("Назва не може бути порожньою.")
        return
    data = await state.get_data()
    updated = await _db.set_finance_account_alias(message.from_user.id, int(data["account_id"]), alias)  # type: ignore[union-attr]
    await state.clear()
    await message.answer("✅ Назву картки оновлено." if updated else "❌ Картку не знайдено.", reply_markup=settings_menu())


@router.callback_query(F.data.in_({"finance:token", "finance:settings:token"}))
async def token_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    await state.clear()
    await state.set_state(FinanceTokenFlow.token)
    await callback.message.edit_text("Надішліть токен Monobank одним повідомленням. Він не буде показаний у меню або логах.", reply_markup=cancel_keyboard())
    await callback.answer()


@router.message(FinanceTokenFlow.token)
async def token_received(message: Message, state: FSMContext) -> None:
    if not await allowed(message):
        return
    try:
        result = await service().save_token(message.from_user.id, message.text or "")
    except Exception as exc:
        await message.answer(f"❌ Не вдалося підключити Monobank: {escape(str(exc))}", reply_markup=cancel_keyboard())
        return
    await state.clear()
    await message.answer(f"✅ Monobank підключено для {escape(result['name'])}.\nПервинно завантажено операцій: {result['imported']}.", reply_markup=back_to_finance_keyboard())


@router.callback_query(F.data == "finance:accounts")
async def accounts(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        info = await service().account_info(callback.from_user.id)
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    lines = ["💳 <b>Рахунки та скарбнички</b>"]
    aliases = {account["external_id"]: account["alias"] for account in await _db.finance_accounts(callback.from_user.id)}  # type: ignore[union-attr]
    account_lines = []
    for account in info.get("accounts", []):
        code = int(account.get("currencyCode", 980))
        masked_pan = account.get("maskedPan") or ["Рахунок"]
        label = aliases.get(account.get("id")) or masked_pan[0]
        balance = account.get("balance")
        value = money(int(balance), code) if balance is not None else "баланс ще не отримано"
        account_lines.append(f"• <b>{escape(str(label))}</b> · {value}")
    lines.extend(["", "🏦 <b>Рахунки</b>"] + (account_lines or ["Немає синхронізованих рахунків."]))
    jar_lines = []
    for jar in info.get("jars", []):
        code = int(jar.get("currencyCode", 980))
        balance = jar.get("balance")
        goal = jar.get("goal")
        label = escape(str(jar.get("title") or "Скарбничка"))
        if balance is None:
            jar_lines.append(f"• <b>{label}</b> · баланс ще не отримано")
        elif goal:
            jar_lines.append(f"• <b>{label}</b> · {money(int(balance), code)} із {money(int(goal), code)}")
        else:
            jar_lines.append(f"• <b>{label}</b> · {money(int(balance), code)}")
    lines.extend(["", "🐷 <b>Скарбнички</b>"] + (jar_lines or ["Скарбничок немає."]))
    if info.get("cached"):
        lines.extend(["", "⚠️ Показані останні збережені дані. Не вдалося оновити їх із Monobank."])
    await callback.message.edit_text("\n".join(lines), reply_markup=keyboard([[('← До фінансів', 'finance:menu')]]), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "finance:sync")
async def sync(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        count = await service().sync(callback.from_user.id)
    except Exception as exc:
        await callback.answer(f"Помилка синхронізації: {exc}", show_alert=True)
        return
    try:
        data = await service().dashboard(callback.from_user.id)
        text = f"✅ Синхронізацію завершено. Додано нових операцій: {count}.\n\n{dashboard_text(data)}"
    except Exception:
        text = f"✅ Синхронізацію завершено. Додано нових операцій: {count}."
    uncategorized_count = 0
    if 'data' in locals():
        uncategorized_count = data["summary"]["uncategorized"]
    try:
        await callback.message.edit_text(text, reply_markup=dashboard_menu(uncategorized_count), parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise
        await callback.answer("✅ Все вже актуально!")
        return
    await callback.answer()


@router.callback_query(F.data == "finance:transactions")
async def transactions(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    rows = await _db.finance_transactions(callback.from_user.id, 12)  # type: ignore[union-attr]
    if not rows:
        text = "📋 <b>Операції</b>\n\nЗафіксованих операцій ще немає. Синхронізуйте Monobank або додайте готівкову витрату."
    else:
        lines = ["📋 <b>Операції</b>", "<i>Найновіші зверху · натисніть номер, щоб змінити категорію</i>", ""]
        for index, row in enumerate(rows, 1):
            amount = int(row["amount_minor"])
            code = int(row["currency_code"] or 980)
            kind = "Надходження" if amount >= 0 else "Витрата"
            signed = "+" if amount >= 0 else "−"
            source = row["display_account_label"] if row["source"] == "monobank" else {"cash": "Готівка", "manual": "Ручна операція"}.get(row["source"], row["source"])
            description_text = " ".join((row["description"] or "Без опису").split())
            if len(description_text) > 90:
                description_text = description_text[:87].rstrip() + "…"
            description = escape(description_text)
            lines.append(
                f"<b>{index}. {escape(kind)} · {signed}{money(abs(amount), code)}</b>\n"
                f"   {row['occurred_at'][:10]} · {description}\n"
                f"   {escape(str(source or 'Рахунок'))} · {escape(row['category_name'] or 'Без категорії')}"
            )
        text = "\n".join(lines)
    category_buttons = [[(f"🏷 №{index}", f"finance:category:{row['id']}") for index, row in enumerate(rows[start:start + 2], start + 1)] for start in range(0, len(rows), 2)]
    category_buttons.append([("← До фінансів", "finance:menu")])
    await callback.message.edit_text(text, reply_markup=keyboard(category_buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("finance:category:"))
async def category_choices(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        prefix, transaction_id_text = callback.data.rsplit(":", 1)
        if prefix != "finance:category":
            raise ValueError
        transaction_id = int(transaction_id_text)
    except (AttributeError, ValueError):
        await callback.answer("Некоректні дані транзакції.", show_alert=True)
        return
    transaction = next((row for row in await _db.finance_transactions(callback.from_user.id, 100) if row["id"] == transaction_id), None)  # type: ignore[union-attr]
    if not transaction:
        await callback.answer("Операцію не знайдено.", show_alert=True)
        return
    amount = int(transaction["amount_minor"])
    currency_code = int(transaction["currency_code"] or 980)
    amount_text = ("+" if amount >= 0 else "−") + money(abs(amount), currency_code)
    description = escape(transaction["description"] or "Без опису")
    categories = await _db.finance_categories(callback.from_user.id)  # type: ignore[union-attr]
    rows = [[(category["name"], f"finance:set-category:{transaction_id}:{category['id']}")] for category in categories]
    rows.append([("🎯 Прив'язати до цілі", f"finance:goal-select:{transaction_id}")])
    rows.append([("➕ Додати категорію", f"finance:add-category:{transaction_id}")])
    rows.append([("← До операцій", "finance:transactions")])
    await callback.message.edit_text(
        f"🏷 <b>Категорія операції</b>\n{transaction['occurred_at'][:10]} · {amount_text}\n{description}\n\nОберіть категорію:",
        reply_markup=keyboard(rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("finance:goal-select:"))
async def goal_select(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        transaction_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некоректна транзакція.", show_alert=True)
        return
    goals = await _db.active_finance_goals(callback.from_user.id)  # type: ignore[union-attr]
    rows = [[(f"🎯 {goal['title']}", f"finance:goal-link:{transaction_id}:{goal['id']}")] for goal in goals]
    rows.append([("← До транзакції", f"finance:category:{transaction_id}")])
    text = "Оберіть активну ціль:"
    if not goals:
        text = "Активних цілей немає. Спочатку створіть ціль у розділі «Мої цілі»."
    await callback.message.edit_text(text, reply_markup=keyboard(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("finance:goal-link:"))
async def goal_link(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        _, _, transaction_id_text, goal_id_text = callback.data.split(":")
        transaction_id = int(transaction_id_text)
        goal_id = int(goal_id_text)
    except (AttributeError, ValueError):
        await callback.answer("Некоректні дані прив’язки.", show_alert=True)
        return
    linked = await _db.link_finance_transaction_goal(callback.from_user.id, transaction_id, goal_id)  # type: ignore[union-attr]
    if not linked:
        await callback.answer("Транзакцію або ціль не знайдено.", show_alert=True)
        return
    amount = abs(int(linked["amount_minor"]))
    currency_code = int(linked["currency_code"] or 980)
    await callback.message.edit_text(
        f"✅ Надходження <b>{money(amount, currency_code)}</b> прив’язано до цілі «{escape(linked['goal_title'])}».",
        reply_markup=keyboard([[('📋 До транзакцій', 'finance:transactions'), ('🎯 До цілей', 'goals:list')]]),
        parse_mode="HTML",
    )
    await callback.answer("✅ Ціль оновлено")


@router.callback_query(F.data == "finance:settings:categories")
async def settings_categories(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    categories = await _db.finance_categories(callback.from_user.id)  # type: ignore[union-attr]
    rows = [[("➕ Додати категорію", "finance:add-category")]]
    rows.append([("← До налаштувань", "finance:settings")])
    category_list = "\n".join(f"• {escape(category['name'])}" for category in categories) or "Категорій ще немає."
    await callback.message.edit_text("🏷 <b>Категорії витрат</b>\n\n" + category_list, reply_markup=keyboard(rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("finance:add-category"))
async def add_category_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    await state.clear()
    parts = callback.data.split(":")
    if len(parts) == 3:
        await state.update_data(transaction_id=int(parts[2]))
    await state.set_state(CustomCategoryFlow.name)
    await callback.message.edit_text("Введіть назву нової категорії (бажано з емодзі, наприклад: 🎮 Хобі):", reply_markup=cancel_keyboard())
    await callback.answer()


@router.message(CustomCategoryFlow.name)
async def add_category_received(message: Message, state: FSMContext) -> None:
    if not await allowed(message):
        return
    try:
        category_id = await _db.create_finance_category(message.from_user.id, message.text or "")  # type: ignore[union-attr]
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    transaction_id = data.get("transaction_id")
    await state.clear()
    if transaction_id is None:
        await message.answer("✅ Категорію створено.", reply_markup=settings_menu())
        return
    await message.answer("✅ Категорію створено. Оберіть її для транзакції:", reply_markup=keyboard([
        [(message.text.strip()[:80], f"finance:set-category:{transaction_id}:{category_id}")],
        [("← До операцій", "finance:transactions")],
    ]))


@router.callback_query(F.data.startswith("finance:set-category:"))
async def set_category(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    try:
        prefix, transaction_id_text, category_id_text = callback.data.rsplit(":", 2)
        if prefix != "finance:set-category":
            raise ValueError
        transaction_id = int(transaction_id_text)
        category_id = int(category_id_text)
    except (AttributeError, ValueError):
        await callback.answer("Некоректні дані категорії транзакції.", show_alert=True)
        return
    updated = await _db.update_transaction_category(callback.from_user.id, transaction_id, category_id)  # type: ignore[union-attr]
    await callback.answer("✅ Категорію змінено успішно!" if updated else "Операцію або категорію не знайдено.", show_alert=not updated)
    if updated:
        data = await service().dashboard(callback.from_user.id)
        await callback.message.edit_text(
            "✅ <b>Категорію змінено успішно!</b>\n\n" + dashboard_text(data),
            reply_markup=keyboard([
                [("🔗 Завжди застосовувати для таких платежів", f"finance:auto-rule-history:{transaction_id}:{category_id}")],
                [("📜 До історії транзакцій", "finance:transactions")],
                [("📊 Оновити dashboard", "finance:menu")],
            ]),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "finance:cash")
async def cash_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    await state.clear()
    await state.set_state(CashFlow.amount)
    await callback.message.edit_text("Введіть суму готівкової витрати у гривнях, наприклад: 250,50.", reply_markup=cancel_keyboard())
    await callback.answer()


@router.message(CashFlow.amount)
async def cash_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = parse_number(message.text)
        if not 0 < amount <= 100_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Введіть додатне число до 100 000 000 грн, наприклад 250,50.", reply_markup=cancel_keyboard())
        return
    await state.update_data(amount_minor=round(amount * 100))
    await state.set_state(CashFlow.description)
    await message.answer("Введіть опис витрати або надішліть «-».", reply_markup=cancel_keyboard())


@router.message(CashFlow.description)
async def cash_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    description = (message.text or "").strip()
    if len(description) > 200:
        await message.answer("Опис має містити не більше 200 символів.", reply_markup=cancel_keyboard())
        return
    await service().add_cash(message.from_user.id, data["amount_minor"], "" if description == "-" else description)
    await state.clear()
    await message.answer("✅ Готівкову витрату збережено.", reply_markup=back_to_finance_keyboard())


@router.callback_query(F.data.in_({"finance:remove", "finance:settings:remove"}))
async def remove_token(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    if callback.data == "finance:settings:remove":
        await callback.message.edit_text(
            "Видалити підключення Monobank? Рахунки, історія операцій і категорії залишаться в боті.",
            reply_markup=keyboard([
                [("🗑 Так, видалити токен", "finance:remove")],
                [("← Скасувати", "finance:settings")],
            ]),
        )
        await callback.answer()
        return
    await service().remove_token(callback.from_user.id)
    await callback.message.edit_text("✅ Токен Monobank видалено.", reply_markup=settings_menu())
    await callback.answer()


@router.callback_query(F.data == "finance:settings")
async def finance_settings(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    await callback.message.edit_text("⚙️ <b>Налаштування фінансів</b>", reply_markup=settings_menu(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "finance:settings:budget")
async def budget_settings(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    await callback.message.edit_text(
        "📊 <b>Пресет бюджету</b>\nОберіть готовий варіант або створіть власний:",
        reply_markup=keyboard([
            [("Класичний 50/30/20", "finance:budget:classic")],
            [("Агресивний 40/20/40", "finance:budget:aggressive")],
            [("Власний кастомний %", "finance:budget:custom")],
            [("← До налаштувань", "finance:settings")],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.in_({"finance:budget:classic", "finance:budget:aggressive"}))
async def preset_budget(callback: CallbackQuery) -> None:
    if not await allowed(callback):
        return
    shares = (50, 30, 20) if callback.data.endswith("classic") else (40, 20, 40)
    name = "Класичний 50/30/20" if callback.data.endswith("classic") else "Агресивний"
    await service().set_budget(callback.from_user.id, name, shares)
    await callback.message.edit_text(f"✅ Активний бюджет: <b>{name}</b>\nПотреби {shares[0]}% · Бажання {shares[1]}% · Залишок доходу {shares[2]}%", reply_markup=settings_menu(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "finance:budget:custom")
async def custom_budget_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await allowed(callback):
        return
    await state.clear()
    await state.set_state(BudgetFlow.needs)
    await callback.message.edit_text("Введіть відсоток на потреби (0–100).", reply_markup=cancel_keyboard())
    await callback.answer()


async def percentage(message: Message) -> float | None:
    try:
        value = parse_number(message.text)
        if not 0 <= value <= 100:
            raise ValueError
        return value
    except ValueError:
        await message.answer("Введіть число від 0 до 100.", reply_markup=cancel_keyboard())
        return None


@router.message(BudgetFlow.needs)
async def custom_budget_needs(message: Message, state: FSMContext) -> None:
    value = await percentage(message)
    if value is None:
        return
    await state.update_data(needs=value)
    await state.set_state(BudgetFlow.wants)
    await message.answer("Введіть відсоток на бажання.", reply_markup=cancel_keyboard())


@router.message(BudgetFlow.wants)
async def custom_budget_wants(message: Message, state: FSMContext) -> None:
    value = await percentage(message)
    if value is None:
        return
    await state.update_data(wants=value)
    await state.set_state(BudgetFlow.savings)
    await message.answer("Введіть відсоток на заощадження. Усі три значення мають разом дорівнювати 100%.", reply_markup=cancel_keyboard())


@router.message(BudgetFlow.savings)
async def custom_budget_savings(message: Message, state: FSMContext) -> None:
    value = await percentage(message)
    if value is None:
        return
    data = await state.get_data()
    try:
        await service().set_budget(message.from_user.id, "Власний", (data["needs"], data["wants"], value))
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=cancel_keyboard())
        return
    await state.clear()
    await message.answer("✅ Власний бюджет збережено.", reply_markup=settings_menu())
