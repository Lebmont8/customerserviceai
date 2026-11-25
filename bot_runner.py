"""
bot_runner.py — менеджер клієнтських ботів.
Запускає/зупиняє окремий процес для кожного магазину.
"""
import asyncio
import logging
import os
import sys
import subprocess


import database as db
from logging_utils import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

# shop_id → subprocess
_processes: dict[str, subprocess.Popen] = {}


def _bot_script_path() -> str:
    return os.path.join(os.path.dirname(__file__), "client_bot.py")


def start_bot(shop_id: str, token: str) -> bool:
    """Запустити клієнтський бот для магазину."""
    if shop_id in _processes:
        proc = _processes[shop_id]
        if proc.poll() is None:
            logger.info(f"Bot {shop_id} already running (pid {proc.pid})")
            return False
        else:
            del _processes[shop_id]

    if not token:
        logger.warning("Client bot not started: missing token | shop_id=%s", shop_id)
        return False

    env = os.environ.copy()
    env["CLIENT_SHOP_ID"] = shop_id

    proc = subprocess.Popen(
        [sys.executable, _bot_script_path()],
        env=env,
        stdin=subprocess.PIPE,
        stdout=None,
        stderr=None,
        start_new_session=True,
        text=True,
    )
    try:
        proc.stdin.write(token + "\n")
        proc.stdin.flush()
    except Exception:
        proc.terminate()
        proc.wait(timeout=5)
        raise
    finally:
        proc.stdin.close()
    _processes[shop_id] = proc
    logger.info(f"Started bot for shop {shop_id} (pid {proc.pid})")
    return True


def stop_bot(shop_id: str):
    """Зупинити клієнтський бот магазину."""
    proc = _processes.pop(shop_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        logger.info(f"Stopped bot for shop {shop_id}")


def restart_bot(shop_id: str, token: str):
    stop_bot(shop_id)
    start_bot(shop_id, token)


def is_bot_running(shop_id: str) -> bool:
    proc = _processes.get(shop_id)
    return bool(proc and proc.poll() is None)


def running_shop_ids() -> set[str]:
    dead = [shop_id for shop_id, proc in _processes.items() if proc.poll() is not None]
    for shop_id in dead:
        _processes.pop(shop_id, None)
    return set(_processes)


async def start_all_bots():
    """При старті сервера — запустити всі боти з активними підписками."""
    import payments
    await db.init_db()
    await payments.init_payments_db()

    async with __import__("aiosqlite").connect(db.DB_PATH) as conn:
        conn.row_factory = __import__("aiosqlite").Row
        cur = await conn.execute(
            "SELECT shop_id, client_bot_token FROM shops WHERE client_bot_token != ''"
        )
        rows = await cur.fetchall()

    started = 0
    for row in rows:
        shop_id = row["shop_id"]
        shop = await db.get_shop_by_id(shop_id)
        token = shop["client_bot_token"] if shop else ""
        is_active = await payments.is_active(shop_id)
        if is_active:
            start_bot(shop_id, token)
            started += 1
        else:
            logger.info(f"Skipping {shop_id} — subscription inactive")

    logger.info(f"Started {started} client bots on launch")


async def reconcile_bots() -> dict:
    """Match child processes to configured shops with active subscriptions."""
    import payments
    await db.init_db()
    await payments.init_payments_db()

    async with __import__("aiosqlite").connect(db.DB_PATH) as conn:
        conn.row_factory = __import__("aiosqlite").Row
        cur = await conn.execute(
            "SELECT shop_id FROM shops WHERE client_bot_token != ''"
        )
        configured_ids = [row["shop_id"] for row in await cur.fetchall()]

    expected: set[str] = set()
    started = 0
    for shop_id in configured_ids:
        if not await payments.is_active(shop_id):
            continue
        expected.add(shop_id)
        if not is_bot_running(shop_id):
            shop = await db.get_shop_by_id(shop_id)
            if shop and start_bot(shop_id, shop.get("client_bot_token") or ""):
                started += 1

    stopped = 0
    for shop_id in running_shop_ids() - expected:
        stop_bot(shop_id)
        stopped += 1

    return {
        "expected": len(expected),
        "running": len(running_shop_ids()),
        "started": started,
        "stopped": stopped,
    }


def stop_all_bots():
    """Зупинити всі клієнтські боти."""
    for shop_id in list(_processes.keys()):
        stop_bot(shop_id)
    logger.info("All client bots stopped")


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    asyncio.run(start_all_bots())
    logger.info("Press Ctrl+C to stop all bots")
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        stop_all_bots()
