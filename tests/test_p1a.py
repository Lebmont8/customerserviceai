import tests  # installs synthetic offline prompt configuration
import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


os.environ["TOKEN_ENCRYPTION_KEY"] = "test-only-encryption-key"
os.environ["OPENAI_API_KEY"] = "test-only-openai-key"

import billing
import database
import payments
import ai
import manager_bot


class P1ADatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="customerserviceai-p1a-")
        self.db_path = str(Path(self.tempdir.name) / "test.db")
        database.DB_PATH = self.db_path
        payments.DB_PATH = self.db_path
        await database.init_db()
        await payments.init_payments_db()
        self.shop_a = await database.create_shop(2001)
        self.shop_b = await database.create_shop(2002)

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_onboarding_defaults_are_fail_closed_for_new_shop(self):
        status = await database.get_onboarding_status(self.shop_a)
        self.assertEqual(status, {
            "bot_connected": False,
            "knowledge_ready": False,
            "test_passed": False,
            "ai_enabled": False,
            "ready_to_enable": False,
            "complete": False,
        })
        with self.assertRaisesRegex(ValueError, "onboarding_incomplete"):
            await database.set_shop_ai_enabled(self.shop_a, True)

    async def test_settings_validation_and_tenant_isolation(self):
        updated = await database.update_shop_settings(
            self.shop_a,
            language="de",
            tone="professional",
            timezone="Europe/Berlin",
            working_hours={"mon": "09:00-18:00"},
            required_order_fields=["item", "phone"],
        )
        self.assertEqual(updated["language"], "de")
        self.assertEqual(updated["working_hours"]["mon"], "09:00-18:00")
        self.assertEqual((await database.get_shop_settings(self.shop_b))["language"], "auto")
        with self.assertRaisesRegex(ValueError, "invalid_timezone"):
            await database.update_shop_settings(self.shop_a, timezone="Moon/Base")

    pass

    async def test_ai_can_only_enable_after_all_readiness_steps(self):
        token = "123456789:" + "Z" * 32
        await database.update_client_bot(self.shop_a, token, "ready_bot", 99001)
        await database.commit_knowledge(self.shop_a, "Fact", source="test")
        await database.mark_onboarding_test_passed(self.shop_a)
        settings = await database.set_shop_ai_enabled(self.shop_a, True)
        self.assertTrue(settings["ai_enabled"])
        self.assertTrue((await database.get_onboarding_status(self.shop_a))["complete"])

    async def test_knowledge_preview_commit_versions_and_rollback(self):
        draft = await database.create_knowledge_draft(self.shop_a, "Назва бізнесу: Alpha")
        self.assertIn("+Назва бізнесу: Alpha", draft["diff"])
        version_one = await database.commit_knowledge_draft(self.shop_a)
        self.assertEqual(version_one, 1)
        await database.create_knowledge_draft(self.shop_a, "Назва бізнесу: Beta")
        version_two = await database.commit_knowledge_draft(self.shop_a)
        self.assertEqual(version_two, 2)
        rollback_version = await database.rollback_knowledge(self.shop_a, 1)
        self.assertEqual(rollback_version, 3)
        self.assertEqual((await database.get_shop_by_id(self.shop_a))["knowledge"], "Назва бізнесу: Alpha")
        self.assertEqual(len(await database.list_knowledge_versions(self.shop_a)), 3)

    async def test_stale_knowledge_draft_is_rejected(self):
        await database.create_knowledge_draft(self.shop_a, "Draft")
        await database.commit_knowledge(self.shop_a, "Concurrent", source="test")
        with self.assertRaisesRegex(ValueError, "stale_draft"):
            await database.commit_knowledge_draft(self.shop_a)

    async def test_catalog_is_tenant_scoped(self):
        item_id = await database.upsert_catalog_item(
            self.shop_a, "Consultation", price="49", currency="EUR",
            availability="service", required_fields=["item", "phone"],
        )
        self.assertEqual(len(await database.list_catalog_items(self.shop_a)), 1)
        self.assertEqual(await database.list_catalog_items(self.shop_b), [])
        self.assertFalse(await database.deactivate_catalog_item(self.shop_b, item_id))
        self.assertTrue(await database.deactivate_catalog_item(self.shop_a, item_id))

    async def test_usage_limits_and_cost_are_tenant_scoped(self):
        await database.update_shop_settings(
            self.shop_a, monthly_ai_request_limit=2, monthly_ai_cost_limit_usd=0.02
        )
        first = await database.record_ai_usage(self.shop_a, 100, 20, 0.01)
        self.assertTrue(first["allowed"])
        second = await database.record_ai_usage(self.shop_a, 100, 20, 0.01)
        self.assertFalse(second["allowed"])
        self.assertTrue((await database.get_ai_usage(self.shop_b))["allowed"])

    async def test_usage_reservation_is_atomic(self):
        await database.update_shop_settings(
            self.shop_a, monthly_ai_request_limit=3, monthly_ai_cost_limit_usd=10
        )
        reservations = await asyncio.gather(*[
            database.reserve_ai_request(self.shop_a) for _ in range(12)
        ])
        self.assertEqual(reservations.count(True), 3)
        self.assertEqual((await database.get_ai_usage(self.shop_a))["request_count"], 3)

    async def test_analytics_summary_is_tenant_scoped(self):
        await database.add_analytics_event(self.shop_a, "inquiry")
        await database.add_analytics_event(self.shop_a, "handoff")
        await database.add_analytics_event(self.shop_b, "inquiry")
        summary_a = await database.get_analytics_summary(self.shop_a)
        self.assertEqual(summary_a["inquiry"]["events"], 1)
        self.assertEqual(summary_a["handoff"]["events"], 1)
        self.assertNotIn("handoff", await database.get_analytics_summary(self.shop_b))

    async def test_billing_placeholders_never_collect(self):
        request = billing.prepare_billing_request(self.shop_a, "card_placeholder", "49", "eur")
        self.assertFalse(request.collectable)
        profile = await database.update_billing_profile(
            self.shop_a, "manual_invoice", "billing@example.test", {"company": "Test"}
        )
        self.assertEqual(profile["status"], "prepared")
        self.assertEqual((await database.get_billing_profile(self.shop_b))["status"], "unconfigured")

    async def test_export_and_delete_cover_p1a_tables(self):
        await database.create_knowledge_draft(self.shop_a, "Knowledge")
        await database.upsert_catalog_item(self.shop_a, "Product")
        await database.record_ai_usage(self.shop_a, 10, 5, 0.001)
        await database.add_analytics_event(self.shop_a, "inquiry")
        exported = await database.export_shop_data(self.shop_a)
        self.assertIn("shop_settings", exported)
        self.assertIn("catalog_items", exported)
        await database.delete_shop_data(self.shop_a)
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in (
                "shop_settings", "knowledge_versions", "knowledge_drafts", "catalog_items",
                "ai_usage", "analytics_events", "billing_profiles",
            ):
                count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE shop_id = ?", (self.shop_a,)).fetchone()[0]
                self.assertEqual(count, 0, table)


class P1AMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_existing_shop_is_preserved_and_remains_enabled(self):
        with tempfile.TemporaryDirectory(prefix="customerserviceai-p1a-migrate-") as td:
            path = str(Path(td) / "legacy.db")
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("""CREATE TABLE shops (
                    shop_id TEXT PRIMARY KEY, admin_id INTEGER NOT NULL UNIQUE,
                    shop_name TEXT DEFAULT '', knowledge TEXT DEFAULT '',
                    client_bot_token TEXT DEFAULT '', client_bot_id INTEGER,
                    client_bot_fingerprint TEXT DEFAULT '', client_bot_username TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
                conn.execute("INSERT INTO shops (shop_id, admin_id, shop_name, knowledge) VALUES ('legacy1', 1, 'Legacy', 'Fact')")
                conn.commit()
            database.DB_PATH = path
            payments.DB_PATH = path
            await database.init_db()
            self.assertEqual((await database.get_shop_by_id("legacy1"))["knowledge"], "Fact")
            self.assertTrue((await database.get_shop_settings("legacy1"))["ai_enabled"])
            self.assertEqual((await database.list_knowledge_versions("legacy1"))[0]["version"], 1)


class P1AUXTest(unittest.TestCase):
    def test_owner_preferences_only_offer_tone_controls(self):
        callbacks = {
            button.callback_data
            for row in manager_bot.preferences_keyboard().inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertEqual(
            callbacks,
            {"pref_tone_friendly", "pref_tone_professional", "pref_tone_concise", "back_menu"},
        )


if __name__ == "__main__":
    unittest.main()
