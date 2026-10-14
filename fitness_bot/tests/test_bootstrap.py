import asyncio

from aiogram import Dispatcher

from fitness_bot import bot as bot_module
from fitness_bot.config import Settings


def test_application_bootstrap_without_telegram_network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bot_module.Settings,
        "from_env",
        classmethod(lambda cls: Settings("123456:local-test-token", str(tmp_path / "bootstrap.sqlite3"), 123456)),
    )
    monkeypatch.setattr(bot_module, "setup_logging", lambda level: None)

    async def stop_before_polling(self, bot, **kwargs):
        assert len(self.sub_routers) == 3

    monkeypatch.setattr(Dispatcher, "start_polling", stop_before_polling)

    asyncio.run(bot_module.main())
