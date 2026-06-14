import asyncio
import sys
import types
import unittest
from unittest import mock


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


_STUBBED_MODULE_NAMES = (
    "loguru",
    "websockets",
    "aiohttp",
    "config",
    "utils.xianyu_utils",
    "db_manager",
    "utils.notification_dispatcher",
    "XianyuAutoAsync",
)
_ORIGINAL_MODULES = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}


def _install_import_stubs():
    loguru = types.ModuleType("loguru")
    loguru.logger = _Logger()
    sys.modules["loguru"] = loguru

    websockets = types.ModuleType("websockets")
    websockets.__version__ = "test"
    sys.modules["websockets"] = websockets

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientTimeout = lambda *args, **kwargs: None
    sys.modules["aiohttp"] = aiohttp

    config = types.ModuleType("config")
    config.WEBSOCKET_URL = ""
    config.HEARTBEAT_INTERVAL = 30
    config.HEARTBEAT_TIMEOUT = 10
    config.TOKEN_REFRESH_INTERVAL = 72000
    config.TOKEN_RETRY_INTERVAL = 600
    config.SESSION_KEEPALIVE_INTERVAL = 600
    config.SESSION_KEEPALIVE_RETRY_INTERVAL = 180
    config.COOKIES_STR = ""
    config.LOG_CONFIG = {}
    config.AUTO_REPLY = {}
    config.DEFAULT_HEADERS = {}
    config.WEBSOCKET_HEADERS = {}
    config.APP_CONFIG = {}
    config.API_ENDPOINTS = {"token": "https://example.invalid/token"}
    config.YIFAN_API = {}
    config.RISK_CONTROL = {
        "pending_order_reconcile_enabled": True,
        "pending_order_reconcile_interval_seconds": 120,
        "pending_order_reconcile_boot_delay_seconds": 30,
        "pending_order_reconcile_max_orders": 20,
        "pending_order_reconcile_max_order_age_minutes": 1440,
        "pending_order_reconcile_notice_cooldown_seconds": 1800,
        "pending_order_reconcile_session_expired_backoff_seconds": 1800,
        "pending_order_reconcile_error_backoff_seconds": 300,
    }
    sys.modules["config"] = config

    xianyu_utils = types.ModuleType("utils.xianyu_utils")
    xianyu_utils.decrypt = lambda value: value
    xianyu_utils.generate_mid = lambda: "mid"
    xianyu_utils.generate_uuid = lambda: "uuid"
    xianyu_utils.trans_cookies = lambda text: dict(
        part.strip().split("=", 1)
        for part in str(text or "").split(";")
        if "=" in part
    )
    xianyu_utils.generate_device_id = lambda user_id: f"device-{user_id}"
    xianyu_utils.generate_sign = lambda *args, **kwargs: "sign"
    sys.modules["utils.xianyu_utils"] = xianyu_utils

    db_manager = types.ModuleType("db_manager")
    db_manager.db_manager = object()
    sys.modules["db_manager"] = db_manager

    notification_dispatcher = types.ModuleType("utils.notification_dispatcher")
    for name in (
        "build_face_verify_notification",
        "dispatch_account_notifications",
        "format_notification_template",
        "get_notification_template_text",
        "guess_verification_type",
        "render_notification_template",
    ):
        setattr(notification_dispatcher, name, lambda *args, **kwargs: None)
    sys.modules["utils.notification_dispatcher"] = notification_dispatcher


def _restore_import_stubs():
    for name, original in _ORIGINAL_MODULES.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


_install_import_stubs()

import XianyuAutoAsync as xianyu_module
from XianyuAutoAsync import ConnectionState, XianyuLive

_restore_import_stubs()


class _FakeWebSocket:
    closed = False

    async def send(self, _message):
        raise AssertionError("send should not be called directly by these tests")


class _FakeDbManager:
    def get_order_by_id(self, _order_id):
        return None


class _CookieManager:
    def get_cookie_status(self, _cookie_id):
        return True


class PendingOrderReconcileTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_db_manager = xianyu_module.db_manager
        xianyu_module.db_manager = _FakeDbManager()

        self.live = object.__new__(XianyuLive)
        self.live.cookie_id = "cookie-1"
        self.live.cookies_str = "unb=123; _m_h5_tk=token_1"
        self.live.ws = _FakeWebSocket()
        self.live.connection_state = ConnectionState.CONNECTED
        self.live.pending_order_reconcile_enabled = True
        self.live.pending_order_reconcile_lock = asyncio.Lock()
        self.live.pending_order_reconcile_max_order_age_minutes = 43200
        self.live.pending_order_reconcile_notice_cooldown = 300
        self.live.pending_order_reconcile_notice_times = {}
        self.live.pending_order_reconcile_session_expired_backoff = 1800
        self.live.pending_order_reconcile_error_backoff = 300
        self.live.pending_order_reconcile_backoff_until = 0
        self.live.pending_order_reconcile_last_error_kind = None
        self.live.last_token_refresh_status = None
        self.live.last_token_refresh_error_message = None
        self.live._safe_str = str
        self.live._set_runtime_cookie_state = lambda *args, **kwargs: False
        self.live._reload_latest_cookies_from_db = lambda *args, **kwargs: False
        self.live.can_auto_delivery = lambda _order_id: True
        self.live.is_lock_held = lambda _order_id: False

    def tearDown(self):
        xianyu_module.db_manager = self.original_db_manager

    def _pending_order(self, order_id="ORDER-1", raw_source=None):
        return {
            "order_id": order_id,
            "item_id": "ITEM-1",
            "buyer_id": "2200000000001",
            "buyer_nick": "buyer",
            "order_status": "pending_ship",
            "amount": "1.00",
            "platform_created_at": "2026-05-21 01:00:00",
            "platform_paid_at": "2026-05-21 01:00:01",
            "raw_source": raw_source or {},
        }

    async def test_reconcile_delivers_when_chat_id_is_available(self):
        order = self._pending_order(raw_source={"sid": "9876543210@goofish"})
        deliveries = []
        logs = []
        notices = []

        async def fetch_orders():
            return {"orders": [order]}, self.live.cookies_str

        async def fetch_detail(*_args, **_kwargs):
            return {"order_status": "pending_ship", "quantity": "1"}

        async def delivery(**kwargs):
            deliveries.append(kwargs)

        async def notify(order_arg, reason, chat_id=None):
            notices.append((order_arg, reason, chat_id))

        self.live._fetch_recent_history_orders_for_reconcile = fetch_orders
        self.live.fetch_order_detail_info = fetch_detail
        self.live._handle_simple_message_auto_delivery = delivery
        self.live._notify_pending_order_reconcile_issue = notify
        self.live._persist_history_order_candidate = lambda *args, **kwargs: True
        self.live._record_delivery_log = lambda **kwargs: logs.append(kwargs)

        stats = await self.live._reconcile_pending_orders_once()

        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(deliveries[0]["order_id"], "ORDER-1")
        self.assertEqual(deliveries[0]["chat_id"], "9876543210")
        self.assertEqual(notices, [])
        self.assertEqual(logs, [])

    async def test_reconcile_skips_and_notifies_when_chat_id_is_missing(self):
        order = self._pending_order(order_id="ORDER-2", raw_source={"rightVO": {"btnList": []}})
        deliveries = []
        logs = []
        notices = []

        async def fetch_orders():
            return {"orders": [order]}, self.live.cookies_str

        async def fetch_detail(*_args, **_kwargs):
            return {"order_status": "pending_ship", "quantity": "1"}

        async def delivery(**kwargs):
            deliveries.append(kwargs)

        async def notify(order_arg, reason, chat_id=None):
            notices.append((order_arg, reason, chat_id))

        self.live._fetch_recent_history_orders_for_reconcile = fetch_orders
        self.live.fetch_order_detail_info = fetch_detail
        self.live._handle_simple_message_auto_delivery = delivery
        self.live._notify_pending_order_reconcile_issue = notify
        self.live._persist_history_order_candidate = lambda *args, **kwargs: True
        self.live._record_delivery_log = lambda **kwargs: logs.append(kwargs)
        self.live._lookup_recent_chat_id_for_history_order = lambda _order: None

        stats = await self.live._reconcile_pending_orders_once()

        self.assertEqual(stats["delivered"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(deliveries, [])
        self.assertEqual(logs[0]["order_id"], "ORDER-2")
        self.assertEqual(logs[0]["status"], "skipped")
        self.assertEqual(notices[0][0]["order_id"], "ORDER-2")

    async def test_reconcile_session_expired_enters_backoff_without_delivery(self):
        class SessionExpiredError(RuntimeError):
            kind = "session_expired"

        fetch_count = 0
        deliveries = []

        async def fetch_orders():
            nonlocal fetch_count
            fetch_count += 1
            raise SessionExpiredError("FAIL_SYS_SESSION_EXPIRED::Session expired")

        async def delivery(**kwargs):
            deliveries.append(kwargs)

        self.live._fetch_recent_history_orders_for_reconcile = fetch_orders
        self.live._handle_simple_message_auto_delivery = delivery

        stats = await self.live._reconcile_pending_orders_once()

        self.assertEqual(stats, {"scanned": 0, "pending": 0, "delivered": 0, "skipped": 0})
        self.assertEqual(fetch_count, 1)
        self.assertEqual(deliveries, [])
        self.assertEqual(self.live.pending_order_reconcile_last_error_kind, "session_expired")
        self.assertGreater(self.live.pending_order_reconcile_backoff_until, 0)
        self.assertEqual(self.live.last_token_refresh_status, "history_session_expired")

        stats = await self.live._reconcile_pending_orders_once()

        self.assertEqual(stats, {"scanned": 0, "pending": 0, "delivered": 0, "skipped": 0})
        self.assertEqual(fetch_count, 1)


class MessageStreamWatchdogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.live = object.__new__(XianyuLive)
        self.live.cookie_id = "cookie-1"
        self.live.ws = _FakeWebSocket()
        self.live.heartbeat_timeout = 10
        self.live.heartbeat_interval = 30
        self.live.stream_watchdog_check_interval = 1
        self.live.stream_watchdog_grace_period = 1
        self.live.message_stream_watchdog_timeout = 100
        self.live.last_non_heartbeat_message_time = 0
        self.live.last_sync_package_time = 0
        self.live.last_user_chat_time = 0
        self.live.last_stream_watchdog_reconnect_time = 0
        self.live.last_stream_watchdog_probe_time = 0
        self.live.message_stream_watchdog_probe_cooldown = 60
        self.live.message_stream_watchdog_reconnect_on_keepalive_success = False
        self.live.last_heartbeat_response = 1000
        self.live._safe_str = str

    async def _run_one_watchdog_pass(self):
        cookie_manager = types.ModuleType("cookie_manager")
        cookie_manager.manager = _CookieManager()
        sleep_calls = 0

        async def interruptible_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError()

        self.live._interruptible_sleep = interruptible_sleep
        async def notify_stale(*_args, **_kwargs):
            return None

        self.live._maybe_notify_message_stream_stale = notify_stale

        with mock.patch.dict(sys.modules, {"cookie_manager": cookie_manager}), \
                mock.patch.object(xianyu_module.time, "time", return_value=1000):
            with self.assertRaises(asyncio.CancelledError):
                await self.live.message_stream_watchdog_loop()

    async def test_initial_silence_under_threshold_keeps_observing(self):
        reconnect_reasons = []

        async def force_reconnect(reason):
            reconnect_reasons.append(reason)
            return True

        self.live.last_successful_connection = 850
        self.live.message_stream_initial_silence_reconnect_timeout = 200
        self.live._force_websocket_reconnect = force_reconnect

        await self._run_one_watchdog_pass()

        self.assertEqual(reconnect_reasons, [])
        self.assertEqual(self.live.last_stream_watchdog_reconnect_time, 0)

    async def test_initial_silence_over_threshold_keeps_connection_when_keepalive_succeeds(self):
        reconnect_reasons = []

        async def force_reconnect(reason):
            reconnect_reasons.append(reason)
            return True

        async def keepalive_success():
            self.live.last_session_keepalive_status = "success"
            return True

        self.live.last_successful_connection = 700
        self.live.message_stream_initial_silence_reconnect_timeout = 200
        self.live._force_websocket_reconnect = force_reconnect
        self.live.keep_session_alive = keepalive_success

        await self._run_one_watchdog_pass()

        self.assertEqual(reconnect_reasons, [])
        self.assertEqual(self.live.last_stream_watchdog_probe_time, 1000)
        self.assertEqual(self.live.last_stream_watchdog_reconnect_time, 0)

    async def test_initial_silence_over_threshold_reconnects_when_keepalive_fails(self):
        reconnect_reasons = []

        async def force_reconnect(reason):
            reconnect_reasons.append(reason)
            return True

        async def keepalive_failed():
            self.live.last_session_keepalive_status = "network_failed"
            return False

        self.live.last_successful_connection = 700
        self.live.message_stream_initial_silence_reconnect_timeout = 200
        self.live._force_websocket_reconnect = force_reconnect
        self.live.keep_session_alive = keepalive_failed

        await self._run_one_watchdog_pass()

        self.assertEqual(
            reconnect_reasons,
            ["业务消息流长时间只有心跳且轻量保活未确认安全(status=network_failed)"],
        )
        self.assertEqual(self.live.last_stream_watchdog_reconnect_time, 1000)


if __name__ == "__main__":
    unittest.main()
