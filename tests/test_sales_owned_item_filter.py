import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from db_manager import DBManager

import reply_server


class SalesOwnedItemFilterTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "sales_owned_item_filter.db"
        self.db = DBManager(str(self.db_path))

        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                INSERT INTO users (id, username, email, password_hash)
                VALUES (101, 'sales-test-user', 'sales-test@example.com', 'hash')
                """
            )
            cursor.execute(
                """
                INSERT INTO cookies (id, value, user_id)
                VALUES ('cookie-owned-filter', 'cookie=value', 101)
                """
            )
            cursor.execute(
                """
                INSERT INTO item_info (cookie_id, item_id, item_title)
                VALUES ('cookie-owned-filter', 'owned-item', 'Owned item')
                """
            )
            cursor.execute(
                """
                INSERT INTO orders (
                    order_id, item_id, amount, order_status, cookie_id,
                    platform_created_at, platform_paid_at
                )
                VALUES
                    ('owned-order', 'owned-item', '17.60', 'completed', 'cookie-owned-filter',
                     '2026-05-30 01:00:00', '2026-05-30 01:01:00'),
                    ('foreign-order', 'foreign-item', '76.00', 'completed', 'cookie-owned-filter',
                     '2026-05-30 02:00:00', '2026-05-30 02:01:00')
                """
            )
            self.db.conn.commit()

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_sales_data_excludes_orders_whose_item_is_not_owned_by_cookie(self):
        user_info = {"user_id": 101, "username": "sales-test-user"}

        with mock.patch("db_manager.db_manager", self.db):
            response = asyncio.run(
                reply_server.get_sales_data(
                    start_date="2026-05-30",
                    end_date="2026-05-31",
                    user_info=user_info,
                )
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["total"], 17.6)
        self.assertEqual(response["data"]["count"], 1)

    def test_orders_endpoint_marks_non_owned_items_for_dashboard_totals(self):
        user_info = {"user_id": 101, "username": "sales-test-user"}

        with mock.patch.object(reply_server, "db_manager", self.db), \
             mock.patch("db_manager.db_manager", self.db), \
             mock.patch.object(reply_server, "log_with_user"):
            response = reply_server.get_user_orders(user_info)

        orders_by_id = {order["order_id"]: order for order in response["data"]}
        self.assertTrue(orders_by_id["owned-order"]["is_owned_item"])
        self.assertFalse(orders_by_id["foreign-order"]["is_owned_item"])


if __name__ == "__main__":
    unittest.main()
