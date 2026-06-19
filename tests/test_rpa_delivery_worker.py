import sys
import types
import unittest
from datetime import datetime, timedelta
from datetime import UTC

if "loguru" not in sys.modules:
    loguru = types.ModuleType("loguru")

    class _Logger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    loguru.logger = _Logger()
    sys.modules["loguru"] = loguru

if "db_manager" not in sys.modules:
    db_manager = types.ModuleType("db_manager")
    db_manager.db_manager = object()
    sys.modules["db_manager"] = db_manager

from utils.rpa_delivery_worker import (
    RpaDeliveryConfig,
    is_order_too_old,
    is_supported_text_delivery_steps,
    normalize_chat_id,
    parse_quantity,
)


class RpaDeliveryWorkerLogicTest(unittest.TestCase):
    def test_config_defaults_are_safe(self):
        config = RpaDeliveryConfig.from_mapping({})

        self.assertFalse(config.enabled)
        self.assertTrue(config.only_when_ws_unready)
        self.assertTrue(config.require_buyer_nick)
        self.assertTrue(config.open_browser_on_start)
        self.assertEqual(config.profile_dir, "/app/browser_data/rpa_chrome")

    def test_config_bool_and_bounds(self):
        config = RpaDeliveryConfig.from_mapping({
            "enabled": "true",
            "interval_seconds": 1,
            "max_orders_per_cycle": 999,
            "headless": "yes",
        })

        self.assertTrue(config.enabled)
        self.assertEqual(config.interval_seconds, 30)
        self.assertEqual(config.max_orders_per_cycle, 20)
        self.assertTrue(config.headless)

    def test_text_only_delivery_step_detection(self):
        self.assertTrue(is_supported_text_delivery_steps([
            {"type": "text", "content": "hello"},
            {"type": "text", "content": "world"},
        ]))
        self.assertFalse(is_supported_text_delivery_steps([
            {"type": "image", "content": "__IMAGE_SEND__x"},
        ]))
        self.assertFalse(is_supported_text_delivery_steps([
            {"type": "text", "content": "   "},
        ]))

    def test_order_helpers(self):
        self.assertEqual(normalize_chat_id("12345@goofish"), "12345")
        self.assertEqual(parse_quantity("x2"), 2)
        self.assertEqual(parse_quantity(None), 1)

        recent = {"created_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")}
        old = {"created_at": (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")}

        self.assertFalse(is_order_too_old(recent, 1440))
        self.assertTrue(is_order_too_old(old, 1440))


if __name__ == "__main__":
    unittest.main()
