import os
import tempfile
import unittest

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-owner-ux-key")

import database
import manager_bot


class OwnerUxTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_db_path = database.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory(prefix="customerserviceai-owner-ux-")
        database.DB_PATH = os.path.join(self.tempdir.name, "test.db")
        await database.init_db()
        self.shop_id = await database.create_shop(70001)

    async def asyncTearDown(self):
        database.DB_PATH = self._old_db_path
        self.tempdir.cleanup()

    async def test_preferences_show_examples_and_hide_removed_controls(self):
        text = await manager_bot.build_preferences_text(self.shop_id)
        keyboard = manager_bot.preferences_keyboard()
        callback_data = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("Приклад для питання", text)
        self.assertIn("Дружній", text)
        self.assertIn("Професійний", text)
        self.assertIn("Короткий", text)
        self.assertIn("автоматично відповідає мовою клієнта", text)
        self.assertFalse(any(value.startswith("pref_lang_") for value in callback_data))
        self.assertNotIn("pref_timezone", callback_data)
        self.assertNotIn("pref_hours", callback_data)

    async def test_knowledge_wizard_has_no_rejected_questions_or_technical_diff(self):
        questions = "\n".join(item["question"] for item in manager_bot.KNOWLEDGE_QUESTIONS)
        self.assertNotIn("Типові заперечення", questions)
        self.assertNotIn("Переваги бізнесу", questions)
        self.assertNotIn("менш ніж за 24 години", questions)

        existing = "Назва бізнесу: Стара назва\nВнутрішня примітка: не видаляти"
        answers = manager_bot._parse_knowledge_answers(existing)
        answers["business_name"] = "Нова назва"
        composed = manager_bot._compose_knowledge(existing, answers)
        self.assertIn("Назва бізнесу: Нова назва", composed)
        self.assertIn("Внутрішня примітка: не видаляти", composed)
        self.assertNotIn("--- v", composed)
        self.assertNotIn("+++ draft", composed)
        self.assertNotIn("@@", composed)

    async def test_catalog_wizard_saves_uah_and_renders_ukrainian_status(self):
        state = {
            "item_id": None,
            "values": {
                "name": "Букет із троянд",
                "price": "850",
                "availability": "in_stock",
                "description": "12 червоних троянд",
            },
        }
        item_id, _ = await manager_bot._save_catalog_state(self.shop_id, state)
        item = (await database.list_catalog_items(self.shop_id))[0]
        self.assertEqual(item["currency"], "UAH")
        self.assertEqual(item["item_id"], item_id)

        text, _ = await manager_bot.build_catalog_view(self.shop_id)
        self.assertIn("850 грн", text)
        self.assertIn("Є в наявності", text)
        self.assertNotIn("in_stock", text)
        self.assertNotIn("EUR", text)

    async def test_catalog_edit_updates_same_tenant_item(self):
        item_id = await database.upsert_catalog_item(
            self.shop_id, "Стара назва", price="100", currency="UAH", availability="unknown"
        )
        state = {
            "item_id": item_id,
            "values": {
                "name": "Нова назва",
                "price": "120",
                "availability": "preorder",
                "description": "Оновлений опис",
            },
        }
        saved_id, _ = await manager_bot._save_catalog_state(self.shop_id, state)
        items = await database.list_catalog_items(self.shop_id)
        self.assertEqual(saved_id, item_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Нова назва")

    async def test_readiness_uses_owner_facing_actions(self):
        shop = await database.get_shop_by_id(self.shop_id)
        text = await manager_bot.build_readiness_text(shop)
        self.assertIn("Відповіді AI перевірені", text)
        self.assertIn("AI відповідає клієнтам", text)
        self.assertNotIn("Контрольний тест", text)

    def test_nested_flows_return_to_their_parent(self):
        setup_keyboard = manager_bot._knowledge_question_keyboard({"index": 1, "answers": {}})
        catalog_keyboard = manager_bot._catalog_step_keyboard({"stage": "price", "values": {}})
        test_keyboard = manager_bot.owner_test_keyboard()

        setup_back = next(button for row in setup_keyboard.inline_keyboard for button in row if button.text == "⬅ Назад")
        catalog_back = next(button for row in catalog_keyboard.inline_keyboard for button in row if button.text == "⬅ Назад")
        test_back = next(button for row in test_keyboard.inline_keyboard for button in row if button.text == "⬅ Назад")
        self.assertEqual(setup_back.callback_data, "setup_back")
        self.assertEqual(catalog_back.callback_data, "catalog_back")
        self.assertEqual(test_back.callback_data, "owner_readiness")


if __name__ == "__main__":
    unittest.main()
