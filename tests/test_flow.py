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
        fake_site.MENU.clear()
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
        self.assertIn("ناهار: خوراک مرغ", result.seen)
        result = await c.attempt(Job(1, "1405/08/05", "شام", ["*"]))
        self.assertEqual(result.status, "empty")
        await c.close()
        self.assertEqual(fake_site.PURCHASES, [])

    async def test_buys_without_meal_from_any_open_row(self):
        fake_site.STOCK["1405/08/05"] = {"شام": {"عدس پلو": 1}}
        c = self.client()
        result = await c.attempt(Job(1, "1405/08/05", "", ["عدس پلو"]))
        await c.close()
        self.assertEqual(result.status, "bought", result.message)
        self.assertEqual(fake_site.PURCHASES, [("1405/08/05", "شام", "عدس پلو", 1)])

    async def test_reads_profile_name_and_credit(self):
        c = self.client()
        await c.login()
        profile = await c.read_profile()
        await c.close()
        self.assertEqual(profile.get("name"), "معین فیروزآبادی")
        self.assertIn("3,345,000", profile.get("credit", ""))

    async def test_reads_menu_and_reservations_from_week_page(self):
        fake_site.MENU["1405/08/05"] = {
            "ناهار": {"foods": ["جوجه کباب", "خوراک مرغ"], "reserved": "خوراک مرغ"},
            "شام": {"foods": ["عدس پلو", "کوکو"], "locked": True},
        }
        c = self.client()
        menu = await c.read_menu("1405/08/05")
        await c.close()
        self.assertEqual(menu, {
            "صبحانه": {"foods": [], "reserved": "", "locked": True},
            "ناهار": {"foods": ["جوجه کباب", "خوراک مرغ"], "reserved": "خوراک مرغ", "locked": False},
            # Past days are locked but still list their foods.
            "شام": {"foods": ["عدس پلو", "کوکو"], "reserved": "", "locked": True},
        })
        self.assertEqual(fake_site.PURCHASES, [])

    async def test_reads_next_week_for_one_meal(self):
        fake_site.MENU["1405/08/12"] = {"شام": {"foods": ["ماکارونی"]}, "ناهار": {"foods": ["کباب"]}}
        c = self.client()
        menu = await c.read_menu("1405/08/12", "شام")
        await c.close()
        self.assertEqual(menu, {"شام": {"foods": ["ماکارونی"], "reserved": "", "locked": False}})

    async def test_job_skips_reserved_meal(self):
        from app.worker import Worker

        fake_site.MENU["1405/08/05"] = {"ناهار": {"foods": ["جوجه کباب"], "reserved": "جوجه کباب"}}
        c = self.client()
        row = {"id": 9, "skip_if_reserved": 1, "meal": "ناهار", "date": "1405/08/05"}
        self.assertEqual(await Worker()._reserved_food(c, row), "جوجه کباب")
        self.assertEqual(await Worker()._reserved_food(c, {**row, "skip_if_reserved": 0}), "")
        await c.close()

    async def test_check_basket_is_quick(self):
        # Real throttle settings: an on-demand test must not wait the polling interval.
        self.throttle = Throttle()
        c = self.client()
        c.interactive = True
        started = time.monotonic()
        await c.check_basket()
        await c.close()
        self.assertLess(time.monotonic() - started, 15)

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

    def test_iran_time(self):
        import datetime as dt

        from app.jalali import format_ts

        # 2026-10-24 11:10:58 UTC is 14:40:58 on 2 Aban in Tehran.
        ts = dt.datetime(2026, 10, 24, 11, 10, 58, tzinfo=dt.timezone.utc).timestamp()
        self.assertTrue(format_ts(ts).startswith("2 آبان"))
        self.assertTrue(format_ts(ts).endswith("14:40:58"))
        # 22:00 UTC is already the next Jalali day in Iran.
        late = dt.datetime(2026, 10, 24, 22, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertTrue(format_ts(late).startswith("3 آبان"))

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

    def test_lane_has_own_spacing_but_shared_cap(self):
        from app.ratelimit import RateLimited

        async def run():
            t = Throttle(per_hour=3)
            t.min_interval, t.jitter = 30, 0
            lane = t.lane()
            await t.wait()
            start = time.monotonic()
            await lane.wait(step=True)  # does not queue behind t's 30 s interval
            self.assertLess(time.monotonic() - start, 3)
            await lane.wait(step=True)
            with self.assertRaises(RateLimited):  # 3 requests in total hit the shared cap
                await t.wait()

        asyncio.run(run())

    def test_db_migration_and_attempts(self):
        import sqlite3

        from app import db

        conn = sqlite3.connect(config.DB_PATH)
        conn.executescript(
            "DROP TABLE IF EXISTS jobs; CREATE TABLE jobs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "date TEXT NOT NULL, meal TEXT NOT NULL DEFAULT '', foods TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 1, "
            "active_from TEXT NOT NULL DEFAULT '', active_to TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', "
            "message TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);"
            "INSERT INTO jobs (user_id, date, foods, created_at, updated_at) VALUES (1, '1405/08/05', '*', 0, 0);"
        )
        conn.commit()
        conn.close()
        db.init()  # adds the new columns to the old table
        self.assertEqual(db.count_attempt(1, "x"), 1)
        self.assertEqual(db.count_attempt(1, "y"), 2)

    def test_multiple_accounts_migration_and_support(self):
        from starlette.requests import Request
        from app import web, db, config

        config.SECRET_KEY = "test-secret"
        db.init()
        with db.tx() as c:
            c.execute("DELETE FROM accounts")
            c.execute("DELETE FROM users")
            c.execute("INSERT INTO users (id, username, password_hash, created_at) VALUES (1, 'tester', 'hash', 0)")

        req = Request({"type": "http", "session": {"uid": 1}, "query_string": b""})
        # 1) Add first account
        res = web.save_account(req, site_username="40111111", site_password="pass1", display_name="کاربر اول")
        self.assertEqual(res.status_code, 303)

        # 2) Add second account for same user
        res = web.save_account(req, site_username="40222222", site_password="pass2", display_name="کاربر دوم")
        self.assertEqual(res.status_code, 303)

        with db.tx() as c:
            accs = c.execute("SELECT site_username, display_name FROM accounts WHERE user_id = 1 ORDER BY id").fetchall()
            self.assertEqual(len(accs), 2)
            self.assertEqual(accs[0]["site_username"], "40111111")
            self.assertEqual(accs[0]["display_name"], "کاربر اول")
            self.assertEqual(accs[1]["site_username"], "40222222")
            self.assertEqual(accs[1]["display_name"], "کاربر دوم")

    def test_background_endpoints_run_on_event_loop(self):
        import inspect
        from app import web

        self.assertTrue(inspect.iscoroutinefunction(web.test_account))
        self.assertTrue(inspect.iscoroutinefunction(web.fetch_menu))

    def test_floor(self):
        self.assertGreaterEqual(Throttle(min_interval=0.1).min_interval, config.MIN_POLL_FLOOR)

    def test_registration_invite_code_logic(self):
        from starlette.requests import Request
        from app import web, db, config

        orig_invite = config.INVITE_CODE
        try:
            config.SECRET_KEY = "test-secret"
            with db.tx() as c:
                c.execute("DELETE FROM users")
            # 1) First user registers without invite code
            config.INVITE_CODE = "SECRET123"
            req = Request({"type": "http", "session": {}})
            res = web.register(req, username="user1", password="password123", invite="")
            self.assertEqual(res.status_code, 303)
            self.assertIn("ثبت‌نام انجام شد", req.session.get("flash", ""))

            # 2) Second user with wrong invite code is rejected
            req = Request({"type": "http", "session": {}})
            res = web.register(req, username="user2", password="password123", invite="WRONG")
            self.assertEqual(req.session.get("flash"), "کد دعوت نادرست است")

            # 3) Second user with empty invite code when INVITE_CODE is unset succeeds
            config.INVITE_CODE = ""
            req = Request({"type": "http", "session": {}})
            res = web.register(req, username="user2", password="password123", invite="")
            self.assertEqual(res.status_code, 303)
            self.assertIn("ثبت‌نام انجام شد", req.session.get("flash", ""))
        finally:
            config.INVITE_CODE = orig_invite

    def test_admin_rate_limit_reset(self):
        from starlette.requests import Request
        from app import web, db, config
        from app.worker import worker

        config.SECRET_KEY = "test-secret"
        db.init()
        with db.tx() as c:
            c.execute("DELETE FROM users")
            c.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'admin_user', 'hash', 1, 0)")
            c.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'normal_user', 'hash', 0, 0)")
            c.execute("DELETE FROM accounts")
            c.execute("INSERT INTO accounts (id, user_id, site_username, site_password_enc, display_name) VALUES (10, 1, '401', 'enc', 'Acc 10')")

        # Fake some rate limit hits
        now = time.monotonic()
        t = worker.throttle(10)
        t._hits.extend([now - 10, now - 5, now])
        t._logins.extend([now])
        stats = worker.get_throttle_stats(10)
        self.assertEqual(stats["hits"], 3)
        self.assertEqual(stats["logins"], 1)

        # Admin resets rate limit
        req = Request({"type": "http", "session": {"uid": 1}, "query_string": b""})
        res = web.admin_reset_rate_limit(req, account_id=10)
        self.assertEqual(res.status_code, 303)
        stats = worker.get_throttle_stats(10)
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["logins"], 0)

        # Non-admin is rejected
        req_non_admin = Request({"type": "http", "session": {"uid": 2}, "query_string": b""})
        res_denied = web.admin_page(req_non_admin)
        self.assertEqual(res_denied.status_code, 303)


if __name__ == "__main__":
    unittest.main()
