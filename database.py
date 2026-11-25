"""Database module — multi-shop support."""
import aiosqlite
import json
import logging
import os
import random
import re
import string
from datetime import UTC, datetime
from difflib import unified_diff
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from security_utils import (
    encrypt_token,
    decrypt_token,
    is_encrypted_token,
    is_plausible_telegram_token,
    token_fingerprint,
)

logger = logging.getLogger(__name__)

DB_PATH = os.getenv(
    "CUSTOMERSERVICEAI_DB_PATH",
    os.path.join(os.path.dirname(__file__), "customerserviceai.db"),
)
MAX_USERNAME_LEN = 32
MAX_FIRST_NAME_LEN = 20
DB_BUSY_TIMEOUT_MS = 10_000
DEFAULT_MONTHLY_AI_REQUEST_LIMIT = 1000
DEFAULT_MONTHLY_AI_COST_LIMIT_USD = 20.0
ALLOWED_LANGUAGES = {"auto", "uk", "ru", "de", "en"}
ALLOWED_TONES = {"friendly", "professional", "concise"}


def _normalize_field(value: str, max_len: int) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    return value[:max_len]


def generate_shop_id() -> str:
    """Generate unique 8-char shop ID."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                shop_id TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL UNIQUE,
                shop_name TEXT DEFAULT '',
                knowledge TEXT DEFAULT '',
                client_bot_token TEXT DEFAULT '',
                client_bot_id INTEGER DEFAULT NULL,
                client_bot_fingerprint TEXT DEFAULT '',
                client_bot_username TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Міграція: додаємо колонки якщо їх ще немає (для існуючих БД)
        try:
            await db.execute("ALTER TABLE shops ADD COLUMN client_bot_token TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE shops ADD COLUMN client_bot_username TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE shops ADD COLUMN client_bot_id INTEGER DEFAULT NULL")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE shops ADD COLUMN client_bot_fingerprint TEXT DEFAULT ''")
        except Exception:
            pass
        await _backfill_token_fingerprints(db)
        await db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_shops_client_bot_id
               ON shops(client_bot_id) WHERE client_bot_id IS NOT NULL"""
        )
        await db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_shops_client_bot_fingerprint
               ON shops(client_bot_fingerprint) WHERE client_bot_fingerprint != ''"""
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_conv
            ON conversations(shop_id, user_id, created_at)
        """)
        await _migrate_clients_table(db)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                admin_id INTEGER NOT NULL,
                fire_at TIMESTAMP NOT NULL,
                is_last INTEGER DEFAULT 0,
                sent INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS spam_state (
                shop_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                last_text TEXT DEFAULT '',
                repeat_count INTEGER DEFAULT 0,
                series_count INTEGER DEFAULT 0,
                burst_count INTEGER DEFAULT 0,
                last_message_at REAL DEFAULT 0,
                ignore_until REAL DEFAULT 0,
                PRIMARY KEY (shop_id, user_id)
            )
        """)
        try:
            await db.execute("ALTER TABLE spam_state ADD COLUMN burst_count INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE spam_state ADD COLUMN last_message_at REAL DEFAULT 0")
        except Exception:
            pass
        await db.execute(
            "UPDATE spam_state SET last_text = '' WHERE last_text != '' AND length(last_text) != 64"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_error_state (
                shop_id TEXT PRIMARY KEY,
                consecutive_errors INTEGER DEFAULT 0,
                last_error_type TEXT DEFAULT '',
                last_error_at REAL DEFAULT 0,
                last_notified_at REAL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS maintenance_windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                starts_at TIMESTAMP NOT NULL,
                ends_at TIMESTAMP NOT NULL,
                message TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_client_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                claimed_at TIMESTAMP DEFAULT NULL,
                next_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for statement in [
            "ALTER TABLE pending_client_messages ADD COLUMN claimed_at TIMESTAMP DEFAULT NULL",
            "ALTER TABLE pending_client_messages ADD COLUMN next_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        ]:
            try:
                await db.execute(statement)
            except Exception:
                pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                customer_name TEXT DEFAULT '',
                item TEXT DEFAULT '',
                datetime_text TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                details TEXT DEFAULT '',
                auto_complete_at TIMESTAMP DEFAULT NULL,
                source_update_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN auto_complete_at TIMESTAMP DEFAULT NULL")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN source_update_id INTEGER DEFAULT NULL")
        except Exception:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_active ON orders(shop_id, status, updated_at)")
        await db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_source_update
               ON orders(shop_id, source_update_id) WHERE source_update_id IS NOT NULL"""
        )
        await _deduplicate_active_orders(db)
        await db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_one_active
               ON orders(shop_id, user_id) WHERE status = 'active'"""
        )
        await _migrate_processed_updates(db)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS human_pauses (
                shop_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                paused INTEGER NOT NULL DEFAULT 1,
                reason TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (shop_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                shop_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                claimed_at TIMESTAMP DEFAULT NULL,
                last_error TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, available_at, id)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                actor_id INTEGER,
                action TEXT NOT NULL,
                entity_type TEXT DEFAULT '',
                entity_id TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await _init_p1a_schema(db)
        await db.commit()


async def _init_p1a_schema(db):
    await db.execute("""
        CREATE TABLE IF NOT EXISTS shop_settings (
            shop_id TEXT PRIMARY KEY,
            language TEXT NOT NULL DEFAULT 'auto',
            tone TEXT NOT NULL DEFAULT 'friendly',
            timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
            working_hours TEXT NOT NULL DEFAULT '{}',
            required_order_fields TEXT NOT NULL DEFAULT '["customer_name","item","datetime_text","phone"]',
            ai_enabled INTEGER NOT NULL DEFAULT 0,
            test_passed INTEGER NOT NULL DEFAULT 0,
            monthly_ai_request_limit INTEGER NOT NULL DEFAULT 1000,
            monthly_ai_cost_limit_usd REAL NOT NULL DEFAULT 20.0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        INSERT OR IGNORE INTO shop_settings (shop_id, ai_enabled)
        SELECT shop_id, 1 FROM shops
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'owner',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(shop_id, version)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_versions_shop ON knowledge_versions(shop_id, version DESC)")
    await db.execute("""
        INSERT OR IGNORE INTO knowledge_versions (shop_id, version, content, source, status)
        SELECT shop_id, 1, knowledge, 'legacy_migration', 'active'
        FROM shops WHERE TRIM(knowledge) != ''
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_drafts (
            shop_id TEXT PRIMARY KEY,
            base_version INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            diff_text TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS catalog_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT '',
            availability TEXT NOT NULL DEFAULT 'unknown',
            active INTEGER NOT NULL DEFAULT 1,
            required_fields TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(shop_id, name)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_catalog_shop ON catalog_items(shop_id, active, name)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ai_usage (
            shop_id TEXT NOT NULL,
            period TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(shop_id, period)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS analytics_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            value REAL NOT NULL DEFAULT 1,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_analytics_shop_time ON analytics_events(shop_id, created_at, event_type)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS billing_profiles (
            shop_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL DEFAULT 'cryptobot',
            billing_email TEXT NOT NULL DEFAULT '',
            invoice_details TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'unconfigured',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("INSERT OR IGNORE INTO billing_profiles (shop_id) SELECT shop_id FROM shops")


async def _migrate_clients_table(db):
    cur = await db.execute("PRAGMA table_info(clients)")
    columns = {row[1] for row in await cur.fetchall()}
    if columns and "shop_id" not in columns:
        await db.execute("ALTER TABLE clients RENAME TO clients_legacy")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            shop_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (shop_id, user_id)
        )
    """)

    legacy_cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'clients_legacy'"
    )
    if await legacy_cur.fetchone():
        await db.execute("""
            INSERT OR IGNORE INTO clients (shop_id, user_id, username, first_name)
            SELECT DISTINCT c.shop_id, l.user_id, l.username, l.first_name
            FROM clients_legacy l
            JOIN conversations c ON c.user_id = l.user_id
        """)
        await db.execute("DROP TABLE clients_legacy")


async def _backfill_token_fingerprints(db):
    cur = await db.execute(
        """SELECT shop_id, client_bot_token, client_bot_fingerprint FROM shops
           WHERE client_bot_token != ''"""
    )
    seen: dict[str, str] = {}
    for shop_id, encrypted_token, stored_fingerprint in await cur.fetchall():
        fingerprint = stored_fingerprint or token_fingerprint(decrypt_token(encrypted_token))
        if fingerprint in seen:
            raise RuntimeError(
                f"Duplicate Telegram bot token configured for shops {seen[fingerprint]} and {shop_id}"
            )
        seen[fingerprint] = shop_id
        if not stored_fingerprint:
            await db.execute(
                "UPDATE shops SET client_bot_fingerprint = ? WHERE shop_id = ?",
                (fingerprint, shop_id),
            )


async def _deduplicate_active_orders(db):
    cur = await db.execute("""
        SELECT shop_id, user_id, GROUP_CONCAT(order_id) AS ids
        FROM orders
        WHERE status = 'active'
        GROUP BY shop_id, user_id
        HAVING COUNT(*) > 1
    """)
    for row in await cur.fetchall():
        ids = sorted((int(value) for value in row[2].split(',')), reverse=True)
        for order_id in ids[1:]:
            await db.execute(
                "UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                (order_id,),
            )
        logger.warning(
            "Deduplicated active orders | shop_id=%s user_id=%s kept_order_id=%s",
            row[0], row[1], ids[0],
        )


async def _migrate_processed_updates(db):
    cur = await db.execute("PRAGMA table_info(processed_updates)")
    columns = {row[1] for row in await cur.fetchall()}
    if columns and "source" not in columns:
        await db.execute("ALTER TABLE processed_updates RENAME TO processed_updates_legacy")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS processed_updates (
            source TEXT NOT NULL,
            shop_id TEXT NOT NULL,
            update_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (source, shop_id, update_id)
        )
    """)
    legacy_cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='processed_updates_legacy'"
    )
    if await legacy_cur.fetchone():
        await db.execute("""
            INSERT OR IGNORE INTO processed_updates (source, shop_id, update_id, created_at)
            SELECT 'client', shop_id, update_id, created_at FROM processed_updates_legacy
        """)
        await db.execute("DROP TABLE processed_updates_legacy")


def _decrypt_shop_row(row) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["client_bot_token"] = decrypt_token(data.get("client_bot_token", ""))
    return data


async def get_shop_by_admin(admin_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shops WHERE admin_id = ?", (admin_id,))
        row = await cur.fetchone()
        return _decrypt_shop_row(row)


async def get_shop_by_id(shop_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shops WHERE shop_id = ?", (shop_id,))
        row = await cur.fetchone()
        return _decrypt_shop_row(row)


async def create_shop(admin_id: int) -> str:
    """Create shop, return shop_id."""
    shop_id = generate_shop_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shops (shop_id, admin_id) VALUES (?, ?)",
            (shop_id, admin_id)
        )
        await db.execute(
            "INSERT INTO shop_settings (shop_id, ai_enabled) VALUES (?, 0)",
            (shop_id,),
        )
        await db.execute(
            "INSERT INTO billing_profiles (shop_id) VALUES (?)",
            (shop_id,),
        )
        await db.commit()
    return shop_id


async def update_knowledge(shop_id: str, knowledge: str):
    await commit_knowledge(shop_id, knowledge, source="legacy_update", clear_draft=True)


async def update_shop_name(shop_id: str, name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shops SET shop_name = ? WHERE shop_id = ?",
            (name, shop_id)
        )
        await db.commit()


async def get_conversation(shop_id: str, user_id: int, limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT role, content FROM conversations
               WHERE shop_id = ? AND user_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (shop_id, user_id, limit)
        )
        rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def add_message(shop_id: str, user_id: int, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO conversations (shop_id, user_id, role, content) VALUES (?,?,?,?)",
            (shop_id, user_id, role, content)
        )
        await db.execute(
            """DELETE FROM conversations
               WHERE id IN (
                   SELECT id FROM conversations
                   WHERE shop_id = ? AND user_id = ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT -1 OFFSET 70
               )""",
            (shop_id, user_id)
        )
        await db.commit()


async def update_client_bot(shop_id: str, token: str, username: str, bot_id: int):
    encrypted_token = encrypt_token(token)
    fingerprint = token_fingerprint(token)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """SELECT shop_id FROM shops
               WHERE shop_id != ? AND (client_bot_id = ? OR client_bot_fingerprint = ?)""",
            (shop_id, int(bot_id), fingerprint),
        )
        existing = await cur.fetchone()
        if existing:
            await db.rollback()
            raise ValueError("telegram_bot_already_connected")
        await db.execute(
            """UPDATE shops
               SET client_bot_token = ?, client_bot_username = ?, client_bot_id = ?,
                   client_bot_fingerprint = ?
               WHERE shop_id = ?""",
            (encrypted_token, username, int(bot_id), fingerprint, shop_id)
        )
        await db.commit()


async def save_reminder(shop_id: str, admin_id: int, fire_at, is_last: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reminders (shop_id, admin_id, fire_at, is_last) VALUES (?,?,?,?)",
            (shop_id, admin_id, fire_at.isoformat(), int(is_last))
        )
        await db.commit()


async def get_pending_reminders() -> list:
    """Нагадування які ще не відправлені."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM reminders WHERE sent = 0 ORDER BY fire_at ASC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def mark_reminder_sent(reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
        await db.commit()


async def save_client(shop_id: str, user_id: int, username: str, first_name: str):
    normalized_username = _normalize_field(username, MAX_USERNAME_LEN)
    normalized_first_name = _normalize_field(first_name, MAX_FIRST_NAME_LEN)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO clients (shop_id, user_id, username, first_name)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(shop_id, user_id) DO UPDATE SET
               username=excluded.username,
               first_name=excluded.first_name,
               updated_at=CURRENT_TIMESTAMP""",
            (shop_id, user_id, normalized_username, normalized_first_name)
        )
        await db.commit()


async def get_client(shop_id: str, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM clients WHERE shop_id = ? AND user_id = ?",
            (shop_id, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_clients(shop_id: str) -> list:
    """Список унікальних клієнтів магазину з останнім повідомленням."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT c1.user_id, MAX(c1.created_at) as last_msg,
               (SELECT content FROM conversations c2
                WHERE c2.shop_id = c1.shop_id AND c2.user_id = c1.user_id
                ORDER BY created_at DESC LIMIT 1) as last_content,
               cl.username, cl.first_name
               FROM conversations c1
               LEFT JOIN clients cl ON cl.shop_id = c1.shop_id AND cl.user_id = c1.user_id
               WHERE c1.shop_id = ? AND c1.role = 'user'
               GROUP BY c1.user_id
               ORDER BY last_msg DESC""",
            (shop_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_client_ids(shop_id: str) -> list[int]:
    """Unique client ids that wrote to this shop."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT DISTINCT user_id
               FROM conversations
               WHERE shop_id = ? AND role = 'user'
               ORDER BY user_id""",
            (shop_id,)
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


async def get_conversation_full(shop_id: str, user_id: int, limit: int = 20) -> list:
    """Повна історія переписки клієнта."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT role, content, created_at FROM conversations
               WHERE shop_id = ? AND user_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (shop_id, user_id, limit)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]


async def clear_conversation(shop_id: str, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM conversations WHERE shop_id = ? AND user_id = ?",
            (shop_id, user_id)
        )
        await db.commit()


async def clear_shop_conversations(shop_id: str) -> dict:
    """Delete chat-derived data for exactly one tenant, preserving orders and billing."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        counts = {}
        for table in [
            "conversations", "clients", "spam_state", "human_pauses",
            "pending_client_messages", "ai_error_state",
        ]:
            cur = await db.execute(f"DELETE FROM {table} WHERE shop_id = ?", (shop_id,))
            counts[table] = cur.rowcount or 0
        await db.commit()
        return counts


async def claim_update(shop_id: str, update_id: int, source: str = "client") -> bool:
    """Return True exactly once for a Telegram update within a shop."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT OR IGNORE INTO processed_updates (source, shop_id, update_id)
               VALUES (?, ?, ?)""",
            (source, shop_id, int(update_id)),
        )
        await db.commit()
        return (cur.rowcount or 0) == 1


async def is_human_paused(shop_id: str, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT paused FROM human_pauses WHERE shop_id = ? AND user_id = ?",
            (shop_id, user_id),
        )
        row = await cur.fetchone()
        return bool(row and row[0])


async def set_human_pause(shop_id: str, user_id: int, paused: bool, reason: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO human_pauses (shop_id, user_id, paused, reason)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(shop_id, user_id) DO UPDATE SET
               paused=excluded.paused,
               reason=excluded.reason,
               updated_at=CURRENT_TIMESTAMP""",
            (shop_id, user_id, int(paused), reason[:120]),
        )
        await db.commit()


async def enqueue_outbox(event_key: str, shop_id: str, topic: str, payload: dict) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT OR IGNORE INTO outbox (event_key, shop_id, topic, payload)
               VALUES (?, ?, ?, ?)""",
            (event_key, shop_id, topic, json.dumps(payload, ensure_ascii=False)),
        )
        await db.commit()
        return (cur.rowcount or 0) == 1


async def claim_outbox(limit: int = 20, stale_after_seconds: int = 300) -> list:
    """Lease pending events. A stale processing lease becomes retryable."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """UPDATE outbox
               SET status = 'pending', claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
               WHERE status = 'processing'
                 AND claimed_at < datetime('now', ?)""",
            (f'-{int(stale_after_seconds)} seconds',),
        )
        cur = await db.execute(
            """SELECT * FROM outbox
               WHERE status = 'pending' AND available_at <= CURRENT_TIMESTAMP
               ORDER BY id LIMIT ?""",
            (limit,),
        )
        rows = await cur.fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            await db.execute(
                f"""UPDATE outbox
                    SET status = 'processing', claimed_at = CURRENT_TIMESTAMP,
                        attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})""",
                ids,
            )
        await db.commit()
        return [dict(row) for row in rows]


async def mark_outbox_done(event_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE outbox SET status = 'done', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (event_id,),
        )
        await db.commit()


async def mark_outbox_failed(event_id: int, error_type: str, retry_seconds: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE outbox
               SET status = CASE WHEN attempts >= 8 THEN 'failed' ELSE 'pending' END,
                   last_error = ?, claimed_at = NULL,
                   available_at = datetime('now', ?), updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (error_type[:120], f'+{int(retry_seconds)} seconds', event_id),
        )
        await db.commit()


async def add_audit_event(
    shop_id: str,
    actor_type: str,
    actor_id: int | None,
    action: str,
    entity_type: str = "",
    entity_id: str = "",
    metadata: dict | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO audit_events
               (shop_id, actor_type, actor_id, action, entity_type, entity_id, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                shop_id, actor_type, actor_id, action,
                entity_type, entity_id,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        await db.commit()


async def get_all_shops() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT shop_id, admin_id, shop_name, knowledge,
                      client_bot_id, client_bot_username, created_at,
                      CASE WHEN client_bot_token != '' THEN 1 ELSE 0 END AS has_client_bot
               FROM shops ORDER BY created_at DESC"""
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_all_admin_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT admin_id FROM shops ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


async def create_maintenance_window(starts_at: str, ends_at: str, message: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO maintenance_windows (starts_at, ends_at, message, enabled)
               VALUES (?, ?, ?, 1)""",
            (starts_at, ends_at, message)
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_active_maintenance(now_iso: str | None = None) -> dict | None:
    now_iso = now_iso or datetime.now().isoformat(sep=" ", timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM maintenance_windows
               WHERE enabled = 1 AND starts_at <= ? AND ends_at > ?
               ORDER BY ends_at DESC LIMIT 1""",
            (now_iso, now_iso)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_latest_maintenance() -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM maintenance_windows
               WHERE enabled = 1
               ORDER BY created_at DESC, id DESC LIMIT 1"""
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def migrate_client_bot_tokens() -> int:
    migrated = 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT shop_id, client_bot_token FROM shops WHERE client_bot_token != ''")
        rows = await cur.fetchall()

        for row in rows:
            token_value = row["client_bot_token"] or ""
            if not token_value or is_encrypted_token(token_value):
                continue
            if not is_plausible_telegram_token(token_value):
                raise RuntimeError(
                    f"Refusing to migrate invalid token storage for shop {row['shop_id']}"
                )
            encrypted_token = encrypt_token(token_value)
            await db.execute(
                "UPDATE shops SET client_bot_token = ? WHERE shop_id = ?",
                (encrypted_token, row["shop_id"])
            )
            migrated += 1

        await db.commit()
    return migrated


async def get_spam_state(shop_id: str, user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM spam_state WHERE shop_id = ? AND user_id = ?",
            (shop_id, user_id)
        )
        row = await cur.fetchone()
        if row:
            return dict(row)
        return {
            "shop_id": shop_id,
            "user_id": user_id,
            "last_text": "",
            "repeat_count": 0,
            "series_count": 0,
            "burst_count": 0,
            "last_message_at": 0,
            "ignore_until": 0,
        }


async def save_spam_state(
    shop_id: str,
    user_id: int,
    last_text: str,
    repeat_count: int,
    series_count: int,
    ignore_until: float,
    burst_count: int = 0,
    last_message_at: float = 0,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO spam_state (shop_id, user_id, last_text, repeat_count, series_count, burst_count, last_message_at, ignore_until)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(shop_id, user_id) DO UPDATE SET
               last_text=excluded.last_text,
               repeat_count=excluded.repeat_count,
               series_count=excluded.series_count,
               burst_count=excluded.burst_count,
               last_message_at=excluded.last_message_at,
               ignore_until=excluded.ignore_until""",
            (shop_id, user_id, last_text, repeat_count, series_count, burst_count, last_message_at, ignore_until)
        )
        await db.commit()


async def clear_expired_spam_state(shop_id: str, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE spam_state SET ignore_until = 0 WHERE shop_id = ? AND user_id = ?",
            (shop_id, user_id)
        )
        await db.commit()


async def get_ai_error_state(shop_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM ai_error_state WHERE shop_id = ?",
            (shop_id,)
        )
        row = await cur.fetchone()
        if row:
            return dict(row)
        return {
            "shop_id": shop_id,
            "consecutive_errors": 0,
            "last_error_type": "",
            "last_error_at": 0,
            "last_notified_at": 0,
        }


async def save_ai_error_state(shop_id: str, consecutive_errors: int, last_error_type: str, last_error_at: float, last_notified_at: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO ai_error_state (shop_id, consecutive_errors, last_error_type, last_error_at, last_notified_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(shop_id) DO UPDATE SET
               consecutive_errors=excluded.consecutive_errors,
               last_error_type=excluded.last_error_type,
               last_error_at=excluded.last_error_at,
               last_notified_at=excluded.last_notified_at""",
            (shop_id, consecutive_errors, last_error_type, last_error_at, last_notified_at)
        )
        await db.commit()


async def reset_ai_error_state(shop_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ai_error_state SET consecutive_errors = 0, last_error_type = '', last_error_at = 0 WHERE shop_id = ?",
            (shop_id,)
        )
        await db.commit()


async def save_pending_client_message(shop_id: str, user_id: int, text: str, error_type: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO pending_client_messages (shop_id, user_id, text, attempts, last_error, status)
               VALUES (?, ?, ?, 0, ?, 'pending')""",
            (shop_id, user_id, text, error_type)
        )
        await db.commit()


async def get_pending_client_messages(shop_id: str, limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM pending_client_messages
               WHERE shop_id = ? AND status = 'pending' AND attempts < 5
               ORDER BY created_at ASC, id ASC
               LIMIT ?""",
            (shop_id, limit)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def claim_pending_client_messages(shop_id: str, limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("""
            UPDATE pending_client_messages
            SET status = 'pending', claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing'
              AND claimed_at < datetime('now', '-5 minutes')
        """)
        cur = await db.execute(
            """SELECT * FROM pending_client_messages
               WHERE shop_id = ? AND status = 'pending' AND attempts < 5
                 AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP)
               ORDER BY created_at ASC, id ASC LIMIT ?""",
            (shop_id, limit),
        )
        rows = await cur.fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            await db.execute(
                f"""UPDATE pending_client_messages
                    SET status = 'processing', claimed_at = CURRENT_TIMESTAMP,
                        attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})""",
                ids,
            )
        await db.commit()
        return [dict(row) for row in rows]


async def mark_pending_client_message_done(message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE pending_client_messages
               SET status = 'done', claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (message_id,)
        )
        await db.commit()


async def mark_pending_client_message_attempt(message_id: int, error_type: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE pending_client_messages
               SET status = CASE WHEN attempts >= 5 THEN 'failed' ELSE 'pending' END,
                   last_error = ?, claimed_at = NULL,
                   next_attempt_at = datetime('now', '+1 minute'),
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (error_type, message_id)
        )
        await db.commit()


async def get_active_order(shop_id: str, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE shop_id = ? AND user_id = ? AND status = 'active' ORDER BY updated_at DESC, order_id DESC LIMIT 1",
            (shop_id, user_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_latest_order(shop_id: str, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE shop_id = ? AND user_id = ? ORDER BY updated_at DESC, order_id DESC LIMIT 1",
            (shop_id, user_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_order_draft(
    shop_id: str,
    user_id: int,
    customer_name: str,
    item: str,
    datetime_text: str,
    phone: str,
    details: str,
    source_update_id: int | None = None,
) -> int:
    """Create one pending confirmation draft, idempotent per Telegram update."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        if source_update_id is not None:
            cur = await db.execute(
                "SELECT order_id FROM orders WHERE shop_id = ? AND source_update_id = ?",
                (shop_id, int(source_update_id)),
            )
            existing = await cur.fetchone()
            if existing:
                await db.commit()
                return int(existing["order_id"])

        await db.execute(
            """UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
               WHERE shop_id = ? AND user_id = ? AND status = 'pending_confirmation'""",
            (shop_id, user_id),
        )
        cur = await db.execute(
            """INSERT INTO orders
               (shop_id, user_id, status, customer_name, item, datetime_text, phone, details, source_update_id)
               VALUES (?, ?, 'pending_confirmation', ?, ?, ?, ?, ?, ?)""",
            (
                shop_id, user_id, customer_name, item, datetime_text,
                phone, details, source_update_id,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def confirm_order_draft(shop_id: str, user_id: int, order_id: int) -> tuple[bool, dict | None]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """SELECT * FROM orders
               WHERE shop_id = ? AND user_id = ? AND order_id = ?
                 AND status = 'pending_confirmation'""",
            (shop_id, user_id, order_id),
        )
        draft = await cur.fetchone()
        if not draft:
            await db.rollback()
            return False, None
        await db.execute(
            """UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
               WHERE shop_id = ? AND user_id = ? AND status = 'active'""",
            (shop_id, user_id),
        )
        cur = await db.execute(
            """UPDATE orders SET status = 'active', updated_at = CURRENT_TIMESTAMP
               WHERE shop_id = ? AND user_id = ? AND order_id = ?
                 AND status = 'pending_confirmation'""",
            (shop_id, user_id, order_id),
        )
        await db.commit()
        if (cur.rowcount or 0) != 1:
            return False, None
        result = dict(draft)
        result["status"] = "active"
        return True, result


async def reject_order_draft(shop_id: str, user_id: int, order_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
               WHERE shop_id = ? AND user_id = ? AND order_id = ?
                 AND status = 'pending_confirmation'""",
            (shop_id, user_id, order_id),
        )
        await db.commit()
        return (cur.rowcount or 0) == 1


async def upsert_active_order(shop_id: str, user_id: int, customer_name: str, item: str, datetime_text: str, phone: str, details: str, auto_complete_at: str | None = None) -> tuple[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT order_id FROM orders WHERE shop_id = ? AND user_id = ? AND status = 'active' ORDER BY updated_at DESC, order_id DESC LIMIT 1",
            (shop_id, user_id)
        )
        row = await cur.fetchone()
        if row:
            order_id = row["order_id"]
            await db.execute(
                """UPDATE orders
                   SET customer_name = ?, item = ?, datetime_text = ?, phone = ?, details = ?, auto_complete_at = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE shop_id = ? AND order_id = ?""",
                (customer_name, item, datetime_text, phone, details, auto_complete_at, shop_id, order_id)
            )
            await db.commit()
            return "updated", order_id

        cur = await db.execute(
            """INSERT INTO orders (shop_id, user_id, status, customer_name, item, datetime_text, phone, details, auto_complete_at)
               VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
            (shop_id, user_id, customer_name, item, datetime_text, phone, details, auto_complete_at)
        )
        await db.commit()
        return "created", cur.lastrowid


async def cancel_active_order(shop_id: str, user_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT order_id FROM orders WHERE shop_id = ? AND user_id = ? AND status = 'active' ORDER BY updated_at DESC, order_id DESC LIMIT 1",
            (shop_id, user_id)
        )
        row = await cur.fetchone()
        if not row:
            return None
        order_id = row["order_id"]
        await db.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
            (order_id,)
        )
        await db.commit()
        return order_id


async def list_active_orders(shop_id: str, limit: int = 50) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM orders
               WHERE shop_id = ? AND status = 'active'
               ORDER BY
                 CASE WHEN auto_complete_at IS NULL THEN 1 ELSE 0 END,
                 auto_complete_at ASC,
                 created_at ASC,
                 order_id ASC
               LIMIT ?""",
            (shop_id, limit)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_order(shop_id: str, order_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE shop_id = ? AND order_id = ?",
            (shop_id, order_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def complete_order(shop_id: str, order_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE orders SET status = 'completed', updated_at = CURRENT_TIMESTAMP
               WHERE shop_id = ? AND order_id = ? AND status = 'active'""",
            (shop_id, order_id)
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def cancel_order(shop_id: str, order_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
               WHERE shop_id = ? AND order_id = ? AND status = 'active'""",
            (shop_id, order_id)
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def get_orders_for_autocomplete() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE status = 'active' AND auto_complete_at IS NOT NULL"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def cleanup_old_conversations(days: int = 14) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        conv_cur = await db.execute(
            """DELETE FROM conversations
               WHERE (shop_id, user_id) IN (
                   SELECT shop_id, user_id
                   FROM conversations
                   GROUP BY shop_id, user_id
                   HAVING MAX(created_at) < datetime('now', ?)
               )""",
            (f'-{days} day',)
        )
        spam_cutoff = datetime.utcnow().timestamp() - (days * 24 * 60 * 60)
        await db.execute(
            "DELETE FROM spam_state WHERE ignore_until > 0 AND ignore_until < ?",
            (spam_cutoff,)
        )
        await db.execute(
            "DELETE FROM ai_error_state WHERE last_error_at > 0 AND last_error_at < ? AND consecutive_errors = 0",
            (spam_cutoff,)
        )
        await db.execute(
            "DELETE FROM orders WHERE status != 'active' AND updated_at < datetime('now', ?)",
            (f'-{days} day',)
        )
        await db.execute(
            """DELETE FROM pending_client_messages
               WHERE status IN ('done', 'failed') AND updated_at < datetime('now', ?)""",
            (f'-{days} day',),
        )
        await db.execute("DELETE FROM processed_updates WHERE created_at < datetime('now', '-30 day')")
        await db.execute("DELETE FROM outbox WHERE status = 'done' AND updated_at < datetime('now', '-30 day')")
        await db.execute("DELETE FROM audit_events WHERE created_at < datetime('now', '-90 day')")
        await db.execute("""
            DELETE FROM clients
            WHERE NOT EXISTS (
                SELECT 1 FROM conversations c
                WHERE c.shop_id = clients.shop_id AND c.user_id = clients.user_id
            )
              AND NOT EXISTS (
                SELECT 1 FROM orders o
                WHERE o.shop_id = clients.shop_id AND o.user_id = clients.user_id
            )
        """)
        await db.commit()
        return conv_cur.rowcount or 0


def _json_list(value: str | list | None, default: list[str] | None = None) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        try:
            raw = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            raw = default or []
    if not isinstance(raw, list):
        raw = default or []
    return [str(item).strip() for item in raw if str(item).strip()]


async def get_shop_settings(shop_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shop_settings WHERE shop_id = ?", (shop_id,))
        row = await cur.fetchone()
        if not row:
            raise ValueError("shop_not_found")
        result = dict(row)
        try:
            result["working_hours"] = json.loads(result["working_hours"] or "{}")
        except json.JSONDecodeError:
            result["working_hours"] = {}
        result["required_order_fields"] = _json_list(result.get("required_order_fields"))
        result["ai_enabled"] = bool(result["ai_enabled"])
        result["test_passed"] = bool(result["test_passed"])
        return result


async def update_shop_settings(shop_id: str, **changes) -> dict:
    allowed = {
        "language", "tone", "timezone", "working_hours", "required_order_fields",
        "ai_enabled", "test_passed", "monthly_ai_request_limit", "monthly_ai_cost_limit_usd",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError("unsupported_setting")
    if "language" in changes and changes["language"] not in ALLOWED_LANGUAGES:
        raise ValueError("invalid_language")
    if "tone" in changes and changes["tone"] not in ALLOWED_TONES:
        raise ValueError("invalid_tone")
    if "timezone" in changes:
        try:
            ZoneInfo(str(changes["timezone"]))
        except ZoneInfoNotFoundError as exc:
            raise ValueError("invalid_timezone") from exc
    if "working_hours" in changes:
        if not isinstance(changes["working_hours"], dict):
            raise ValueError("invalid_working_hours")
        changes["working_hours"] = json.dumps(changes["working_hours"], ensure_ascii=False, sort_keys=True)
    if "required_order_fields" in changes:
        fields = _json_list(changes["required_order_fields"])
        valid = {"customer_name", "item", "datetime_text", "phone", "details"}
        if not fields or not set(fields) <= valid:
            raise ValueError("invalid_required_order_fields")
        changes["required_order_fields"] = json.dumps(fields, ensure_ascii=False)
    if "monthly_ai_request_limit" in changes:
        value = int(changes["monthly_ai_request_limit"])
        if value < 0 or value > 1_000_000:
            raise ValueError("invalid_request_limit")
        changes["monthly_ai_request_limit"] = value
    if "monthly_ai_cost_limit_usd" in changes:
        value = float(changes["monthly_ai_cost_limit_usd"])
        if value < 0 or value > 100_000:
            raise ValueError("invalid_cost_limit")
        changes["monthly_ai_cost_limit_usd"] = value
    for flag in ("ai_enabled", "test_passed"):
        if flag in changes:
            changes[flag] = int(bool(changes[flag]))
    if not changes:
        return await get_shop_settings(shop_id)
    assignments = ", ".join(f"{key} = ?" for key in changes)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE shop_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE shop_id = ?",
            (*changes.values(), shop_id),
        )
        if not cur.rowcount:
            raise ValueError("shop_not_found")
        await db.commit()
    return await get_shop_settings(shop_id)


async def get_onboarding_status(shop_id: str) -> dict:
    shop = await get_shop_by_id(shop_id)
    if not shop:
        raise ValueError("shop_not_found")
    settings = await get_shop_settings(shop_id)
    status = {
        "bot_connected": bool(shop.get("client_bot_id") and shop.get("client_bot_username")),
        "knowledge_ready": bool((shop.get("knowledge") or "").strip()),
        "test_passed": bool(settings["test_passed"]),
        "ai_enabled": bool(settings["ai_enabled"]),
    }
    status["ready_to_enable"] = all(status[key] for key in ("bot_connected", "knowledge_ready", "test_passed"))
    status["complete"] = status["ready_to_enable"] and status["ai_enabled"]
    return status


async def mark_onboarding_test_passed(shop_id: str, passed: bool = True) -> dict:
    return await update_shop_settings(shop_id, test_passed=passed)


async def set_shop_ai_enabled(shop_id: str, enabled: bool) -> dict:
    if enabled:
        status = await get_onboarding_status(shop_id)
        if not status["ready_to_enable"]:
            raise ValueError("onboarding_incomplete")
    return await update_shop_settings(shop_id, ai_enabled=enabled)


async def create_knowledge_draft(shop_id: str, content: str) -> dict:
    content = (content or "").strip()
    if not content:
        raise ValueError("empty_knowledge")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT knowledge FROM shops WHERE shop_id = ?", (shop_id,))).fetchone()
        if not row:
            raise ValueError("shop_not_found")
        version_row = await (await db.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM knowledge_versions WHERE shop_id = ?",
            (shop_id,),
        )).fetchone()
        base_version = int(version_row["version"])
        old = row["knowledge"] or ""
        diff_text = "\n".join(unified_diff(
            old.splitlines(), content.splitlines(), fromfile=f"v{base_version}",
            tofile="draft", lineterm="",
        ))
        await db.execute(
            """INSERT INTO knowledge_drafts (shop_id, base_version, content, diff_text)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(shop_id) DO UPDATE SET base_version=excluded.base_version,
               content=excluded.content, diff_text=excluded.diff_text, created_at=CURRENT_TIMESTAMP""",
            (shop_id, base_version, content, diff_text),
        )
        await db.commit()
        return {"shop_id": shop_id, "base_version": base_version, "content": content, "diff": diff_text}


async def get_knowledge_draft(shop_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM knowledge_drafts WHERE shop_id = ?", (shop_id,))).fetchone()
        return dict(row) if row else None


async def delete_knowledge_draft(shop_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM knowledge_drafts WHERE shop_id = ?", (shop_id,))
        await db.commit()


async def commit_knowledge(
    shop_id: str, content: str, source: str = "owner", clear_draft: bool = False,
) -> int:
    content = (content or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        exists = await (await db.execute("SELECT 1 FROM shops WHERE shop_id = ?", (shop_id,))).fetchone()
        if not exists:
            await db.rollback()
            raise ValueError("shop_not_found")
        row = await (await db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM knowledge_versions WHERE shop_id = ?",
            (shop_id,),
        )).fetchone()
        version = int(row[0]) + 1
        await db.execute("UPDATE knowledge_versions SET status = 'superseded' WHERE shop_id = ? AND status = 'active'", (shop_id,))
        await db.execute(
            "INSERT INTO knowledge_versions (shop_id, version, content, source, status) VALUES (?, ?, ?, ?, 'active')",
            (shop_id, version, content, source[:40]),
        )
        await db.execute("UPDATE shops SET knowledge = ? WHERE shop_id = ?", (content, shop_id))
        if clear_draft:
            await db.execute("DELETE FROM knowledge_drafts WHERE shop_id = ?", (shop_id,))
        await db.commit()
        return version


async def commit_knowledge_draft(shop_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        draft = await (await db.execute(
            "SELECT * FROM knowledge_drafts WHERE shop_id = ?", (shop_id,)
        )).fetchone()
        if not draft:
            await db.rollback()
            raise ValueError("draft_not_found")
        current = await (await db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM knowledge_versions WHERE shop_id = ?", (shop_id,)
        )).fetchone()
        current_version = int(current[0])
        if current_version != int(draft["base_version"]):
            await db.rollback()
            raise ValueError("stale_draft")
        version = current_version + 1
        await db.execute(
            "UPDATE knowledge_versions SET status = 'superseded' WHERE shop_id = ? AND status = 'active'",
            (shop_id,),
        )
        await db.execute(
            "INSERT INTO knowledge_versions (shop_id, version, content, source, status) VALUES (?, ?, ?, 'owner_preview', 'active')",
            (shop_id, version, draft["content"]),
        )
        await db.execute("UPDATE shops SET knowledge = ? WHERE shop_id = ?", (draft["content"], shop_id))
        await db.execute("DELETE FROM knowledge_drafts WHERE shop_id = ?", (shop_id,))
        await db.commit()
        return version


async def list_knowledge_versions(shop_id: str, limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT version, source, status, created_at FROM knowledge_versions WHERE shop_id = ? ORDER BY version DESC LIMIT ?",
            (shop_id, max(1, min(int(limit), 50))),
        )).fetchall()
        return [dict(row) for row in rows]


async def rollback_knowledge(shop_id: str, version: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT content FROM knowledge_versions WHERE shop_id = ? AND version = ?", (shop_id, int(version))
        )).fetchone()
    if not row:
        raise ValueError("knowledge_version_not_found")
    return await commit_knowledge(shop_id, row[0], source=f"rollback_v{int(version)}")


async def upsert_catalog_item(
    shop_id: str, name: str, description: str = "", price: str = "",
    currency: str = "", availability: str = "unknown", required_fields: list[str] | None = None,
    item_id: int | None = None,
) -> int:
    name = re.sub(r"\s+", " ", (name or "").strip())[:120]
    if not name:
        raise ValueError("catalog_name_required")
    if availability not in {"in_stock", "out_of_stock", "preorder", "service", "unknown"}:
        raise ValueError("invalid_availability")
    fields = _json_list(required_fields or [])
    valid = {"customer_name", "item", "datetime_text", "phone", "details"}
    if not set(fields) <= valid:
        raise ValueError("invalid_required_order_fields")
    async with aiosqlite.connect(DB_PATH) as db:
        if item_id is not None:
            cur = await db.execute(
                """UPDATE catalog_items SET name = ?, description = ?, price = ?, currency = ?,
                   availability = ?, required_fields = ?, active = 1, updated_at = CURRENT_TIMESTAMP
                   WHERE shop_id = ? AND item_id = ?""",
                (
                    name, description[:1000], str(price)[:60], currency.upper()[:8],
                    availability, json.dumps(fields), shop_id, int(item_id),
                ),
            )
            if not cur.rowcount:
                await db.rollback()
                raise ValueError("catalog_item_not_found")
            await db.commit()
            return int(item_id)
        cur = await db.execute(
            """INSERT INTO catalog_items
               (shop_id, name, description, price, currency, availability, required_fields)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(shop_id, name) DO UPDATE SET description=excluded.description,
               price=excluded.price, currency=excluded.currency, availability=excluded.availability,
               required_fields=excluded.required_fields, active=1, updated_at=CURRENT_TIMESTAMP""",
            (shop_id, name, description[:1000], str(price)[:60], currency.upper()[:8], availability, json.dumps(fields)),
        )
        await db.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = await (await db.execute("SELECT item_id FROM catalog_items WHERE shop_id = ? AND name = ?", (shop_id, name))).fetchone()
        return int(row[0])


async def list_catalog_items(shop_id: str, active_only: bool = True) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM catalog_items WHERE shop_id = ?"
        params: list = [shop_id]
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY name"
        rows = await (await db.execute(sql, params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["required_fields"] = _json_list(item.get("required_fields"))
            result.append(item)
        return result


async def deactivate_catalog_item(shop_id: str, item_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE catalog_items SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE shop_id = ? AND item_id = ?",
            (shop_id, int(item_id)),
        )
        await db.commit()
        return bool(cur.rowcount)


def current_usage_period(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


async def get_ai_usage(shop_id: str, period: str | None = None) -> dict:
    period = period or current_usage_period()
    settings = await get_shop_settings(shop_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM ai_usage WHERE shop_id = ? AND period = ?", (shop_id, period)
        )).fetchone()
    usage = dict(row) if row else {
        "shop_id": shop_id, "period": period, "request_count": 0,
        "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0,
    }
    usage["request_limit"] = settings["monthly_ai_request_limit"]
    usage["cost_limit_usd"] = settings["monthly_ai_cost_limit_usd"]
    usage["allowed"] = (
        usage["request_count"] < usage["request_limit"]
        and float(usage["estimated_cost_usd"]) < float(usage["cost_limit_usd"])
    )
    return usage


async def reserve_ai_request(shop_id: str) -> bool:
    period = current_usage_period()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        settings = await (await db.execute(
            "SELECT monthly_ai_request_limit, monthly_ai_cost_limit_usd FROM shop_settings WHERE shop_id = ?",
            (shop_id,),
        )).fetchone()
        if not settings:
            await db.rollback()
            raise ValueError("shop_not_found")
        usage = await (await db.execute(
            "SELECT request_count, estimated_cost_usd FROM ai_usage WHERE shop_id = ? AND period = ?",
            (shop_id, period),
        )).fetchone()
        request_count = int(usage["request_count"]) if usage else 0
        cost = float(usage["estimated_cost_usd"]) if usage else 0.0
        if request_count >= int(settings["monthly_ai_request_limit"]) or cost >= float(settings["monthly_ai_cost_limit_usd"]):
            await db.rollback()
            return False
        await db.execute(
            """INSERT INTO ai_usage (shop_id, period, request_count)
               VALUES (?, ?, 1)
               ON CONFLICT(shop_id, period) DO UPDATE SET
               request_count=request_count+1, updated_at=CURRENT_TIMESTAMP""",
            (shop_id, period),
        )
        await db.commit()
        return True


async def record_ai_usage(
    shop_id: str, input_tokens: int, output_tokens: int,
    estimated_cost_usd: float, count_request: bool = True,
) -> dict:
    period = current_usage_period()
    request_increment = 1 if count_request else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO ai_usage (shop_id, period, request_count, input_tokens, output_tokens, estimated_cost_usd)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(shop_id, period) DO UPDATE SET
               request_count=request_count+excluded.request_count, input_tokens=input_tokens+excluded.input_tokens,
               output_tokens=output_tokens+excluded.output_tokens,
               estimated_cost_usd=estimated_cost_usd+excluded.estimated_cost_usd,
               updated_at=CURRENT_TIMESTAMP""",
            (shop_id, period, request_increment, max(0, int(input_tokens)), max(0, int(output_tokens)), max(0.0, float(estimated_cost_usd))),
        )
        await db.commit()
    return await get_ai_usage(shop_id, period)


async def add_analytics_event(shop_id: str, event_type: str, value: float = 1, metadata: dict | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO analytics_events (shop_id, event_type, value, metadata) VALUES (?, ?, ?, ?)",
            (shop_id, event_type[:60], float(value), json.dumps(metadata or {}, ensure_ascii=False)),
        )
        await db.commit()


async def get_analytics_summary(shop_id: str, days: int = 30) -> dict:
    days = max(1, min(int(days), 365))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT event_type, COUNT(*) AS events, SUM(value) AS total
               FROM analytics_events WHERE shop_id = ? AND created_at >= datetime('now', ?)
               GROUP BY event_type ORDER BY event_type""",
            (shop_id, f"-{days} day"),
        )).fetchall()
    result = {row["event_type"]: {"events": row["events"], "total": row["total"]} for row in rows}
    result["period_days"] = days
    result["ai_usage"] = await get_ai_usage(shop_id)
    return result


async def update_billing_profile(shop_id: str, provider: str, billing_email: str = "", invoice_details: dict | None = None) -> dict:
    if provider not in {"cryptobot", "manual_invoice", "card_placeholder"}:
        raise ValueError("unsupported_billing_provider")
    status = "ready" if provider == "cryptobot" else "prepared"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE billing_profiles SET provider = ?, billing_email = ?, invoice_details = ?,
               status = ?, updated_at = CURRENT_TIMESTAMP WHERE shop_id = ?""",
            (provider, billing_email.strip()[:254], json.dumps(invoice_details or {}, ensure_ascii=False), status, shop_id),
        )
        await db.commit()
    return await get_billing_profile(shop_id)


async def get_billing_profile(shop_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM billing_profiles WHERE shop_id = ?", (shop_id,))).fetchone()
        if not row:
            raise ValueError("shop_not_found")
        result = dict(row)
        try:
            result["invoice_details"] = json.loads(result["invoice_details"] or "{}")
        except json.JSONDecodeError:
            result["invoice_details"] = {}
        return result


async def get_shop_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        total = (await (await db.execute("SELECT COUNT(*) AS c FROM shops")).fetchone())["c"]
        with_bot = (await (await db.execute("SELECT COUNT(*) AS c FROM shops WHERE client_bot_token != ''")).fetchone())["c"]
        without_bot = total - with_bot
        with_knowledge = (await (await db.execute("SELECT COUNT(*) AS c FROM shops WHERE TRIM(knowledge) != ''")).fetchone())["c"]
        conversations = (await (await db.execute("SELECT COUNT(*) AS c FROM conversations")).fetchone())["c"]
        clients = (await (await db.execute("SELECT COUNT(DISTINCT user_id) AS c FROM conversations WHERE role = 'user' ")).fetchone())["c"]

        return {
            "total": total,
            "with_bot": with_bot,
            "without_bot": without_bot,
            "with_knowledge": with_knowledge,
            "conversations": conversations,
            "clients": clients,
        }


async def export_shop_data(shop_id: str) -> dict:
    """Export one tenant without service secrets or other tenants' rows."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT shop_id, admin_id, shop_name, knowledge,
                      client_bot_id, client_bot_username, created_at
               FROM shops WHERE shop_id = ?""",
            (shop_id,),
        )
        shop = await cur.fetchone()
        if not shop:
            raise ValueError("shop_not_found")

        result = {"shop": dict(shop)}
        table_queries = {
            "clients": "SELECT * FROM clients WHERE shop_id = ? ORDER BY user_id",
            "conversations": "SELECT * FROM conversations WHERE shop_id = ? ORDER BY id",
            "orders": "SELECT * FROM orders WHERE shop_id = ? ORDER BY order_id",
            "subscriptions": "SELECT * FROM subscriptions WHERE shop_id = ?",
            "invoices": """SELECT invoice_id, shop_id, amount, currency, status,
                                  provider_status, paid_at, processed_at, created_at
                           FROM invoices WHERE shop_id = ? ORDER BY created_at""",
            "human_pauses": "SELECT * FROM human_pauses WHERE shop_id = ? ORDER BY user_id",
            "audit_events": "SELECT * FROM audit_events WHERE shop_id = ? ORDER BY id",
            "shop_settings": "SELECT * FROM shop_settings WHERE shop_id = ?",
            "knowledge_versions": "SELECT * FROM knowledge_versions WHERE shop_id = ? ORDER BY version",
            "catalog_items": "SELECT * FROM catalog_items WHERE shop_id = ? ORDER BY item_id",
            "ai_usage": "SELECT * FROM ai_usage WHERE shop_id = ? ORDER BY period",
            "analytics_events": "SELECT * FROM analytics_events WHERE shop_id = ? ORDER BY event_id",
            "billing_profiles": "SELECT * FROM billing_profiles WHERE shop_id = ?",
        }
        for key, sql in table_queries.items():
            rows = await (await db.execute(sql, (shop_id,))).fetchall()
            result[key] = [dict(row) for row in rows]
        return result


async def delete_shop_data(shop_id: str) -> dict:
    """Delete exactly one tenant and all tenant-owned rows in one transaction."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("BEGIN IMMEDIATE")
        tables = [
            "conversations", "clients", "reminders", "spam_state", "ai_error_state",
            "pending_client_messages", "orders", "processed_updates", "human_pauses",
            "outbox", "audit_events", "subscriptions", "invoices",
            "shop_settings", "knowledge_versions", "knowledge_drafts", "catalog_items",
            "ai_usage", "analytics_events", "billing_profiles",
        ]
        counts = {}
        for table in tables:
            cur = await db.execute(f"DELETE FROM {table} WHERE shop_id = ?", (shop_id,))
            counts[table] = cur.rowcount or 0
        cur = await db.execute("DELETE FROM shops WHERE shop_id = ?", (shop_id,))
        counts["shops"] = cur.rowcount or 0
        if counts["shops"] != 1:
            await db.rollback()
            raise ValueError("shop_not_found")
        await db.commit()
        return counts
