import asyncio
from datetime import date, datetime, timedelta

from fitness_bot.db import Database
from fitness_bot.finance.monobank import category_for_mcc, decrypt_token, encrypt_token
from fitness_bot.finance.router import comparison_text, dashboard_menu, dashboard_text as finance_dashboard_text, money
from fitness_bot.finance.service import FinanceService
from fitness_bot.goals.router import dashboard_text, progress_bar, streak_achievement


def test_monobank_token_encryption_authenticates_payload(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "legacy-bot-secret-for-migration")
    monkeypatch.delenv("MONOBANK_TOKEN_SECRET", raising=False)
    legacy_v2 = encrypt_token("legacy-monobank-token-123456")
    monkeypatch.setenv("MONOBANK_TOKEN_SECRET", "unit-test-secret-with-sufficient-entropy")
    assert decrypt_token(legacy_v2) == "legacy-monobank-token-123456"
    ciphertext = encrypt_token("test-monobank-token-123456")
    assert ciphertext.startswith("v2:")
    assert decrypt_token(ciphertext) == "test-monobank-token-123456"
    damaged = ciphertext[:8] + ("A" if ciphertext[8] != "A" else "B") + ciphertext[9:]
    try:
        decrypt_token(damaged)
    except ValueError:
        pass
    else:
        raise AssertionError("Tampered token ciphertext must be rejected")


def test_finance_dashboard_explains_period_budget_and_separates_currencies():
    data = {
        "summary": {
            "income": 100_000,
            "expenses": 125_000,
            "uncategorized": 2,
            "currency_totals": [
                {"currency_code": 840, "income": 25_000, "expenses": 10_000},
                {"currency_code": 980, "income": 100_000, "expenses": 125_000},
            ],
            "categories": [
                {"name": "🛒 Продукти", "amount": 60_000, "currency_code": 980},
                {"name": "🎬 Розваги", "amount": 65_000, "currency_code": 980},
                {"name": "Покупки", "amount": 10_000, "currency_code": 840},
            ],
        },
        "budget": (50, 30, 20),
        "balances_by_currency": {980: 500_000, 840: 30_000},
        "account_balances_by_currency": {980: 400_000, 840: 20_000},
        "jar_balances_by_currency": {980: 100_000, 840: 10_000},
        "jars": [],
        "info": {},
    }

    text = finance_dashboard_text(data)

    assert "Операції за останні 30 днів" in text
    assert "USD · $" in text and "UAH · ₴" in text
    assert "Різниця: <b>−250.00 ₴</b>" in text
    assert "перевищено на 100.00 ₴" in text
    assert "Без категорії:</b> 2 операцій" in text
    assert "запас автономії" not in text.casefold()
    assert len(dashboard_menu(0).inline_keyboard) == 7
    assert dashboard_menu(0).inline_keyboard[4][0].callback_data == "finance:repeat-cash"


def test_finance_comparison_labels_periods_and_currencies():
    text = comparison_text({
        "current": {"income": 10000, "expenses": 20000, "currency_totals": [{"currency_code": 980, "income": 10000, "expenses": 20000}]},
        "previous": {"income": 5000, "expenses": 10000, "currency_totals": [{"currency_code": 980, "income": 5000, "expenses": 10000}]},
        "income_diff_pct": 100,
        "expenses_diff_pct": 100,
    })
    assert "Останні 30 днів проти попередніх 30 днів" in text
    assert "Витрати: 200.00 ₴ · раніше 100.00 ₴" in text
    assert "Зміна надходжень: ▲ +100.0%" in text


def test_finance_goals_and_habits_are_isolated(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "fitness.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        await database.ensure_account(2)

        duplicate_category = await database._db().execute("INSERT INTO finance_categories(telegram_id, name, kind, mcc_codes) VALUES (NULL, 'Здоровʼя', 'expense', '')")
        await database._db().commit()
        await database._migrate_finance_categories()
        category_names = [row["name"] for row in await database.finance_categories(1)]
        assert len(category_names) == len({name.casefold() for name in category_names})
        assert "🩺 Здоровʼя" in category_names

        account_id = await database.save_monobank_account(1, "cipher", "…1234")
        category_id = (await database.finance_categories(1))[0]["id"]
        rule_id = await database.save_finance_auto_rule(1, "Київстар", category_id)
        assert rule_id > 0
        rule = await database.matching_finance_auto_rule(1, "Оплата КИЇВСТАР за тариф")
        assert rule["category_id"] == category_id
        assert await database.matching_finance_auto_rule(2, "Оплата Київстар") is None
        jar_id = await database.save_finance_jar(1, account_id, "jar-1", "На ноутбук", 2800000, 4000000)
        jars = await database.finance_jars(1)
        assert jars[0]["id"] == jar_id
        assert (jars[0]["title"], jars[0]["balance_minor"], jars[0]["goal_minor"]) == ("На ноутбук", 2800000, 4000000)
        await database.save_finance_jar(1, account_id, "jar-1", "Ноутбук", 3000000, 4000000)
        assert (await database.finance_jars(1))[0]["title"] == "Ноутбук"
        assert await database.finance_jars(2) == []
        first = await database.add_finance_transaction(
            1,
            {
                "monobank_account_id": account_id,
                "mono_transaction_id": "mono-1",
                "amount_minor": -25000,
                "description": "Тест",
                "source": "monobank",
                "occurred_at": "2026-01-01T10:00:00",
            },
        )
        duplicate = await database.add_finance_transaction(
            1,
            {
                "monobank_account_id": account_id,
                "mono_transaction_id": "mono-1",
                "amount_minor": -25000,
                "description": "Тест",
                "source": "monobank",
                "occurred_at": "2026-01-01T10:00:00",
            },
        )
        assert first is not None
        assert duplicate is None
        assert len(await database.finance_transactions(2)) == 0

        categories = await database.finance_categories(1)
        first_category = categories[0]["id"]
        second_category = categories[1]["id"]
        transaction_category = await database.add_finance_transaction(
            1,
            {
                "amount_minor": -12345,
                "description": "Категорія тест",
                "source": "manual",
                "occurred_at": "2999-01-01T10:00:00",
                "category_id": first_category,
            },
        )
        assert transaction_category is not None
        before = await database.finance_summary(1, 300000)
        assert any(row["category_id"] == first_category and row["amount"] == 12345 for row in before["categories"])
        assert await database.update_transaction_category(1, transaction_category, second_category)
        after = await database.finance_summary(1, 300000)
        assert not any(row["category_id"] == first_category and row["amount"] == 12345 for row in after["categories"])
        assert any(row["category_id"] == second_category and row["amount"] == 12345 for row in after["categories"])

        goal_id = await database.create_goal(1, {"title": "Накопити", "target_value": 1000})
        assert len(await database.goals(1)) == 1
        assert len(await database.goals(2)) == 0
        assert await database.update_goal_progress(1, goal_id, 1000)

        habit_id = await database.create_habit(1, {"name": "Вода"})
        created, streak = await database.log_habit(1, habit_id, "2026-01-01")
        repeated, repeated_streak = await database.log_habit(1, habit_id, "2026-01-01")
        assert (created, streak) == (True, 1)
        assert (repeated, repeated_streak) == (False, 1)

        await database.close()

    asyncio.run(scenario())


def test_interactive_goal_and_habit_actions(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "interactive-goals.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        goal_id = await database.create_goal(1, {"title": "Книги", "target_value": 3, "goal_type": "books"})
        assert await database.add_goal_contribution(1, goal_id, 1)
        assert await database.goal_progress(1, goal_id) == 1
        habit_id = await database.create_habit(1, {"name": "Читати"})
        today_date = datetime.now().date()
        today = today_date.isoformat()
        completed, _ = await database.toggle_habit(1, habit_id, today)
        assert completed is True
        assert await database.habit_completed(1, habit_id, today)
        week_start = today_date.fromordinal(today_date.toordinal() - today_date.weekday())
        expected_week_status = [offset == today_date.weekday() for offset in range(7)]
        assert await database.habit_week_status(1, habit_id, week_start.isoformat()) == expected_week_status
        completed, _ = await database.toggle_habit(1, habit_id, today)
        assert completed is False
        assert not await database.habit_completed(1, habit_id, today)
        completed, _ = await database.toggle_habit(1, habit_id, today)
        assert completed is True
        assert await database.delete_habit(2, habit_id) is False
        assert await database.delete_habit(1, habit_id) is True
        assert await database.habits(1) == []
        assert await database.delete_goal(1, goal_id)
        assert await database.goal_progress(1, goal_id) is None
        await database.close()

    asyncio.run(scenario())


def test_goals_dashboard_uses_specified_progress_format():
    assert progress_bar(1, 2) == "[█████░░░░░] 50%"
    text = dashboard_text(
        [{"title": "Книги", "goal_type": "books", "calculated_current_value": 1, "target_value": 2, "calculated_status": "active"}],
        [],
        [{"name": "Читати", "completed_today": True, "streak": 5, "week_status": [True, True, False, False, False, False, False]}],
    )
    assert "1 / 2 шт. — [█████░░░░░] 50%" in text
    assert "🔥 5 дн. поспіль" in text
    assert "✅✅⬜⬜⬜⬜⬜ <i>Пн–Нд</i>" in text


def test_habit_streak_achievements_use_highest_reached_threshold():
    assert streak_achievement(2) is None
    assert streak_achievement(3) == ("🥉", "Бронзовий старт")
    assert streak_achievement(7) == ("🥈", "Срібний тиждень")
    assert streak_achievement(21) == ("🥇", "Золота звичка")
    assert streak_achievement(30) == ("💎", "Платиновий місяць")
    assert streak_achievement(100) == ("👑", "Легенда")
    assert streak_achievement(101) == ("👑", "Легенда")

    text = dashboard_text(
        [],
        [],
        [{"name": "Читати", "completed_today": True, "streak": 7, "week_status": [True] * 7}],
    )
    assert "🔥 7 дн. поспіль 🥈" in text


def test_savings_category_allocates_to_newer_goals_first(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "finance-savings-goals.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        older_goal = await database.create_goal(1, {"title": "Стара ціль", "target_value": 100})
        newer_goal = await database.create_goal(1, {"title": "Нова ціль", "target_value": 100})
        savings_category = next(row["id"] for row in await database.finance_categories(1) if "накопичення" in row["name"].casefold())
        await database.add_finance_transaction(1, {"amount_minor": 15000, "category_id": savings_category, "description": "Переказ у накопичення", "source": "manual"})

        goals = {row["id"]: row for row in await database.goals(1)}
        assert goals[newer_goal]["calculated_current_value"] == 100
        assert goals[older_goal]["calculated_current_value"] == 50
        await database.close()

    asyncio.run(scenario())


def test_finance_transaction_goal_link_updates_progress_and_isolation(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "finance-goal-link.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        await database.ensure_account(2)
        goal_id = await database.create_goal(1, {"title": "Новий ноутбук", "target_value": 1000, "current_value": 100})
        monobank_id = await database.save_monobank_account(1, "cipher", "…1234")
        transaction_id = await database.add_finance_transaction(1, {"monobank_account_id": monobank_id, "amount_minor": 25000, "description": "Накопичення", "source": "monobank"})

        linked = await database.link_finance_transaction_goal(1, transaction_id, goal_id)
        assert linked["goal_title"] == "Новий ноутбук"
        assert await database.goal_progress(1, goal_id) == 350
        visible_goal = (await database.goals(1))[0]
        assert visible_goal["calculated_current_value"] == 350
        assert visible_goal["calculated_status"] == "active"
        assert await database.link_finance_transaction_goal(2, transaction_id, goal_id) is None
        await database.close()

    asyncio.run(scenario())


def test_finance_mcc_rules_and_analytics(tmp_path, monkeypatch):
    async def scenario():
        database = Database(str(tmp_path / "finance-analytics.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        monobank_id = await database.save_monobank_account(1, encrypt_token("test-monobank-token-123456"), "…3456")
        category_id = (await database.finance_categories(1))[0]["id"]
        await database.save_finance_mcc_rule(1, 5411, category_id)
        rule = await database.matching_finance_rule(1, "Супермаркет", 5411)
        assert rule["category_id"] == category_id
        assert category_for_mcc(4814) == "Комунальні"

        now = datetime.now() - timedelta(minutes=1)
        await database.add_finance_transaction(1, {"monobank_account_id": monobank_id, "amount_minor": -300000, "description": "Поточна витрата", "occurred_at": now.isoformat(timespec="seconds")})
        await database.add_finance_transaction(1, {"monobank_account_id": monobank_id, "amount_minor": -150000, "description": "Минула витрата", "occurred_at": (now - timedelta(days=40)).isoformat(timespec="seconds")})

        class SuccessfulClient:
            def __init__(self, token):
                pass

            async def client_info(self):
                return {"accounts": [{"id": "card-1", "currencyCode": 980, "balance": 100000}], "jars": []}

        monkeypatch.setattr("fitness_bot.finance.service.MonobankClient", SuccessfulClient)
        service = FinanceService(database)
        dashboard = await service.dashboard(1)
        assert dashboard["runway_days"] == 10
        comparison = await service.comparison(1)
        assert comparison["current"]["expenses"] == 300000
        assert comparison["previous"]["expenses"] == 150000
        assert comparison["expenses_diff_pct"] == 100
        await database.close()

    asyncio.run(scenario())


def test_finance_dashboard_uses_negative_live_balance(tmp_path, monkeypatch):
    async def scenario():
        database = Database(str(tmp_path / "finance-negative.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        monobank_id = await database.save_monobank_account(1, encrypt_token("test-monobank-token-123456"), "…3456")

        class SuccessfulClient:
            def __init__(self, token):
                pass

            async def client_info(self):
                return {"accounts": [{"id": "card-1", "currencyCode": 980, "balance": -403310}], "jars": []}

        monkeypatch.setattr("fitness_bot.finance.service.MonobankClient", SuccessfulClient)
        data = await FinanceService(database).dashboard(1)

        assert data["balance_minor"] == -403310
        assert (await database.finance_accounts(1))[0]["balance_minor"] == -403310
        await database.close()

    asyncio.run(scenario())


def test_finance_dashboard_uses_cached_balances_when_monobank_fails(tmp_path, monkeypatch):
    async def scenario():
        database = Database(str(tmp_path / "finance-cache.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        monobank_id = await database.save_monobank_account(1, encrypt_token("test-monobank-token-123456"), "…3456")
        await database.save_finance_account(1, monobank_id, "card-1", "••1111", balance_minor=125000)
        await database.save_finance_jar(1, monobank_id, "jar-1", "Резерв", 50000, 100000)

        class FailingClient:
            def __init__(self, token):
                pass

            async def client_info(self):
                raise RuntimeError("429")

        monkeypatch.setattr("fitness_bot.finance.service.MonobankClient", FailingClient)
        data = await FinanceService(database).dashboard(1)

        assert data["balance_minor"] == 175000
        assert data["info"]["accounts"][0]["balance"] == 125000
        assert data["info"]["jars"][0]["balance"] == 50000
        await database.close()

    asyncio.run(scenario())


def test_finance_balance_cache_preserves_successful_zero(tmp_path, monkeypatch):
    async def scenario():
        database = Database(str(tmp_path / "finance-zero.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        await database.save_monobank_account(1, encrypt_token("test-monobank-token-123456"), "…3456")

        class SuccessfulClient:
            def __init__(self, token):
                pass

            async def client_info(self):
                return {"accounts": [{"id": "card-1", "type": "black", "currencyCode": 980, "balance": 0}], "jars": []}

        monkeypatch.setattr("fitness_bot.finance.service.MonobankClient", SuccessfulClient)
        service = FinanceService(database)
        info = await service.account_info(1)
        assert info["accounts"][0]["balance"] == 0

        class FailingClient(SuccessfulClient):
            async def client_info(self):
                raise RuntimeError("network error")

        monkeypatch.setattr("fitness_bot.finance.service.MonobankClient", FailingClient)
        cached = await service.account_info(1)
        assert cached["accounts"][0]["balance"] == 0
        assert (await database.finance_accounts(1))[0]["balance_minor"] == 0
        await database.close()

    asyncio.run(scenario())


def test_report_periods_habits_and_repeat_cash_are_scoped(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "reports-and-repeat.sqlite3"))
        await database.connect()
        await database.ensure_account(1)
        await database.ensure_account(2)
        await database.set_access(1, "approved")
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        original = await database.add_finance_transaction(1, {
            "amount_minor": -12550,
            "currency_code": 980,
            "description": "Кава",
            "source": "cash",
            "occurred_at": f"{today}T10:00:00",
        })
        await database.add_finance_transaction(1, {
            "amount_minor": -5000,
            "currency_code": 980,
            "description": "Вчора",
            "source": "cash",
            "occurred_at": f"{yesterday}T10:00:00",
        })
        assert original is not None
        daily = await database.finance_report_summary(1, today, today)
        assert len(daily) == 1
        assert (daily[0]["expenses"], daily[0]["income"]) == (12550, 0)
        assert await database.cash_expense_template(2, original) is None
        repeated = await FinanceService(database).repeat_cash(1, original)
        assert repeated > original
        recent = await database.recent_cash_expenses(1)
        assert recent[0]["id"] == repeated
        assert recent[0]["amount_minor"] == -12550
        assert recent[0]["description"] == "Кава"

        habit_id = await database.create_habit(1, {"name": "Прогулянка"})
        await database.log_habit(1, habit_id, today)
        habit_summary = await database.habit_period_summary(1, today, today)
        assert habit_summary == {"habits": 1, "completed": 1, "opportunities": 1}
        assert await database.habit_period_summary(2, today, today) == {
            "habits": 0, "completed": 0, "opportunities": 0,
        }
        await database.set_daily_summary_time(1, "20:00")
        assert await database.due_daily_summary_users("20:00", today) == [1]
        assert await database.claim_daily_summary(1, "20:00", today)
        assert await database.due_daily_summary_users("20:00", today) == []
        await database.set_daily_summary_time(1, None)
        assert await database.active_daily_summary_count() == 0
        await database.close()

    asyncio.run(scenario())
