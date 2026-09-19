from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
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
