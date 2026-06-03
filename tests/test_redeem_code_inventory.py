import os
import sys
import tempfile
import types
import unittest

if "aiohttp" not in sys.modules:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = lambda *args, **kwargs: None
    sys.modules["aiohttp"] = aiohttp
if "PIL" not in sys.modules:
    pil = types.ModuleType("PIL")
    sys.modules["PIL"] = pil
    for name in ("Image", "ImageDraw", "ImageFont"):
        module = types.ModuleType(f"PIL.{name}")
        setattr(pil, name, module)
        sys.modules[f"PIL.{name}"] = module
if "cryptography" not in sys.modules:
    crypto = types.ModuleType("cryptography")
    fernet_module = types.ModuleType("cryptography.fernet")

    class InvalidToken(Exception):
        pass

    class Fernet:
        @staticmethod
        def generate_key():
            return b"test-fernet-key"

        def __init__(self, key):
            self.key = key

        def encrypt(self, value):
            return b"token:" + value

        def decrypt(self, value):
            if not value.startswith(b"token:"):
                raise InvalidToken()
            return value[len(b"token:"):]

    fernet_module.Fernet = Fernet
    fernet_module.InvalidToken = InvalidToken
    crypto.fernet = fernet_module
    sys.modules["cryptography"] = crypto
    sys.modules["cryptography.fernet"] = fernet_module
if "loguru" not in sys.modules:
    loguru = types.ModuleType("loguru")

    class _Logger:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    loguru.logger = _Logger()
    sys.modules["loguru"] = loguru

from db_manager import DBManager


class RedeemCodeInventoryTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = DBManager(self.db_path)
        self.user_id = self.db.create_user("tester", "tester@example.com", "password")
        self.card_id = self.db.create_card(
            name="member-code",
            card_type="data",
            data_content="",
            is_multi_spec=True,
            spec_name="plan",
            spec_value="month",
            user_id=self.user_id,
        )
        self.rule_id = self.db.create_delivery_rule(
            keyword="member",
            card_id=self.card_id,
            user_id=self.user_id,
        )
        self.rule = self.db.get_delivery_rule_by_id(self.rule_id, self.user_id)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        key_path = os.path.join(os.path.dirname(self.db_path), ".secret_encryption.key")
        if os.path.exists(key_path):
            try:
                os.remove(key_path)
            except OSError:
                pass

    def test_import_deduplicates_and_reserve_lifecycle(self):
        batch_id = self.db.create_redeem_code_batch(
            name="month-batch",
            keyword="member",
            user_id=self.user_id,
            card_id=self.card_id,
            rule_id=self.rule_id,
            spec_name="plan",
            spec_value="month",
        )

        result = self.db.import_redeem_codes(batch_id, self.user_id, "A001\nA002\nA001\n")
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["duplicate_in_upload"], 1)

        stock = self.db.has_sufficient_redeem_codes_for_rule(self.rule, quantity=2, user_id=self.user_id)
        self.assertTrue(stock["uses_redeem_codes"])
        self.assertTrue(stock["ok"])
        self.assertEqual(stock["available_count"], 2)

        first = self.db.reserve_redeem_code_for_rule(
            self.rule,
            order_id="order-1",
            unit_index=1,
            user_id=self.user_id,
            buyer_id="buyer-1",
        )
        second = self.db.reserve_redeem_code_for_rule(
            self.rule,
            order_id="order-1",
            unit_index=2,
            user_id=self.user_id,
            buyer_id="buyer-1",
        )
        self.assertEqual(first["reserved_content"], "A001")
        self.assertEqual(second["reserved_content"], "A002")

        reused = self.db.reserve_redeem_code_for_rule(
            self.rule,
            order_id="order-1",
            unit_index=1,
            user_id=self.user_id,
            buyer_id="buyer-1",
        )
        self.assertEqual(reused["id"], first["id"])
        self.assertEqual(reused["reserved_content"], "A001")

        self.assertTrue(self.db.mark_redeem_code_sent(first["id"]))
        finalize = self.db.finalize_redeem_code(first["id"])
        self.assertTrue(finalize["success"])

        self.assertFalse(self.db.release_redeem_code(first["id"], error="should not release sent code"))
        self.assertTrue(self.db.release_redeem_code(second["id"], error="send failed"))

        records = self.db.get_redeem_codes(self.user_id, batch_id=batch_id, limit=10)
        statuses = sorted(record["status"] for record in records)
        self.assertEqual(statuses, ["available", "consumed"])
        released = self.db.get_redeem_codes(self.user_id, batch_id=batch_id, status="released", limit=10)
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["status"], "available")
        self.assertTrue(released[0]["released_at"])

    def test_batch_reservation_is_all_or_none_when_stock_is_short(self):
        batch_id = self.db.create_redeem_code_batch(
            name="short-batch",
            keyword="member",
            user_id=self.user_id,
            card_id=self.card_id,
            rule_id=self.rule_id,
            spec_name="plan",
            spec_value="month",
        )
        self.db.import_redeem_codes(batch_id, self.user_id, "ONLY-ONE\n")

        result = self.db.reserve_redeem_codes_for_rule(
            self.rule,
            order_id="order-2",
            unit_indexes=[1, 2],
            user_id=self.user_id,
            buyer_id="buyer-2",
        )

        self.assertTrue(result["uses_redeem_codes"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reservations"], [])

        records = self.db.get_redeem_codes(self.user_id, batch_id=batch_id, limit=10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "available")

    def test_data_card_requires_bound_redeem_pool(self):
        unbound_card_id = self.db.create_card(
            name="unbound-code",
            card_type="data",
            data_content="LEGACY-1\n",
            is_multi_spec=True,
            spec_name="plan",
            spec_value="year",
            user_id=self.user_id,
        )
        unbound_rule_id = self.db.create_delivery_rule(
            keyword="unbound",
            card_id=unbound_card_id,
            user_id=self.user_id,
        )
        unbound_rule = self.db.get_delivery_rule_by_id(unbound_rule_id, self.user_id)

        stock = self.db.has_sufficient_redeem_codes_for_rule(unbound_rule, quantity=1, user_id=self.user_id)
        self.assertTrue(stock["uses_redeem_codes"])
        self.assertFalse(stock["ok"])
        self.assertEqual(stock["error"], "卡券未绑定兑换码池")

        with self.assertRaises(ValueError):
            self.db.reserve_redeem_code_for_rule(
                unbound_rule,
                order_id="order-unbound",
                unit_index=1,
                user_id=self.user_id,
            )

    def test_card_can_bind_existing_redeem_pool(self):
        batch_id = self.db.create_redeem_code_batch(
            name="pool-a",
            keyword="pool-a",
            user_id=self.user_id,
        )
        self.db.import_redeem_codes(batch_id, self.user_id, "POOL-A-1\nPOOL-A-2\n")

        result = self.db.bind_redeem_code_batch_to_card(
            batch_id=batch_id,
            card_id=self.card_id,
            user_id=self.user_id,
        )
        self.assertEqual(result["batch_id"], batch_id)
        self.assertEqual(result["card_id"], self.card_id)
        self.assertEqual(result["rule_id"], self.rule_id)

        card = self.db.get_card_by_id(self.card_id, self.user_id)
        self.assertTrue(card["redeem_inventory"]["uses_redeem_codes"])
        self.assertEqual(card["redeem_inventory"]["batch_count"], 1)
        self.assertEqual(card["redeem_inventory"]["available_count"], 2)
        self.assertEqual(card["redeem_inventory"]["primary_batch"]["id"], batch_id)

        rule = self.db.get_delivery_rule_by_id(self.rule_id, self.user_id)
        self.assertEqual(rule["redeem_inventory"]["primary_batch"]["id"], batch_id)

        reserved = self.db.reserve_redeem_code_for_rule(
            rule,
            order_id="order-bound",
            unit_index=1,
            user_id=self.user_id,
            buyer_id="buyer-bound",
        )
        self.assertEqual(reserved["reserved_content"], "POOL-A-1")

    def test_create_redeem_delivery_config_builds_all_links(self):
        result = self.db.create_redeem_code_delivery_config(
            user_id=self.user_id,
            keyword="pro member",
            config_name="pro-month",
            spec_name="plan",
            spec_value="pro",
            warning_threshold=2,
            codes="P001\nP002\nP001\n",
        )

        card = self.db.get_card_by_id(result["card_id"], self.user_id)
        self.assertEqual(card["type"], "data")
        self.assertTrue(card["is_multi_spec"])
        self.assertEqual(card["spec_name"], "plan")
        self.assertEqual(card["spec_value"], "pro")
        self.assertIn("{DELIVERY_CONTENT}", card["description"])

        rule = self.db.get_delivery_rule_by_id(result["rule_id"], self.user_id)
        self.assertEqual(rule["keyword"], "pro member")
        self.assertEqual(rule["card_id"], result["card_id"])

        batch = self.db.get_redeem_code_batch_by_id(result["batch_id"], self.user_id)
        self.assertEqual(batch["card_id"], result["card_id"])
        self.assertEqual(batch["rule_id"], result["rule_id"])
        self.assertEqual(batch["warning_threshold"], 2)

        self.assertEqual(result["import_result"]["inserted"], 2)
        self.assertEqual(result["import_result"]["duplicate_in_upload"], 1)

        matched_rule = self.db.get_delivery_rule_by_id(result["rule_id"], self.user_id)
        reserved = self.db.reserve_redeem_code_for_rule(
            matched_rule,
            order_id="order-config-1",
            unit_index=1,
            user_id=self.user_id,
            buyer_id="buyer-1",
        )
        self.assertEqual(reserved["reserved_content"], "P001")

    def test_redeem_delivery_items_include_order_specs(self):
        cookie_id = "cookie-spec"
        self.assertTrue(self.db.save_cookie(cookie_id, "foo=bar", user_id=self.user_id))
        self.assertTrue(self.db.save_item_basic_info(
            cookie_id=cookie_id,
            item_id="item-1",
            item_title="MobaXterm",
            item_price="19.9",
            item_detail="MobaXterm 商品说明",
        ))
        self.db.insert_or_update_order(
            order_id="order-spec-1",
            item_id="item-1",
            buyer_id="buyer-1",
            cookie_id=cookie_id,
            spec_name="软件",
            spec_value="26.3汉化专业版",
            quantity="1",
        )

        items = self.db.get_redeem_code_delivery_items(self.user_id)
        item = next((entry for entry in items if entry["item_id"] == "item-1"), None)
        self.assertIsNotNone(item)
        self.assertEqual(item["keyword_suggestion"], "MobaXterm")
        self.assertEqual(item["spec_count"], 1)
        self.assertEqual(item["specs"][0]["spec_name"], "软件")
        self.assertEqual(item["specs"][0]["spec_value"], "26.3汉化专业版")
        self.assertEqual(item["specs"][0]["source"], "orders")


if __name__ == "__main__":
    unittest.main()
