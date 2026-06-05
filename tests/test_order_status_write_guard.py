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


class OrderStatusWriteGuardTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = DBManager(self.db_path)
        self.cookie_id = "seller-cookie"

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

    def test_prevents_order_detail_from_reverting_shipped_to_pending_ship(self):
        self.db.insert_or_update_order(
            order_id="order-1001",
            cookie_id=self.cookie_id,
            order_status="shipped",
        )

        self.db.insert_or_update_order(
            order_id="order-1001",
            cookie_id=self.cookie_id,
            order_status="pending_ship",
        )

        order = self.db.get_order_by_id("order-1001")
        self.assertEqual(order["order_status"], "shipped")

    def test_allows_refunding_to_override_shipped(self):
        self.db.insert_or_update_order(
            order_id="order-1002",
            cookie_id=self.cookie_id,
            order_status="shipped",
        )

        self.db.insert_or_update_order(
            order_id="order-1002",
            cookie_id=self.cookie_id,
            order_status="refunding",
        )

        order = self.db.get_order_by_id("order-1002")
        self.assertEqual(order["order_status"], "refunding")


if __name__ == "__main__":
    unittest.main()
