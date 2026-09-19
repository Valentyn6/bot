"""Unit тести для config.py"""

from unittest.mock import patch

import pytest
from fitness_bot.config import Settings


class TestSettings:
    """Тести конфігурації."""

    def test_settings_valid(self):
        """Тест створення валідних налаштувань."""
        settings = Settings(
            bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            database_path="test.db",
            admin_telegram_id=123456789,
        )

        assert settings.bot_token == "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
        assert "test.db" in settings.database_path
        assert settings.admin_telegram_id == 123456789

    def test_settings_token_empty(self):
        """Тест порожнього токену."""
        with pytest.raises(RuntimeError):
            Settings(bot_token="", database_path="test.db")

    def test_settings_token_too_short(self):
        """Тест занадто короткого токену."""
        with pytest.raises(RuntimeError):
            Settings(bot_token="short", database_path="test.db")

    def test_settings_token_no_colon(self):
        """Тест токену без двокрапки."""
        with pytest.raises(RuntimeError):
            Settings(bot_token="1234567890abcdefghijk", database_path="test.db")

    def test_settings_admin_id_optional(self):
        """Тест опціонального admin ID."""
        settings = Settings(
            bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            database_path="test.db",
            admin_telegram_id=None,
        )
        assert settings.admin_telegram_id is None

    def test_settings_log_level_valid(self):
        """Тест валідного рівня логування."""
        for level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            settings = Settings(
                bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                database_path="test.db",
                log_level=level,
            )
            assert settings.log_level == level

    def test_settings_log_level_invalid(self):
        """Тест невалідного рівня логування."""
        with pytest.raises(RuntimeError):
            Settings(
                bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                database_path="test.db",
                log_level="INVALID",
            )

    def test_settings_log_level_case_insensitive(self):
        """Тест case-insensitive рівня логування."""
        settings = Settings(
            bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            database_path="test.db",
            log_level="debug",
        )
        assert settings.log_level == "DEBUG"

    @patch.dict(
        "os.environ",
        {
            "BOT_TOKEN": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            "ADMIN_TELEGRAM_ID": "123456789",
            "DATABASE_PATH": "custom.db",
            "LOG_LEVEL": "DEBUG",
        },
    )
    def test_settings_from_env(self):
        """Тест завантаження з ENV змінних."""
        settings = Settings.from_env()

        assert settings.bot_token == "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
        assert settings.admin_telegram_id == 123456789
        assert "custom.db" in settings.database_path
        assert settings.log_level == "DEBUG"

    @patch.dict("os.environ", {"BOT_TOKEN": ""})
    def test_settings_from_env_no_token(self):
        """Тест завантаження з ENV без токену."""
        with pytest.raises(RuntimeError):
            Settings.from_env()

    @patch.dict(
        "os.environ",
        {
            "BOT_TOKEN": "1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            "ADMIN_TELEGRAM_ID": "not_a_number",
        },
    )
    def test_settings_from_env_invalid_admin_id(self):
        """Тест завантаження з невалідним admin ID."""
        with pytest.raises(RuntimeError):
            Settings.from_env()

    def test_settings_repr(self):
        """Тест рядкового представлення."""
        settings = Settings(
            bot_token="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            database_path="test.db",
            admin_telegram_id=123456789,
        )

        repr_str = repr(settings)
        assert "Settings" in repr_str
        assert "test.db" in repr_str
        assert "123456789" in repr_str


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
