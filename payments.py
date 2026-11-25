"""CryptoBot payment module."""
import os
import aiohttp
import logging
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime, timedelta
from dotenv import load_dotenv
import aiosqlite
from database import DB_PATH

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN")
SUBSCRIPTION_PRICE = float(os.getenv("SUBSCRIPTION_PRICE", "10"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
TEST_SHOP_ID = os.getenv("TEST_SHOP_ID", "").strip()
CRYPTOBOT_API = "https://pay.crypt.bot/api"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

HEADERS = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}


# ─── DB ─────────────────────────────────────────────────────────────────────

async def init_payments_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                shop_id TEXT PRIMARY KEY,
                trial_end TIMESTAMP,
                paid_until TIMESTAMP,
                active INTEGER DEFAULT 1,
                reminder_sent INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id TEXT PRIMARY KEY,
                shop_id TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
                payload TEXT NOT NULL DEFAULT '',
                status TEXT DEFAULT 'pending',
                provider_status TEXT DEFAULT 'active',
                paid_at TIMESTAMP DEFAULT NULL,
                processed_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for statement in [
            "ALTER TABLE invoices ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'",
            "ALTER TABLE invoices ADD COLUMN payload TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE invoices ADD COLUMN provider_status TEXT DEFAULT 'active'",
            "ALTER TABLE invoices ADD COLUMN paid_at TIMESTAMP DEFAULT NULL",
            "ALTER TABLE invoices ADD COLUMN processed_at TIMESTAMP DEFAULT NULL",
        ]:
            try:
                await db.execute(statement)
            except Exception:
                pass
        await db.execute(
            "UPDATE invoices SET payload = shop_id WHERE payload = '' OR payload IS NULL"
        )
        await db.execute(
            """UPDATE invoices SET status = 'expired', provider_status = 'expired'
               WHERE status = 'pending' AND created_at < datetime('now', '-2 day')"""
        )
        await db.commit()


async def create_subscription(shop_id: str):
    """Create trial subscription for new shop."""
    trial_end = _utcnow() + timedelta(days=TRIAL_DAYS)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO subscriptions (shop_id, trial_end, paid_until, active)
               VALUES (?, ?, NULL, 1)""",
            (shop_id, trial_end.isoformat())
        )
        await db.commit()


async def get_subscription(shop_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM subscriptions WHERE shop_id = ?", (shop_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def is_active(shop_id: str) -> bool:
    """Check if shop subscription is active."""
    if TEST_SHOP_ID and shop_id == TEST_SHOP_ID:
        return True

    sub = await get_subscription(shop_id)
    if not sub:
        return False
    now = _utcnow()
    # Check trial
    if sub["trial_end"]:
        trial_end = datetime.fromisoformat(sub["trial_end"])
        if now < trial_end:
            return True
    # Check paid
    if sub["paid_until"]:
        paid_until = datetime.fromisoformat(sub["paid_until"])
        if now < paid_until:
            return True
    return False


async def days_left(shop_id: str) -> int:
    """Days remaining on subscription."""
    if TEST_SHOP_ID and shop_id == TEST_SHOP_ID:
        return 999999

    sub = await get_subscription(shop_id)
    if not sub:
        return 0
    now = _utcnow()
    end = None
    if sub["paid_until"]:
        end = datetime.fromisoformat(sub["paid_until"])
    elif sub["trial_end"]:
        end = datetime.fromisoformat(sub["trial_end"])
    if end and end > now:
        return (end - now).days
    return 0


async def activate_paid(shop_id: str, months: int = 1):
    """Activate paid subscription for N months."""
    sub = await get_subscription(shop_id)
    now = _utcnow()

    if sub and sub["paid_until"]:
        current = datetime.fromisoformat(sub["paid_until"])
        start = max(current, now)
    else:
        start = now

    new_end = start + timedelta(days=30 * months)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO subscriptions (shop_id, trial_end, paid_until, active)
               VALUES (?, NULL, ?, 1)
               ON CONFLICT(shop_id) DO UPDATE SET paid_until = ?, active = 1""",
            (shop_id, new_end.isoformat(), new_end.isoformat())
        )
        await db.commit()


# ─── CRYPTOBOT API ──────────────────────────────────────────────────────────

async def create_invoice(shop_id: str, amount: float = None) -> dict | None:
    """Create CryptoBot invoice. Returns {pay_url, invoice_id}."""
    if amount is None:
        amount = SUBSCRIPTION_PRICE

    payload = {
        "currency_type": "fiat",
        "fiat": "USD",
        "amount": str(amount),
        "accepted_assets": "USDT,TON,BTC,ETH,SOL,TRX,LTC,BNB,DOGE,USDC",
        "description": f"CustomerServiceAI підписка на 1 місяць | {shop_id}",
        "payload": shop_id,
        "expires_in": 3600,
    }

    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.post(
                f"{CRYPTOBOT_API}/createInvoice",
                json=payload,
                headers=HEADERS
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    inv = data["result"]
                    # Save invoice
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            """INSERT INTO invoices
                               (invoice_id, shop_id, amount, currency, payload, status, provider_status)
                               VALUES (?, ?, ?, 'USD', ?, 'pending', ?)""",
                            (
                                str(inv["invoice_id"]), shop_id, amount, shop_id,
                                str(inv.get("status") or "active"),
                            )
                        )
                        await db.commit()
                    return {"pay_url": inv["bot_invoice_url"], "invoice_id": inv["invoice_id"]}
    except Exception as e:
        logger.warning("CryptoBot invoice creation failed | error_type=%s", type(e).__name__)
    return None


async def check_invoice_details(invoice_id: str) -> dict | None:
    """Fetch one provider invoice without logging credentials or full response."""
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.get(
                f"{CRYPTOBOT_API}/getInvoices",
                params={"invoice_ids": str(invoice_id)},
                headers=HEADERS
            ) as resp:
                data = await resp.json()
                if data.get("ok") and data["result"]["items"]:
                    item = data["result"]["items"][0]
                    if str(item.get("invoice_id")) == str(invoice_id):
                        return item
    except Exception as e:
        logger.warning("CryptoBot invoice check failed | error_type=%s", type(e).__name__)
    return None


async def check_invoice(invoice_id: str) -> str:
    """Check invoice status: paid / active / expired / unknown."""
    details = await check_invoice_details(invoice_id)
    if details:
        return str(details.get("status") or "unknown")
    return "unknown"


async def get_local_invoice(invoice_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?",
            (str(invoice_id),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_pending_invoices() -> list:
    """Get all unpaid pending invoices."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT i.invoice_id, i.shop_id, sh.admin_id
            FROM invoices i
            JOIN shops sh ON i.shop_id = sh.shop_id
            WHERE i.status = 'pending'
        """)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_shops_expiring_soon() -> list:
    """Get shops whose trial expires in next 24h and reminder not sent yet."""
    now = _utcnow()
    soon = now + timedelta(hours=24)
    result = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT s.shop_id, s.trial_end, s.reminder_sent, sh.admin_id, sh.shop_name
            FROM subscriptions s
            JOIN shops sh ON s.shop_id = sh.shop_id
            WHERE s.reminder_sent = 0
              AND s.paid_until IS NULL
              AND s.trial_end IS NOT NULL
              AND s.trial_end > ? AND s.trial_end < ?
        """, (now.isoformat(), soon.isoformat()))
        rows = await cur.fetchall()
        result = [dict(r) for r in rows]
    return result


async def mark_reminder_sent(shop_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET reminder_sent = 1 WHERE shop_id = ?",
            (shop_id,)
        )
        await db.commit()


def _same_amount(left, right) -> bool:
    try:
        return Decimal(str(left)).quantize(Decimal("0.01")) == Decimal(str(right)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return False


async def process_paid_invoice(invoice_id: str, shop_id: str, provider_invoice: dict | None = None) -> dict:
    """Atomically apply a verified provider invoice exactly once."""
    provider_invoice = provider_invoice or await check_invoice_details(invoice_id)
    if not provider_invoice:
        return {"processed": False, "reason": "provider_unavailable"}
    if str(provider_invoice.get("invoice_id")) != str(invoice_id):
        return {"processed": False, "reason": "provider_invoice_mismatch"}
    if provider_invoice.get("status") != "paid":
        return {"processed": False, "reason": "not_paid"}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout = 10000")
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?",
            (str(invoice_id),),
        )
        invoice = await cur.fetchone()
        if not invoice:
            await db.rollback()
            return {"processed": False, "reason": "local_invoice_missing"}
        if invoice["shop_id"] != shop_id:
            await db.rollback()
            return {"processed": False, "reason": "shop_mismatch"}
        if invoice["status"] == "paid":
            await db.rollback()
            return {"processed": False, "reason": "already_processed", "shop_id": shop_id}
        if invoice["status"] != "pending":
            await db.rollback()
            return {"processed": False, "reason": "invalid_local_status"}

        provider_payload = str(provider_invoice.get("payload") or "")
        provider_currency = str(provider_invoice.get("fiat") or provider_invoice.get("asset") or "")
        if provider_payload != invoice["payload"] or provider_payload != shop_id:
            await db.rollback()
            return {"processed": False, "reason": "payload_mismatch"}
        if provider_currency.upper() != str(invoice["currency"]).upper():
            await db.rollback()
            return {"processed": False, "reason": "currency_mismatch"}
        if not _same_amount(provider_invoice.get("amount"), invoice["amount"]):
            await db.rollback()
            return {"processed": False, "reason": "amount_mismatch"}

        now = _utcnow()
        cur = await db.execute(
            "SELECT paid_until FROM subscriptions WHERE shop_id = ?",
            (shop_id,),
        )
        sub = await cur.fetchone()
        current = datetime.fromisoformat(sub["paid_until"]) if sub and sub["paid_until"] else now
        new_end = max(current, now) + timedelta(days=30)
        await db.execute(
            """INSERT INTO subscriptions (shop_id, trial_end, paid_until, active)
               VALUES (?, NULL, ?, 1)
               ON CONFLICT(shop_id) DO UPDATE SET paid_until = excluded.paid_until, active = 1""",
            (shop_id, new_end.isoformat()),
        )
        cur = await db.execute(
            """UPDATE invoices
               SET status = 'paid', provider_status = 'paid', paid_at = ?,
                   processed_at = CURRENT_TIMESTAMP
               WHERE invoice_id = ? AND status = 'pending'""",
            (str(provider_invoice.get("paid_at") or now.isoformat()), str(invoice_id)),
        )
        if (cur.rowcount or 0) != 1:
            await db.rollback()
            return {"processed": False, "reason": "concurrent_processing"}
        await db.commit()
        return {
            "processed": True,
            "reason": "paid",
            "shop_id": shop_id,
            "paid_until": new_end.isoformat(),
        }


async def get_subscription_stats() -> dict:
    now = _utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        total = (await (await db.execute("SELECT COUNT(*) AS c FROM subscriptions")).fetchone())["c"]
        trial = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE paid_until IS NULL AND trial_end IS NOT NULL AND trial_end > ?",
            (now,)
        )).fetchone())["c"]
        paid = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE paid_until IS NOT NULL AND paid_until > ?",
            (now,)
        )).fetchone())["c"]
        expired = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE (paid_until IS NOT NULL AND paid_until <= ?) OR (paid_until IS NULL AND trial_end IS NOT NULL AND trial_end <= ?)",
            (now, now)
        )).fetchone())["c"]
        expiring_soon = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE ((paid_until IS NOT NULL AND paid_until > ? AND paid_until <= datetime(?, '+3 day')) OR (paid_until IS NULL AND trial_end IS NOT NULL AND trial_end > ? AND trial_end <= datetime(?, '+3 day'))) ",
            (now, now, now, now)
        )).fetchone())["c"]

        return {
            "total": total,
            "trial": trial,
            "paid": paid,
            "expired": expired,
            "expiring_soon": expiring_soon,
        }
