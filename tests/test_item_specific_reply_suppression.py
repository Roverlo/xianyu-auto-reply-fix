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


class ItemSpecificReplySuppressionTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = DBManager(self.db_path)
        self.cookie_id = "seller-cookie"
        self.item_id = "item-1001"
        self.chat_id = "62354106701"
        self.buyer_id = "2221518467141"

        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                INSERT INTO users (id, username, email, password_hash)
                VALUES (101, 'seller', 'seller@example.com', 'hash')
                """
            )
            cursor.execute(
                """
                INSERT INTO cookies (id, value, user_id)
                VALUES (?, 'cookie=value', 101)
                """,
                (self.cookie_id,),
            )
            cursor.execute(
                """
                INSERT INTO item_replay (cookie_id, item_id, reply_content)
                VALUES (?, ?, 'template')
                """,
                (self.cookie_id, self.item_id),
            )
            self.db.conn.commit()

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

    def test_does_not_suppress_fresh_presale_chat(self):
        result = self.db.should_suppress_item_specific_reply(
            self.cookie_id,
            "fresh-chat",
            self.item_id,
            "fresh-buyer",
        )

        self.assertIsNone(result)

    def test_suppresses_after_item_specific_reply_was_already_sent(self):
        self.db.save_chat_message(
            cookie_id=self.cookie_id,
            chat_id=self.chat_id,
            sender_id=self.cookie_id,
            sender_name=self.cookie_id,
            content="template",
            item_id=self.item_id,
            direction=1,
            reply_source="指定商品",
        )

        result = self.db.should_suppress_item_specific_reply(
            self.cookie_id,
            self.chat_id,
            self.item_id,
            self.buyer_id,
        )

        self.assertEqual(result["reason"], "already_replied")
        self.assertIn("message_id", result)

    def test_suppresses_when_order_exists_for_chat_item(self):
        self.db.insert_or_update_order(
            order_id="order-1001",
            item_id=self.item_id,
            buyer_id=self.buyer_id,
            sid=f"{self.chat_id}@goofish",
            order_status="completed",
            cookie_id=self.cookie_id,
        )

        result = self.db.should_suppress_item_specific_reply(
            self.cookie_id,
            self.chat_id,
            self.item_id,
            self.buyer_id,
        )

        self.assertEqual(result["reason"], "order_exists")
        self.assertEqual(result["order_id"], "order-1001")
        self.assertEqual(result["order_status"], "completed")


if __name__ == "__main__":
    unittest.main()
