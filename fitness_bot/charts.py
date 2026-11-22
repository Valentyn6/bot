from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from textwrap import fill
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _chart(title: str, dates: list[str], values: list[float], ylabel: str) -> Path:
    handle = NamedTemporaryFile(prefix="fitness_", suffix=".png", delete=False)
    path = Path(handle.name)
    handle.close()
    fig, axis = plt.subplots(figsize=(9, 4.5))
    axis.plot(dates, values, marker="o", linewidth=2, color="#176b87")
    axis.set_title(title)
    axis.set_ylabel(ylabel)
    axis.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def weight_chart(rows: list[Any]) -> Path:
    return _chart("Графік ваги", [row["log_date"] for row in rows], [row["weight"] for row in rows], "кг")


def activity_chart(rows: list[Any], field: str, title: str, ylabel: str) -> Path:
    if field == "total_calories":
        values = [row["active_calories"] for row in rows]
    else:
        values = [row[field] for row in rows]
    return _chart(title, [row["log_date"] for row in rows], values, ylabel)


def expense_category_chart(rows: list[dict[str, Any]], currency_code: int, period_label: str) -> Path:
    symbols = {980: "₴", 840: "$", 978: "€", 826: "£"}
    symbol = symbols.get(int(currency_code), f"{currency_code}")
    ranked = sorted(
        ((str(row["name"]), int(row["amount"])) for row in rows if int(row["amount"]) > 0),
        key=lambda row: row[1],
        reverse=True,
    )
    ranked.reverse()

    handle = NamedTemporaryFile(prefix="finance_expenses_", suffix=".png", delete=False)
    path = Path(handle.name)
    handle.close()
    fig, axis = plt.subplots(figsize=(10, max(4.5, 0.62 * len(ranked) + 1.6)))
    labels = [fill(name, width=28) for name, _ in ranked]
    values = [amount / 100 for _, amount in ranked]
    bars = axis.barh(range(len(ranked)), values, color="#176b87", height=0.64)
    max_value = max(values, default=0)
    axis.set_yticks(range(len(ranked)), labels)
    axis.set_xlim(0, max_value * 1.38 if max_value else 1)
    axis.set_xlabel(f"Сума, {symbol}")
    axis.set_title(f"Витрати за категоріями · {period_label}")
    axis.grid(axis="x", alpha=0.25)
    axis.set_axisbelow(True)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.tick_params(axis="y", length=0)
    total = sum(values)
    for bar, value in zip(bars, values):
        share = value / total * 100 if total else 0
        axis.text(value + max_value * 0.018, bar.get_y() + bar.get_height() / 2, f"{value:,.2f} {symbol} · {share:.0f}%", va="center", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return path


def exercise_progress_chart(rows: list[Any], exercise_name: str) -> Path:
    handle = NamedTemporaryFile(prefix="training_", suffix=".png", delete=False)
    path = Path(handle.name)
    handle.close()
    fig, axis = plt.subplots(figsize=(9, 4.5))
    axis.plot(
        [row["log_date"] for row in rows],
        [row["weight"] for row in rows],
        marker="o", linewidth=2, color="#176b87",
    )
    axis.set_title(f"Прогрес: {exercise_name}")
    axis.set_ylabel("Вага, кг")
    axis.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path
