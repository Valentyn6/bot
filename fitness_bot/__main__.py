import asyncio
import logging

from .bot import main


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as exc:
        logging.critical("%s", exc)
        raise SystemExit(1) from None
