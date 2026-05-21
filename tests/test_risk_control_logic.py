import sys
import types
import unittest


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


if __name__ == "__main__":
    unittest.main()
