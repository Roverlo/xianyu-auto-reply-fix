import unittest

from utils.qr_login import QRLoginManager


class QRLoginCookieTests(unittest.TestCase):
    def test_browser_cookies_use_playwright_url_shape(self):
        manager = QRLoginManager()

        cookies = manager._build_browser_cookies(
            "https://passport.goofish.com/iv/remote/pc/mini_login_check.htm",
            {"foo": "bar"},
        )

        self.assertEqual(cookies[0]["url"], "https://passport.goofish.com")
        self.assertNotIn("path", cookies[0])


if __name__ == "__main__":
    unittest.main()
