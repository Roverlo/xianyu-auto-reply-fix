import sys
import types
import unittest
import asyncio
from unittest.mock import patch


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

    sys.modules["websockets"] = types.ModuleType("websockets")

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
        "slider_failure_backoff_seconds": 1800,
        "slider_consecutive_failure_threshold": 2,
        "slider_consecutive_pause_seconds": 7200,
        "hard_risk_backoff_seconds": 7200,
        "message_stream_watchdog_timeout_seconds": 14400,
        "message_stream_initial_silence_reconnect_seconds": 14400,
        "message_stream_watchdog_probe_cooldown_seconds": 1800,
        "message_stream_watchdog_reconnect_on_keepalive_success": False,
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

from XianyuAutoAsync import XianyuLive

_restore_import_stubs()


class RiskControlLogicTest(unittest.TestCase):
    def setUp(self):
        self.live = object.__new__(XianyuLive)
        self.live.cookie_id = "test-cookie"
        self.live.hard_risk_backoff_seconds = 7200
        self.live.slider_failure_backoff_seconds = 1800
        self.live.server_overload_backoff_seconds = 600
        self.live.last_token_refresh_status = None
        self.live.last_password_login_backoff_log_time = 0.0
        XianyuLive._password_login_failure_backoff = {}
        XianyuLive._manual_refresh_state = {}

    def test_beijibao_response_is_server_overload_not_slider(self):
        response = {
            "ret": [
                "FAIL_SYS_USER_VALIDATE",
                "RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试",
            ],
            "data": {
                "url": (
                    "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
                    "_____tmd_____/punish?x5secdata=abc__bx__h5api.m.goofish.com:443/"
                    "h5/mtop.taobao.idlemessage.pc.login.token/1.0&x5step=2&action=captcha"
                )
            },
        }

        decision = self.live._classify_token_risk_response(response)

        self.assertEqual(decision["risk_category"], "server_overload_rgv587")
        self.assertFalse(decision["auto_slider_allowed"])
        self.assertFalse(self.live._need_captcha_verification(response))

    def test_validate_with_captcha_url_is_slider_candidate(self):
        response = {
            "ret": ["FAIL_SYS_USER_VALIDATE"],
            "data": {
                "url": (
                    "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
                    "_____tmd_____/punish?x5secdata=abc__bx__h5api.m.goofish.com:443/"
                    "h5/mtop.taobao.idlemessage.pc.login.token/1.0&x5step=2&action=captcha"
                )
            },
        }

        decision = self.live._classify_token_risk_response(response)

        self.assertEqual(decision["risk_category"], "slider_challenge")
        self.assertTrue(decision["auto_slider_allowed"])
        self.assertTrue(self.live._need_captcha_verification(response))

    def test_x5secdata_is_not_preserved_when_missing_from_new_snapshot(self):
        result = XianyuLive.protected_merge_cookie_dicts(
            {"unb": "1", "cookie2": "c", "x5secdata": "old-ticket"},
            {"unb": "1", "cookie2": "c"},
        )

        self.assertNotIn("x5secdata", result["merged_cookies_dict"])
        self.assertIn("x5secdata", result["removed_fields"])

    def test_x5secdata_target_detection(self):
        token_ticket = "abc__bx__h5api.m.goofish.com:443/h5/mtop.taobao.idlemessage.pc.login.token/1.0"
        recommend_ticket = "abc__bx__h5api.m.taobao.com:443/h5/mtop.relationrecommend.wirelessrecommend.recommend/2.0"

        self.assertTrue(XianyuLive._is_token_x5secdata(token_ticket))
        self.assertFalse(XianyuLive._is_token_x5secdata(recommend_ticket))

    def test_image_probe_failure_with_valid_web_session_does_not_recommend_relogin(self):
        result = {
            "valid": False,
            "confirm_api": None,
            "web_session_api": True,
            "image_api": False,
            "details": ["图片上传API: 返回登录页面"],
            "inconclusive": False,
            "relogin_recommended": True,
        }

        normalized = self.live._normalize_cookie_validation_result(result)

        self.assertTrue(normalized["valid"])
        self.assertIsNone(normalized["image_api"])
        self.assertTrue(normalized["inconclusive"])
        self.assertFalse(normalized["relogin_recommended"])

    def test_manual_refresh_handoff_bypasses_old_server_overload_backoff_once(self):
        with patch.object(XianyuLive, "_persist_login_backoff", lambda *_args, **_kwargs: None), \
                patch.object(XianyuLive, "_clear_persisted_login_backoff", lambda *_args, **_kwargs: None):
            XianyuLive.set_password_login_failure_backoff("test-cookie", "server_overload", 600)
            self.assertTrue(self.live._should_skip_token_refresh_for_login_backoff())

            XianyuLive.mark_manual_refresh_handoff("test-cookie", ttl=120)
            self.assertFalse(self.live._should_skip_token_refresh_for_login_backoff())
            self.assertIsNone(XianyuLive.get_password_login_failure_backoff("test-cookie"))

            XianyuLive.set_password_login_failure_backoff("test-cookie", "server_overload", 600)
            self.assertTrue(self.live._should_skip_token_refresh_for_login_backoff())

    def test_server_overload_escalates_to_circuit_breaker_and_manual_hint(self):
        with patch.object(XianyuLive, "_persist_login_backoff", lambda *_args, **_kwargs: None):
            XianyuLive.set_password_login_failure_backoff("test-cookie", "server_overload", 600)
            XianyuLive.set_password_login_failure_backoff("test-cookie", "server_overload", 600)
            XianyuLive.set_password_login_failure_backoff("test-cookie", "server_overload", 600)

        state = XianyuLive.get_password_login_failure_backoff("test-cookie")

        self.assertEqual(state["consecutive_count"], 3)
        self.assertGreaterEqual(state["seconds"], 7200)
        self.assertTrue(state["requires_manual_cookie_refresh"])
        self.assertIn("Cookie", state["manual_recovery_hint"])

    def test_risk_control_freeze_active_for_server_overload_backoff(self):
        self.live.risk_control_freeze_background_tasks = True
        now = __import__("time").time()
        XianyuLive._password_login_failure_backoff["test-cookie"] = {
            "until": now + 900,
            "reason": "server_overload",
            "seconds": 900,
            "base_seconds": 600,
            "consecutive_count": 1,
            "created_at": now,
        }

        self.assertTrue(self.live._is_risk_control_freeze_active(now))
        self.assertEqual(self.live._get_risk_control_freeze_sleep_seconds(now), 900)

    def test_auth_recovery_wait_state_does_not_consume_manual_handoff_bypass(self):
        now = __import__("time").time()
        XianyuLive._password_login_failure_backoff["test-cookie"] = {
            "until": now + 900,
            "reason": "server_overload",
            "seconds": 900,
            "base_seconds": 600,
            "consecutive_count": 1,
            "created_at": now,
        }
        XianyuLive.mark_manual_refresh_handoff("test-cookie", ttl=120)

        wait_state = self.live._get_auth_recovery_wait_state(now)

        self.assertEqual(wait_state["reason"], "server_overload")
        self.assertFalse(self.live._should_skip_token_refresh_for_login_backoff(now))
        self.assertIsNone(XianyuLive.get_password_login_failure_backoff("test-cookie"))

    def test_auth_recovery_wait_state_derives_manual_hint_for_legacy_server_overload(self):
        now = __import__("time").time()
        XianyuLive._password_login_failure_backoff["test-cookie"] = {
            "until": now + 900,
            "reason": "server_overload",
            "seconds": 1800,
            "base_seconds": 600,
            "consecutive_count": 6,
            "created_at": now - 120,
        }

        wait_state = self.live._get_auth_recovery_wait_state(now)

        self.assertTrue(wait_state["requires_manual_cookie_refresh"])
        self.assertIn("RGV587", wait_state["manual_recovery_hint"])

    def test_external_auth_recovery_owner_detection(self):
        self.assertTrue(XianyuLive.is_external_auth_recovery_owner("manual_cookie_import:abc"))
        self.assertTrue(XianyuLive.is_external_auth_recovery_owner("password_login:abc"))
        self.assertFalse(XianyuLive.is_external_auth_recovery_owner("auto_cookie_refresh:abc"))

    def test_create_chat_response_extracts_cid(self):
        response = {
            "body": {
                "singleChatConversation": {
                    "cid": "62216320925@goofish",
                }
            }
        }

        self.assertEqual(
            self.live._extract_chat_id_from_create_chat_response(response),
            "62216320925",
        )

    def test_stream_initial_silence_threshold_defaults_to_idle_timeout_or_longer(self):
        self.live.heartbeat_interval = 15
        self.live.session_keepalive_interval = 600
        self.live.stream_watchdog_grace_period = 120
        self.live.message_stream_watchdog_timeout = 14400
        self.live.message_stream_initial_silence_reconnect_timeout = 14400

        self.assertGreaterEqual(
            self.live.message_stream_initial_silence_reconnect_timeout,
            self.live.message_stream_watchdog_timeout,
        )

    def test_stream_watchdog_keepalive_success_does_not_force_reconnect(self):
        class _OpenWebSocket:
            closed = False

        async def _run():
            now = 10_000.0
            self.live.cookie_id = "test-cookie"
            self.live.ws = _OpenWebSocket()
            self.live.heartbeat_timeout = 30
            self.live.heartbeat_interval = 15
            self.live.stream_watchdog_check_interval = 15
            self.live.stream_watchdog_grace_period = 120
            self.live.message_stream_watchdog_timeout = 300
            self.live.message_stream_initial_silence_reconnect_timeout = 300
            self.live.message_stream_watchdog_probe_cooldown = 60
            self.live.message_stream_watchdog_reconnect_on_keepalive_success = False
            self.live.last_successful_connection = now - 400
            self.live.last_non_heartbeat_message_time = now - 400
            self.live.last_sync_package_time = now - 400
            self.live.last_user_chat_time = 0
            self.live.last_heartbeat_response = now - 5
            self.live.last_stream_watchdog_reconnect_time = 0
            self.live.last_stream_watchdog_probe_time = 0
            self.live.stream_watchdog_trigger_times = []
            self.live.message_stream_notification_window = 3600
            self.live.last_token_refresh_status = None

            sleeps = 0
            reconnects = 0

            async def fake_sleep(_seconds):
                nonlocal sleeps
                sleeps += 1
                if sleeps > 1:
                    raise asyncio.CancelledError()

            async def fake_keepalive():
                self.live.last_session_keepalive_status = "success"
                return True

            async def fake_reconnect(_reason):
                nonlocal reconnects
                reconnects += 1
                return True

            async def fake_notify(*_args, **_kwargs):
                return None

            self.live._interruptible_sleep = fake_sleep
            self.live.keep_session_alive = fake_keepalive
            self.live._force_websocket_reconnect = fake_reconnect
            self.live._maybe_notify_message_stream_stale = fake_notify
            self.live._safe_str = str

            cookie_manager_module = types.ModuleType("cookie_manager")
            cookie_manager_module.manager = types.SimpleNamespace(
                get_cookie_status=lambda _cookie_id: True,
            )
            time_module = XianyuLive.message_stream_watchdog_loop.__globals__["time"]
            with patch.dict(sys.modules, {"cookie_manager": cookie_manager_module}):
                with patch.object(time_module, "time", return_value=now):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.live.message_stream_watchdog_loop()

            self.assertEqual(reconnects, 0)
            self.assertEqual(self.live.last_stream_watchdog_probe_time, now)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
