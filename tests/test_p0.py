import tests  # installs synthetic offline prompt configuration
import asyncio
import logging
import os
import sqlite3
from contextlib import closing
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import UTC, datetime
from pathlib import Path


os.environ["TOKEN_ENCRYPTION_KEY"] = "test-only-encryption-key"
os.environ["OPENAI_API_KEY"] = "test-only-openai-key"

import database
import payments
import ai
import bot_runner
import manager_bot
import healthcheck
import runtime_health
from backup_db import backup
from healthcheck import classify_process_argv, evaluate_health, expected_clients
from logging_utils import RedactingFormatter, redact_text
from rotate_manager_token import replace_env_token
from security_utils import SecretDecryptionError, decrypt_token
from client_bot import _looks_like_order_cancel, _missing_order_fields


class P0DatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="customerserviceai-p0-")
        self.db_path = str(Path(self.tempdir.name) / "test.db")
        database.DB_PATH = self.db_path
        payments.DB_PATH = self.db_path
        await database.init_db()
        await payments.init_payments_db()
        self.shop_a = await database.create_shop(1001)
        self.shop_b = await database.create_shop(1002)

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_clients_are_tenant_scoped(self):
        await database.save_client(self.shop_a, 77, "alice-a", "Alice A")
        await database.save_client(self.shop_b, 77, "alice-b", "Alice B")
        self.assertEqual((await database.get_client(self.shop_a, 77))["username"], "alice-a")
        self.assertEqual((await database.get_client(self.shop_b, 77))["username"], "alice-b")

    async def test_duplicate_bot_identity_is_rejected(self):
        token = "123456789:" + "A" * 32
        await database.update_client_bot(self.shop_a, token, "shop_a_bot", 9001)
        with self.assertRaisesRegex(ValueError, "telegram_bot_already_connected"):
            await database.update_client_bot(self.shop_b, token, "shop_b_bot", 9002)
        with self.assertRaisesRegex(ValueError, "telegram_bot_already_connected"):
            await database.update_client_bot(self.shop_b, "987654321:" + "B" * 32, "shop_b_bot", 9001)

    async def test_update_claim_is_exactly_once(self):
        results = await asyncio.gather(*[
            database.claim_update(self.shop_a, 555) for _ in range(8)
        ])
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)
        self.assertTrue(await database.claim_update(self.shop_b, 555))
        self.assertTrue(await database.claim_update(self.shop_a, 555, source="manager"))

    async def test_zero_active_subscriptions_is_a_valid_reconcile_state(self):
        result = await bot_runner.reconcile_bots()
        self.assertEqual(result["expected"], 0)
        self.assertEqual(result["running"], 0)
        self.assertEqual(expected_clients(Path(self.db_path)), 0)
        self.assertTrue(evaluate_health(manager=1, clients=0, expected=0))
        self.assertFalse(evaluate_health(manager=1, clients=1, expected=0))

    async def test_healthcheck_counts_configured_test_shop(self):
        token = "123456789:" + "T" * 32
        await database.update_client_bot(self.shop_a, token, "test_shop_bot", 9010)
        with patch.object(healthcheck, "TEST_SHOP_ID", self.shop_a):
            self.assertEqual(healthcheck.expected_clients(Path(self.db_path)), 1)

    def test_healthcheck_ignores_shell_commands_that_mention_client_bot(self):
        shell_argv = [
            "/bin/bash",
            "-lc",
            "python3 healthcheck.py; pgrep -af /example/customerserviceai/client_bot.py",
        ]
        self.assertIsNone(classify_process_argv(shell_argv))
        self.assertEqual(
            classify_process_argv(["/usr/bin/python3", healthcheck.MAIN_PATH]),
            "manager",
        )
        self.assertEqual(
            classify_process_argv(["/usr/bin/python3", healthcheck.CLIENT_PATH, "shop-id"]),
            "client",
        )

    def test_polling_conflict_marker_blocks_health_until_cleared(self):
        with tempfile.TemporaryDirectory(prefix="customerserviceai-runtime-") as td:
            with patch.object(runtime_health, "RUNTIME_DIR", Path(td)):
                runtime_health.record_polling_conflict("manager")
                runtime_health.record_polling_conflict("client", self.shop_a)
                self.assertEqual(runtime_health.polling_conflict_count(), 2)
                self.assertEqual(runtime_health.clear_polling_conflicts(), 2)
                self.assertEqual(runtime_health.polling_conflict_count(), 0)

    async def test_order_draft_requires_tenant_and_confirmation(self):
        order_id = await database.create_order_draft(
            self.shop_a, 77, "Alice", "Consultation", "tomorrow 10:00",
            "+491234567", "", source_update_id=901,
        )
        repeated = await database.create_order_draft(
            self.shop_a, 77, "Alice", "Consultation", "tomorrow 10:00",
            "+491234567", "", source_update_id=901,
        )
        self.assertEqual(order_id, repeated)
        confirmed, _ = await database.confirm_order_draft(self.shop_b, 77, order_id)
        self.assertFalse(confirmed)
        confirmed, order = await database.confirm_order_draft(self.shop_a, 77, order_id)
        self.assertTrue(confirmed)
        self.assertEqual(order["status"], "active")
        self.assertFalse(await database.cancel_order(self.shop_b, order_id))
        self.assertTrue(await database.cancel_order(self.shop_a, order_id))

    async def test_human_pause_and_outbox_are_durable_and_idempotent(self):
        self.assertFalse(await database.is_human_paused(self.shop_a, 77))
        await database.set_human_pause(self.shop_a, 77, True, "test")
        self.assertTrue(await database.is_human_paused(self.shop_a, 77))
        self.assertTrue(await database.enqueue_outbox("evt-1", self.shop_a, "client_message", {"user_id": 77, "text": "x"}))
        self.assertFalse(await database.enqueue_outbox("evt-1", self.shop_a, "client_message", {"user_id": 77, "text": "x"}))
        events = await database.claim_outbox()
        self.assertEqual(len(events), 1)
        await database.mark_outbox_done(events[0]["id"])
        self.assertEqual(await database.claim_outbox(), [])

    async def test_manager_outbox_delivers_owner_event_and_marks_done(self):
        await database.enqueue_outbox(
            "order:test:1:created",
            self.shop_a,
            "owner_order_event",
            {
                "admin_id": 1001,
                "order_id": 1,
                "event_type": "created",
                "contact": "Alice <unsafe>",
                "order": {"item": "Service <x>", "phone": "+49123"},
            },
        )
        context = SimpleNamespace(bot=AsyncMock())
        await manager_bot.process_outbox_job(context)
        context.bot.send_message.assert_awaited_once()
        sent_text = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Alice &lt;unsafe&gt;", sent_text)
        self.assertIn("Service &lt;x&gt;", sent_text)
        self.assertEqual(await database.claim_outbox(), [])

    async def test_pending_ai_retry_has_single_lease(self):
        await database.save_pending_client_message(self.shop_a, 77, "question", "Timeout")
        claims = await asyncio.gather(*[
            database.claim_pending_client_messages(self.shop_a) for _ in range(5)
        ])
        self.assertEqual(sum(len(items) for items in claims), 1)
        claimed = next(items[0] for items in claims if items)
        await database.mark_pending_client_message_done(claimed["id"])
        self.assertEqual(await database.claim_pending_client_messages(self.shop_a), [])

    async def test_export_omits_secret_and_delete_is_tenant_scoped(self):
        token = "123456789:" + "C" * 32
        await database.update_client_bot(self.shop_a, token, "shop_a_bot", 9101)
        await database.save_client(self.shop_a, 77, "alice", "Alice")
        await database.save_client(self.shop_b, 77, "bob", "Bob")
        exported = await database.export_shop_data(self.shop_a)
        rendered = str(exported)
        self.assertNotIn(token, rendered)
        self.assertNotIn("client_bot_token", rendered)
        await database.delete_shop_data(self.shop_a)
        self.assertIsNone(await database.get_shop_by_id(self.shop_a))
        self.assertIsNotNone(await database.get_shop_by_id(self.shop_b))
        self.assertEqual((await database.get_client(self.shop_b, 77))["username"], "bob")

    async def test_paid_invoice_is_verified_and_applied_once(self):
        invoice_id = "inv-1"
        async with __import__("aiosqlite").connect(self.db_path) as conn:
            await conn.execute(
                """INSERT INTO invoices
                   (invoice_id, shop_id, amount, currency, payload, status)
                   VALUES (?, ?, 49.00, 'USD', ?, 'pending')""",
                (invoice_id, self.shop_a, self.shop_a),
            )
            await conn.commit()
        provider = {
            "invoice_id": invoice_id,
            "status": "paid",
            "amount": "49.00",
            "fiat": "USD",
            "payload": self.shop_a,
            "paid_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        }
        results = await asyncio.gather(*[
            payments.process_paid_invoice(invoice_id, self.shop_a, provider)
            for _ in range(5)
        ])
        self.assertEqual(sum(bool(r.get("processed")) for r in results), 1)
        self.assertTrue(all(r.get("processed") or r.get("reason") in {"already_processed", "concurrent_processing"} for r in results))

        local = await payments.get_local_invoice(invoice_id)
        self.assertEqual(local["status"], "paid")
        sub = await payments.get_subscription(self.shop_a)
        paid_until = datetime.fromisoformat(sub["paid_until"])
        self.assertGreater((paid_until - datetime.now(UTC).replace(tzinfo=None)).days, 28)

    async def test_paid_invoice_rejects_mismatched_payload_amount_and_shop(self):
        async with __import__("aiosqlite").connect(self.db_path) as conn:
            await conn.execute(
                """INSERT INTO invoices
                   (invoice_id, shop_id, amount, currency, payload, status)
                   VALUES ('inv-2', ?, 49.00, 'USD', ?, 'pending')""",
                (self.shop_a, self.shop_a),
            )
            await conn.commit()
        base = {"invoice_id": "inv-2", "status": "paid", "amount": "49.00", "fiat": "USD", "payload": self.shop_a}
        wrong_amount = dict(base, amount="1.00")
        wrong_payload = dict(base, payload=self.shop_b)
        self.assertEqual((await payments.process_paid_invoice("inv-2", self.shop_a, wrong_amount))["reason"], "amount_mismatch")
        self.assertEqual((await payments.process_paid_invoice("inv-2", self.shop_a, wrong_payload))["reason"], "payload_mismatch")
        self.assertEqual((await payments.process_paid_invoice("inv-2", self.shop_b, base))["reason"], "shop_mismatch")


class MigrationAndSecurityTest(unittest.IsolatedAsyncioTestCase):
    def test_manager_token_rotation_updates_env_atomically(self):
        with tempfile.TemporaryDirectory(prefix="customerserviceai-token-") as td:
            env_path = Path(td) / ".env"
            env_path.write_text("OTHER=value\nMANAGER_BOT_TOKEN=old\n", encoding="utf-8")
            token = "123456789:" + "N" * 32
            replace_env_token(env_path, token)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                f"OTHER=value\nMANAGER_BOT_TOKEN={token}\n",
            )
            self.assertEqual(list(Path(td).glob(".env.rotate-*")), [])

    async def test_customer_chat_uses_strict_structured_output(self):
        payload = {
            "reply": "Bitte bestätigen Sie die Anfrage.",
            "intent": "new_order",
            "needs_human": False,
            "order": {
                "customer_name": "Max",
                "item": "Beratung",
                "datetime_text": "morgen 10:00",
                "phone": "+4912345",
                "details": "",
            },
        }
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=__import__("json").dumps(payload)))]
        )
        with patch.object(ai.client.chat.completions, "create", AsyncMock(return_value=response)) as create:
            result = await ai.customer_chat("system", [], "Ich brauche einen Termin")
        self.assertTrue(result["ok"])
        self.assertEqual(result["intent"], "new_order")
        self.assertEqual(result["order"]["item"], "Beratung")
        self.assertEqual(
            create.await_args.kwargs["response_format"]["type"], "json_schema"
        )

    pass

    def test_child_bot_token_is_not_put_in_environment(self):
        token = "123456789:" + "D" * 32
        proc = MagicMock()
        proc.pid = 1234
        proc.poll.return_value = None
        with patch.object(bot_runner.subprocess, "Popen", return_value=proc) as popen:
            self.assertTrue(bot_runner.start_bot("test-shop", token))
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("CLIENT_BOT_TOKEN", env)
        proc.stdin.write.assert_called_once_with(token + "\n")
        bot_runner._processes.clear()

    async def test_legacy_clients_migrate_per_shop(self):
        with tempfile.TemporaryDirectory(prefix="customerserviceai-migration-") as td:
            path = str(Path(td) / "legacy.db")
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript("""
                    CREATE TABLE shops (shop_id TEXT PRIMARY KEY, admin_id INTEGER UNIQUE,
                      shop_name TEXT DEFAULT '', knowledge TEXT DEFAULT '', client_bot_token TEXT DEFAULT '',
                      client_bot_username TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                    CREATE TABLE conversations (id INTEGER PRIMARY KEY, shop_id TEXT, user_id INTEGER,
                      role TEXT, content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                    CREATE TABLE clients (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT);
                    INSERT INTO shops(shop_id, admin_id) VALUES ('a', 1), ('b', 2);
                    INSERT INTO clients(user_id, username, first_name) VALUES (77, 'legacy', 'Legacy');
                    INSERT INTO conversations(shop_id, user_id, role, content) VALUES
                      ('a', 77, 'user', 'x'), ('b', 77, 'user', 'y');
                """)
            old_path = database.DB_PATH
            try:
                database.DB_PATH = path
                await database.init_db()
                with closing(sqlite3.connect(path)) as conn:
                    rows = conn.execute("SELECT shop_id, user_id FROM clients ORDER BY shop_id").fetchall()
                    self.assertEqual(rows, [("a", 77), ("b", 77)])
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                database.DB_PATH = old_path

    def test_secret_decryption_fails_closed(self):
        with self.assertRaises(SecretDecryptionError):
            decrypt_token("not-a-valid-ciphertext")

    def test_logging_redacts_tokens(self):
        token = "123456789:" + "A" * 32
        api_key = "sk-" + "B" * 32
        redacted = redact_text(f"https://api.telegram.org/bot{token}/sendMessage key={api_key}")
        self.assertNotIn(token, redacted)
        self.assertNotIn(api_key, redacted)

    def test_logging_redacts_exception_traceback(self):
        token = "123456789:" + "X" * 32
        try:
            raise RuntimeError(f"request failed for {token}")
        except RuntimeError:
            record = logging.getLogger("test").makeRecord(
                "test", logging.ERROR, __file__, 1, "failed", (), __import__("sys").exc_info()
            )
        rendered = RedactingFormatter("%(message)s").format(record)
        self.assertNotIn(token, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_order_parser_requires_fields_and_respects_negation(self):
        self.assertEqual(
            _missing_order_fields({"item": "", "datetime_text": "", "phone": ""}),
            ["товар/послуга", "дата/час", "телефон"],
        )
        self.assertFalse(_looks_like_order_cancel("Я передумал, но заявку не отменяй"))
        self.assertTrue(_looks_like_order_cancel("Пожалуйста, отменить заявку"))

    def test_backup_and_restore_copy_passes_integrity(self):
        with tempfile.TemporaryDirectory(prefix="customerserviceai-backup-") as td:
            source = Path(td) / "source.db"
            target = Path(td) / "backup.db"
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO sample(value) VALUES ('ok')")
                conn.commit()
            backup(source, target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with closing(sqlite3.connect(target)) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
