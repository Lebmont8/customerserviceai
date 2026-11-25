"""
main.py — точка входу.
Запускає всі клієнтські боти з БД, потім manager_bot.
"""
import asyncio
import logging
import signal
import sys
from dotenv import load_dotenv
import os
from logging_utils import configure_logging

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

configure_logging()
logger = logging.getLogger(__name__)

import bot_runner
import manager_bot


def secure_runtime_permissions():
    for filename in [".env", "customerserviceai.db", "customerserviceai.log"]:
        path = os.path.join(os.path.dirname(__file__), filename)
        if os.path.exists(path):
            os.chmod(path, 0o600)


def shutdown(signum, frame):
    logger.info("Shutting down...")
    bot_runner.stop_all_bots()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Запускаємо всі клієнтські боти
    logger.info("Starting client bots...")
    asyncio.run(bot_runner.start_all_bots())
    secure_runtime_permissions()

    # Новий event loop для manager bot
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Запускаємо manager bot (блокуючий виклик)
    logger.info("Starting manager bot...")
    manager_bot.main()
