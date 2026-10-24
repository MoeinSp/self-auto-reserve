"""End-to-end test of the basket bot against tests/fake_site.py.

Run: python -m unittest tests.test_flow
"""
import asyncio
import os
import socket
import tempfile
import threading
import time
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from app import config  # noqa: E402
from app.cullinan import CullinanClient, Job, LoginFailed, launch_browser  # noqa: E402
from app.jalali import normalize_date, to_jalali  # noqa: E402
from app.ratelimit import Throttle  # noqa: E402
from app.text import matches  # noqa: E402
from tests import fake_site  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeSiteTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        port = _free_port()
        cls.server = uvicorn.Server(uvicorn.Config(fake_site.app, port=port, log_level="warning"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        while not cls.server.started:
            time.sleep(0.05)
        config.SITE_URL = f"http://127.0.0.1:{port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    async def asyncSetUp(self):
        fake_site.STOCK.clear()
        fake_site.PURCHASES.clear()
        self.pw = await async_playwright().start()
        self.browser = await launch_browser(self.pw)
        # Tiny intervals for tests only (the floors are bypassed on purpose).
        self.throttle = Throttle()
        self.throttle.min_interval = self.throttle.step_interval = 0.01

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    def client(self, password=fake_site.PASSWORD, codes=("0000", fake_site.CAPTCHA)):
        answers = iter(codes)

        async def solve(png, attempt):
            return next(answers, fake_site.CAPTCHA)

        return CullinanClient(self.browser, fake_site.USER, password, self.throttle, solve)

    async def test_buys_highest_priority_food(self):
        fake_site.STOCK["1405/08/05"] = {"ناهار": {"خوراک مرغ": 2, "جوجه كباب": 1}}
        c = self.client()
        job = Job(1, "1405/08/05", "ناهار", ["چلو کباب", "جوجه کباب", "*"])
        result = await c.attempt(job)
        await c.close()
        self.assertEqual(result.status, "bought", result.message)
        self.assertEqual(fake_site.PURCHASES, [("1405/08/05", "ناهار", "جوجه كباب", 1)])

    async def test_waits_when_nothing_wanted(self):
        fake_site.STOCK["1405/08/05"] = {"ناهار": {"خوراک مرغ": 2}}
        c = self.client()
        result = await c.attempt(Job(1, "1405/08/05", "ناهار", ["جوجه کباب"]))
        self.assertEqual(result.status, "no_match")
        self.assertIn("خوراک مرغ (2)", result.seen)
        result = await c.attempt(Job(1, "1405/08/05", "شام", ["*"]))
        self.assertEqual(result.status, "empty")
        await c.close()
        self.assertEqual(fake_site.PURCHASES, [])

    async def test_reads_menu_per_meal(self):
        fake_site.STOCK["1405/08/05"] = {"ناهار": {"جوجه کباب": 2, "خوراک مرغ": 1}, "شام": {"عدس پلو": 3}}
        c = self.client()
        menu = await c.read_menu("1405/08/05")
        await c.close()
        self.assertEqual(menu, {"صبحانه": [], "ناهار": ["جوجه کباب", "خوراک مرغ"], "شام": ["عدس پلو"]})
        self.assertEqual(fake_site.PURCHASES, [])

    async def test_check_basket(self):
        c = self.client()
        await c.check_basket()
        await c.close()

    async def test_wrong_password_stops(self):
        c = self.client(password="bad")
        with self.assertRaises(LoginFailed):
            await c.login()
        await c.close()


class UnitTest(unittest.TestCase):
    def test_jalali(self):
        import datetime as dt

        self.assertEqual(to_jalali(dt.date(2026, 10, 24)), (1405, 8, 2))
        self.assertEqual(to_jalali(dt.date(2025, 3, 21)), (1404, 1, 1))
        self.assertEqual(normalize_date("۱۴۰۵/۸/۵"), "1405/08/05")

    def test_matches(self):
        self.assertTrue(matches("جوجه كباب (3)", "جوجه کباب"))
        self.assertFalse(matches("کباب کوبیده", "جوجه کباب"))

    def test_throttle_spacing_and_cap(self):
        from app.ratelimit import RateLimited

        async def run():
            t = Throttle(per_hour=3)
            t.min_interval, t.jitter = 0.2, 0
            start = time.monotonic()
            await t.wait()
            await t.wait()
            self.assertGreaterEqual(time.monotonic() - start, 0.2)
            await t.wait()
            with self.assertRaises(RateLimited):
                await t.wait()

        asyncio.run(run())

    def test_floor(self):
        self.assertGreaterEqual(Throttle(min_interval=0.1).min_interval, config.MIN_POLL_FLOOR)


if __name__ == "__main__":
    unittest.main()
