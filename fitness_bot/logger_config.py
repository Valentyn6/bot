"""Конфігурація логування для Fitness Bot."""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

try:
    from .constants import LOG_DATE_FORMAT, LOG_FILE, LOG_FORMAT, LOG_LEVEL, LOG_MAX_BYTES, LOG_BACKUP_COUNT
except ImportError:
    from constants import LOG_DATE_FORMAT, LOG_FILE, LOG_FORMAT, LOG_LEVEL, LOG_MAX_BYTES, LOG_BACKUP_COUNT


def setup_logging(
    level: str = LOG_LEVEL,
    log_file: str = LOG_FILE,
    console: bool = True,
) -> logging.Logger:
    """Налаштувати логування для програми.

    Args:
        level: Рівень логування (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Шлях до файлу логів
        console: Логувати також в консоль

    Returns:
        Налаштований logger об'єкт
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Очистити існуючі обробники
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Формат для логів
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # Обробник файлів з ротацією
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Обробник консолі
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    root_logger.info(f"Логування налаштовано: рівень={level}, файл={log_file}")
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Отримати логер для модуля."""
    return logging.getLogger(name)
