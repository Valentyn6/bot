from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..db import Database
from .monobank import MonobankClient, category_for_mcc, decrypt_token, encrypt_token, token_hint

logger = logging.getLogger(__name__)


class FinanceService:
    def __init__(self, db: Database):
        self.db = db

    async def save_token(self, telegram_id: int, token: str) -> dict[str, Any]:
        token = token.strip()
        if len(token) < 20 or any(char.isspace() for char in token):
            raise ValueError("Токен має виглядати як довгий рядок без пробілів.")
        info = await MonobankClient(token).client_info()
        account_id = await self.db.save_monobank_account(telegram_id, encrypt_token(token), token_hint(token))
        imported = await self.sync(telegram_id, 30)
        return {"id": account_id, "name": info.get("name") or "клієнт", "accounts": info.get("accounts", []), "jars": info.get("jars", []), "imported": imported}

    async def remove_token(self, telegram_id: int) -> None:
        await self.db.delete_monobank_account(telegram_id)

    async def account_info(self, telegram_id: int) -> dict[str, Any]:
        stored = await self.db.monobank_account(telegram_id)
        if not stored:
            raise ValueError("Токен Monobank ще не додано.")
        try:
            info = await MonobankClient(decrypt_token(stored["token_ciphertext"])).client_info()
        except Exception as exc:
            logger.warning("Monobank client-info failed; using cached balances: %s", exc)
            return await self._cached_account_info(telegram_id, stored)
        if not isinstance(info, dict):
            raise ValueError("Monobank повернув некоректні дані.")
        return await self._cache_account_info(telegram_id, stored, info)

    async def _cache_account_info(self, telegram_id: int, stored: Any, info: dict[str, Any]) -> dict[str, Any]:
        cached = {row["external_id"]: row for row in await self.db.finance_accounts(telegram_id)}
        accounts = []
        raw_accounts = info.get("accounts", [])
        if not isinstance(raw_accounts, list):
            logger.error("Monobank client-info accounts has unexpected type: %s", type(raw_accounts).__name__)
            raw_accounts = []
        for account in raw_accounts:
            if not isinstance(account, dict) or not account.get("id"):
                continue
            external_id = str(account["id"])
            cached_account = cached.get(external_id)
            balance = account.get("balance")
            if balance is not None:
                try:
                    balance = int(balance)
                except (TypeError, ValueError):
                    logger.warning("Ignoring invalid Monobank balance for account %s: %r", external_id, balance)
                    balance = None
            if balance is None and cached_account and cached_account["balance_minor"] is not None:
                balance = int(cached_account["balance_minor"])
                account = {**account, "balance": balance}
            logger.debug("Parsed Monobank account %s: balance_minor=%r", external_id, balance)
            masked_pan = account.get("maskedPan") or []
            default_label = masked_pan[0] if masked_pan else account.get("type") or "Monobank"
            await self.db.save_finance_account(
                telegram_id,
                stored["id"],
                external_id,
                default_label,
                int(account.get("currencyCode", 980)),
                balance,
            )
            accounts.append(account)
        raw_jars = info.get("jars", [])
        if not isinstance(raw_jars, list):
            logger.warning("Monobank client-info jars has unexpected type: %s; treating it as empty", type(raw_jars).__name__)
            raw_jars = []
        for jar in raw_jars:
            if not isinstance(jar, dict) or not jar.get("id") or jar.get("balance") is None:
                continue
            try:
                await self.db.save_finance_jar(
                    telegram_id,
                    stored["id"],
                    str(jar["id"]),
                    str(jar.get("title") or jar.get("name") or "Скарбничка"),
                    int(jar["balance"]),
                    int(jar.get("goal", 0) or 0),
                    int(jar.get("currencyCode", 980) or 980),
                )
            except (TypeError, ValueError):
                continue
        return {**info, "accounts": accounts, "jars": raw_jars}

    async def _cached_account_info(self, telegram_id: int, stored: Any) -> dict[str, Any]:
        accounts = []
        for row in await self.db.finance_accounts(telegram_id):
            account = {
                "id": row["external_id"],
                "type": "card",
                "currencyCode": row["currency_code"],
                "maskedPan": [row["alias"] or row["default_label"]],
            }
            if row["balance_minor"] is not None:
                account["balance"] = int(row["balance_minor"])
            accounts.append(account)
        jars = [dict(row) for row in await self.db.finance_jars(telegram_id)]
        return {"accounts": accounts, "jars": [{"id": jar["external_id"], "title": jar["title"], "balance": jar["balance_minor"], "goal": jar["goal_minor"], "currencyCode": jar["currency_code"]} for jar in jars], "cached": True}

    async def sync(self, telegram_id: int, days: int = 30) -> int:
        stored = await self.db.monobank_account(telegram_id)
        if not stored:
            raise ValueError("Спочатку додайте токен Monobank.")
        client = MonobankClient(decrypt_token(stored["token_ciphertext"]))
        info = await client.client_info()
        categories = {row["name"]: row["id"] for row in await self.db.finance_categories(telegram_id)}
        category_ids = {
            "Продукти": next((value for key, value in categories.items() if "продукт" in key.casefold()), None),
            "Транспорт": next((value for key, value in categories.items() if "транспорт" in key.casefold()), None),
            "Ресторани": next((value for key, value in categories.items() if "кафе" in key.casefold() or "рестора" in key.casefold()), None),
            "Комунальні": next((value for key, value in categories.items() if "комуналь" in key.casefold() or "зв'язок" in key.casefold()), None),
            "Здоровʼя": next((value for key, value in categories.items() if "здоров" in key.casefold()), None),
            "Розваги": next((value for key, value in categories.items() if "розваг" in key.casefold() or "дозвіл" in key.casefold()), None),
            "Покупки": next((value for key, value in categories.items() if "покуп" in key.casefold() or "одяг" in key.casefold()), None),
            "Інше": next((value for key, value in categories.items() if key.casefold().endswith("інше")), None),
        }
        imported = 0
        raw_accounts = info.get("accounts", [])
        if not isinstance(raw_accounts, list):
            raise ValueError("Monobank не повернув список рахунків.")
        for account in raw_accounts:
            account_id = str(account.get("id", ""))
            if not account_id:
                continue
            masked_pan = account.get("maskedPan") or []
            default_label = masked_pan[0] if masked_pan else account.get("type") or "Monobank"
            finance_account_id = await self.db.save_finance_account(
                telegram_id,
                stored["id"],
                account_id,
                default_label,
                int(account.get("currencyCode", 980)),
                int(account["balance"]) if account.get("balance") is not None else None,
            )
            finance_account = next((row for row in await self.db.finance_accounts(telegram_id) if row["id"] == finance_account_id), None)
            account_label = (finance_account["alias"] if finance_account and finance_account["alias"] else default_label)
            for item in await client.statement(account_id, days):
                mcc = item.get("mcc")
                description = item.get("description") or "Без опису"
                rule = await self.db.matching_finance_rule(telegram_id, description, mcc)
                category_name = category_for_mcc(mcc)
                category_id = rule["category_id"] if rule else category_ids.get(category_name)
                saved = await self.db.add_finance_transaction(
                    telegram_id,
                    {
                        "monobank_account_id": stored["id"],
                        "mono_transaction_id": item.get("id"),
                        "finance_account_id": finance_account_id,
                        "amount_minor": int(item.get("amount", 0)),
                        "currency_code": int(item.get("currencyCode", 980)),
                         "description": description,
                        "account_label": account_label,
                        "mcc": mcc,
                        "category_id": category_id,
                        "source": "monobank",
                        "occurred_at": datetime.fromtimestamp(item.get("time", 0)).isoformat(timespec="seconds"),
                    },
                )
                imported += int(saved is not None)
        raw_jars = info.get("jars", [])
        if not isinstance(raw_jars, list):
            raw_jars = []
        for jar in raw_jars:
            jar_id = str(jar.get("id", ""))
            if not jar_id:
                continue
            await self.db.save_finance_jar(
                telegram_id,
                stored["id"],
                jar_id,
                str(jar.get("title") or jar.get("name") or "Скарбничка"),
                int(jar.get("balance", 0) or 0),
                int(jar.get("goal", 0) or 0),
                int(jar.get("currencyCode", 980) or 980),
            )
        return imported

    async def dashboard(self, telegram_id: int) -> dict[str, Any]:
        info = await self.account_info(telegram_id)
        summary = await self.db.finance_summary(telegram_id, 30)
        daily_avg = int(summary["expenses"]) / 100.0 / 30
        account_rows = await self.db.finance_accounts(telegram_id)
        jars = await self.db.finance_jars(telegram_id)
        accounts = info.get("accounts", [])
        if not isinstance(accounts, list):
            accounts = []
        live_balances = {
            str(account.get("id")): (int(account["balance"]), int(account.get("currencyCode") or 980))
            for account in accounts
            if isinstance(account, dict)
            and account.get("balance") is not None
        }
        cached_balances = {
            str(row["external_id"]): (int(row["balance_minor"]), int(row["currency_code"] or 980))
            for row in account_rows
            if row["balance_minor"] is not None
        }
        account_balances = {**cached_balances, **live_balances}
        account_balances_by_currency: dict[int, int] = {}
        for amount, currency_code in account_balances.values():
            account_balances_by_currency[currency_code] = account_balances_by_currency.get(currency_code, 0) + amount
        jars_by_currency: dict[int, int] = {}
        for jar in jars:
            currency_code = int(jar["currency_code"] or 980)
            jars_by_currency[currency_code] = jars_by_currency.get(currency_code, 0) + int(jar["balance_minor"])
        balances_by_currency = dict(account_balances_by_currency)
        for currency_code, amount in jars_by_currency.items():
            balances_by_currency[currency_code] = balances_by_currency.get(currency_code, 0) + amount
        # Keep the legacy aggregate for API consumers; UI formats each currency separately.
        balance_minor = sum(balances_by_currency.values())
        logger.info(
            "Finance dashboard balance: live_accounts=%r, cached_accounts=%r, accounts_minor=%d, jars_minor=%d, total_minor=%d, total_uah=%.2f",
            live_balances,
            cached_balances,
            balance_minor - sum(int(jar["balance_minor"]) for jar in jars),
            sum(int(jar["balance_minor"]) for jar in jars),
            balance_minor,
            balance_minor / 100.0,
        )
        budget = await self.db.active_budget(telegram_id)
        shares = (
            (budget["needs_percent"], budget["wants_percent"], budget["savings_percent"])
            if budget
            else (50, 30, 20)
        )
        return {
            "info": info,
            "summary": summary,
            "jars": jars,
            "balance_minor": balance_minor,
            "balance_uah": balance_minor / 100.0,
            "account_balances_by_currency": account_balances_by_currency,
            "jar_balances_by_currency": jars_by_currency,
            "balances_by_currency": balances_by_currency,
            "daily_avg": daily_avg,
            "runway_days": round(balance_minor / 100.0 / daily_avg) if balance_minor > 0 and daily_avg > 0 else None,
            "budget": shares,
        }

    async def comparison(self, telegram_id: int) -> dict[str, Any]:
        current = await self.db.finance_period_summary(telegram_id, 0, 30)
        previous = await self.db.finance_period_summary(telegram_id, 30, 60)
        previous_expenses = previous["expenses"]
        diff_pct = ((current["expenses"] - previous_expenses) / previous_expenses * 100) if previous_expenses else None
        income_diff_pct = ((current["income"] - previous["income"]) / previous["income"] * 100) if previous["income"] else None
        return {"current": current, "previous": previous, "expenses_diff_pct": diff_pct, "income_diff_pct": income_diff_pct}

    async def add_cash(self, telegram_id: int, amount_minor: int, description: str) -> int:
        transaction_id = await self.db.add_finance_transaction(
            telegram_id,
            {
                "amount_minor": -abs(amount_minor),
                "description": description or "Готівкова витрата",
                "source": "cash",
                "occurred_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        if transaction_id is None:
            raise ValueError("Не вдалося зберегти операцію.")
        return transaction_id

    async def repeat_cash(self, telegram_id: int, transaction_id: int) -> int:
        template = await self.db.cash_expense_template(telegram_id, transaction_id)
        if not template:
            raise ValueError("Цю готівкову витрату не знайдено.")
        if int(template["currency_code"] or 980) != 980:
            raise ValueError("Шаблон витрати має невідому валюту.")
        return await self.add_cash(
            telegram_id,
            abs(int(template["amount_minor"])),
            str(template["description"] or "Готівкова витрата"),
        )

    async def set_budget(self, telegram_id: int, name: str, shares: tuple[float, float, float]) -> None:
        await self.db.set_budget(telegram_id, name, *shares)

    async def budget_share(self, telegram_id: int, name: str = "Класичний 50/30/20") -> tuple[float, float, float]:
        presets = {
            "Класичний 50/30/20": (50, 30, 20),
            "Агресивний": (40, 20, 40),
        }
        budget = await self.db.active_budget(telegram_id)
        if name == "Власний" and budget:
            return budget["needs_percent"], budget["wants_percent"], budget["savings_percent"]
        return presets.get(name, presets["Класичний 50/30/20"])
