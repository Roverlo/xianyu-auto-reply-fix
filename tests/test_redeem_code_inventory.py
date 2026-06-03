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


if __name__ == "__main__":
    unittest.main()
