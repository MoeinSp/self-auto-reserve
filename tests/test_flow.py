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

from pathlib import Path
from app import config, db  # noqa: E402
config.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
db.init()
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
        fake_site.WEEK_SAVES.clear()
        fake_site.state.pop("credit_days", None)
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
            "صبحانه": {"foods": [], "locked": True},
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
        row = {"id": 9, "account_id": 999, "meal": "ناهار", "date": "1405/08/05"}
        self.assertEqual(await Worker()._reserved_food(c, row), "جوجه کباب")
        await c.close()
        # A reservation saved for the account answers without opening the site.
        with db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, created_at) VALUES (1, 'u', 'x$y', 0)")
            conn.execute("INSERT OR IGNORE INTO accounts (id, user_id, site_username, site_password_enc) VALUES (998, 1, '9', 'e')")
        db.save_reservations(998, {"1405/08/06": {"شام": "عدسی", "ناهار": ""}})
        self.assertEqual(await Worker()._reserved_food(None, {**row, "account_id": 998, "id": 10, "meal": "شام", "date": "1405/08/06"}), "عدسی")

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

    async def test_sign_in_with_site_account_without_registering(self):
        from unittest import mock

        from starlette.requests import Request

        from app import captcha, web

        web.worker.browser = self.browser
        web.worker.login_throttles.clear()
        with mock.patch.object(captcha, "ocr", return_value=fake_site.CAPTCHA):
            req = Request({"type": "http", "session": {}})
            res = await web.login_with_site(req, site_username=fake_site.USER, site_password=fake_site.PASSWORD)
            uid = req.session["uid"]
            with db.tx() as c:
                user = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                acc = c.execute("SELECT * FROM accounts WHERE user_id = ?", (uid,)).fetchall()
            self.assertEqual(user["username"], fake_site.USER + "@self")
            self.assertFalse(web.security.verify_password("", user["password_hash"]))  # no panel password
            self.assertEqual([a["site_username"] for a in acc], [fake_site.USER])
            self.assertEqual(acc[0]["display_name"], "معین فیروزآبادی")
            self.assertEqual(web.security.decrypt(acc[0]["site_password_enc"]), fake_site.PASSWORD)
            self.assertIn("#reserves", res.headers["location"])
            # Signing in again reuses the same panel user and account.
            req2 = Request({"type": "http", "session": {}})
            await web.login_with_site(req2, site_username=fake_site.USER, site_password=fake_site.PASSWORD)
            self.assertEqual(req2.session["uid"], uid)
            with db.tx() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (uid,)).fetchone()[0], 1)
            # A wrong site password doesn't sign in.
            req3 = Request({"type": "http", "session": {}})
            await web.login_with_site(req3, site_username=fake_site.USER, site_password="wrong")
            self.assertNotIn("uid", req3.session)
            self.assertIn("اشتباه", req3.session["flash"])

    async def test_auto_reserve_opens_hidden_meal_tabs(self):
        # Only the lunch tab is open on the real page; dinner sits in a hidden tab.
        fake_site.MENU["1405/08/09"] = {"ناهار": {"foods": ["قیمه"], "reserved": "قیمه"}, "شام": {"foods": ["ماکارونی", "کتلت"]}}
        fake_site.MENU["1405/08/10"] = {"ناهار": {"foods": ["کباب"]}, "شام": {"foods": ["عدسی"], "locked": True}}
        c = self.client()
        results = await c.auto_reserve_week("1405/08/09", ["ناهار", "شام"])
        await c.close()
        self.assertEqual(fake_site.WEEK_SAVES, [("1405/08/10", "ناهار"), ("1405/08/09", "شام")], results)
        self.assertEqual(fake_site.MENU["1405/08/09"]["شام"]["reserved"], "ماکارونی")
        self.assertIn("★ 1405/08/09 (شام): ثبت شد («ماکارونی»)", results)

    async def test_auto_reserve_follows_priorities_and_milk(self):
        from app.worker import priorities

        fake_site.MENU["1405/08/09"] = {
            "صبحانه": {"foods": ["پنیر/خیار/نان/چای/قند", "کره/مربا/شیرطعم دار/نان", "شیر/حلواشکری/نان"]},
            "شام": {"foods": ["ماکارونی,ترشی", "فلافل,گوجه"]},
        }
        acc = {"priority_breakfast": "پنیر", "priority_lunch": "", "priority_dinner": "کباب\nفلافل", "milk_pref": "flavored"}
        c = self.client()
        results = await c.auto_reserve_week("1405/08/09", ["صبحانه", "شام"], priorities(acc))
        await c.close()
        self.assertEqual(fake_site.MENU["1405/08/09"]["صبحانه"]["reserved"], "کره/مربا/شیرطعم دار/نان", results)
        # The fake site books the shown option; its first option is what a real "select" would change.
        self.assertIn("★ 1405/08/09 (شام): ثبت شد («فلافل,گوجه»)", results)
        self.assertIn("★ 1405/08/09 (صبحانه): ثبت شد («کره/مربا/شیرطعم دار/نان»)", results)

    async def test_auto_reserve_reports_days_the_site_did_not_save(self):
        fake_site.MENU["1405/08/09"] = {"شام": {"foods": ["ماکارونی"]}}
        fake_site.MENU["1405/08/10"] = {"شام": {"foods": ["عدسی"]}}
        fake_site.state["credit_days"] = 1  # credit for one dinner only
        c = self.client()
        results = await c.auto_reserve_week("1405/08/09", ["شام"])
        await c.close()
        self.assertEqual(fake_site.WEEK_SAVES, [("1405/08/09", "شام")])
        self.assertIn("★ 1405/08/09 (شام): ثبت شد («ماکارونی»)", results)
        self.assertIn("✗ 1405/08/10 (شام): ثبت نشد («عدسی»)", results)

    async def test_refresh_reads_this_and_next_week_for_everyone(self):
        from unittest import mock

        from app import jalali
        from app.worker import Worker

        with db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, created_at) VALUES (1, 'u', 'x$y', 0)")
            conn.execute("INSERT OR IGNORE INTO accounts (id, user_id, site_username, site_password_enc) VALUES (1, 1, '1', 'e')")
        fake_site.MENU["1405/08/05"] = {"ناهار": {"foods": ["قیمه"], "reserved": "قیمه"}}
        fake_site.MENU["1405/08/12"] = {"شام": {"foods": ["ماکارونی", "کتلت"]}}
        w = Worker()
        w.browser = self.browser
        c = self.client()
        with mock.patch.object(w, "client_for", return_value=c), \
                mock.patch.object(jalali, "today_str", return_value="1405/08/05"), \
                mock.patch.object(jalali, "get_next_week_days", return_value=[{"date": "1405/08/09"}]):
            self.assertTrue(w.refresh_week_menu(1, 1))
            self.assertFalse(w.refresh_week_menu(1, 1))  # one at a time
            while "finished" not in w.ops[1] or c.context:
                await asyncio.sleep(0.05)
        self.assertEqual(w.ops[1]["state"], "ok", w.ops[1]["message"])
        self.assertFalse(w.menu_refreshing)
        menu = db.week_menu("1405/08/02")
        self.assertEqual(len(menu), 14)
        self.assertEqual(menu["1405/08/05"]["ناهار"], ["قیمه"])
        self.assertEqual(menu["1405/08/05"], {"صبحانه": [], "ناهار": ["قیمه"], "شام": []})  # empty meals kept
        # The same read stores what this account has reserved.
        self.assertEqual(db.reservations(1, "1405/08/02"), {"1405/08/05": {"ناهار": "قیمه"}})

        # A meal whose grid is missing from the page keeps the foods saved before.
        fake_site.HIDE_GRIDS.add("Lunch")
        try:
            with mock.patch.object(w, "client_for", return_value=self.client()),                     mock.patch.object(jalali, "today_str", return_value="1405/08/05"),                     mock.patch.object(jalali, "get_next_week_days", return_value=[]):
                self.assertTrue(w.refresh_week_menu(1, 1))
                while "finished" not in w.ops[1] or w.ops[1]["state"] == "running":
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.3)
        finally:
            fake_site.HIDE_GRIDS.clear()
        self.assertEqual(w.ops[1]["state"], "ok", w.ops[1]["message"])
        self.assertEqual(db.week_menu("1405/08/05")["1405/08/05"]["ناهار"], ["قیمه"])
        self.assertEqual(menu["1405/08/12"]["شام"], ["ماکارونی", "کتلت"])


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
        self.assertTrue(inspect.iscoroutinefunction(web.dashboard))
        self.assertTrue(inspect.iscoroutinefunction(web.refresh_week_menu))
        self.assertTrue(inspect.iscoroutinefunction(web.sync_credit))
        self.assertTrue(inspect.iscoroutinefunction(web.run_auto_reserve_now))
        self.assertTrue(inspect.iscoroutinefunction(web.get_forgot_receipt_endpoint))

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

    def test_menus_schema_migration_and_upsert(self):
        import json, time
        from app import db

        with db.tx() as c:
            # Recreate old menus schema with PRIMARY KEY (user_id, date)
            c.execute("DROP TABLE IF EXISTS menus")
            c.execute("""
                CREATE TABLE menus (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    date TEXT NOT NULL,
                    data TEXT NOT NULL,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (user_id, date)
                )
            """)
            c.execute(
                "INSERT INTO menus (user_id, date, data, fetched_at) VALUES (1, '1405/08/02', '{}', 100)"
            )

        # Run init to trigger migration
        db.init()

        # Check that table has new primary key and ON CONFLICT works for different accounts
        with db.tx() as c:
            pk_cols = [r["name"] for r in sorted(c.execute("PRAGMA table_info(menus)").fetchall(), key=lambda r: r["pk"]) if r["pk"] > 0]
            self.assertIn("account_id", pk_cols)

            # Insert for account 1
            menu_data1 = json.dumps({"ناهار": {"foods": ["قیمه"]}}, ensure_ascii=False)
            c.execute(
                "INSERT INTO menus (user_id, account_id, date, data, fetched_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, account_id, date) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
                (1, 1, '1405/08/02', menu_data1, time.time())
            )

            # Insert for account 2 (same user, same date, different account)
            menu_data2 = json.dumps({"ناهار": {"foods": ["قورمه سبزی"]}}, ensure_ascii=False)
            c.execute(
                "INSERT INTO menus (user_id, account_id, date, data, fetched_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, account_id, date) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
                (1, 2, '1405/08/02', menu_data2, time.time())
            )

            row1 = c.execute("SELECT data FROM menus WHERE user_id = 1 AND account_id = 1 AND date = '1405/08/02'").fetchone()
            row2 = c.execute("SELECT data FROM menus WHERE user_id = 1 AND account_id = 2 AND date = '1405/08/02'").fetchone()
            self.assertIn("قیمه", json.loads(row1["data"])["ناهار"]["foods"])
            self.assertIn("قورمه سبزی", json.loads(row2["data"])["ناهار"]["foods"])

    def test_jalali_calendar_and_weekdays(self):
        from app import jalali

        self.assertEqual(jalali.weekday_name("1405/08/02"), "شنبه")
        self.assertEqual(jalali.weekday_name("1405/08/03"), "یکشنبه")
        self.assertEqual(jalali.weekday_name("1405/08/06"), "چهارشنبه")
        self.assertIn("شنبه", jalali.format_jalali_full("1405/08/02"))
        self.assertIn("آبان", jalali.format_jalali_short("1405/08/02"))

        days = jalali.get_upcoming_days(7)
        self.assertEqual(len(days), 7)
        self.assertTrue(days[0]["is_today"])
        self.assertEqual(days[0]["date"], jalali.today_str())

        next_week = jalali.get_next_week_days()
        self.assertGreaterEqual(len(next_week), 5)
        self.assertEqual(next_week[0]["day_name"], "شنبه")

    def test_accounts_credit_and_auto_reserve_fields(self):
        from starlette.requests import Request
        from app import web, db, config

        config.SECRET_KEY = "test-secret"
        db.init()
        with db.tx() as c:
            c.execute("DELETE FROM accounts")
            c.execute("DELETE FROM users")
            c.execute("INSERT INTO users (id, username, password_hash, created_at) VALUES (1, 'tester', 'hash', 0)")

        req = Request({"type": "http", "session": {"uid": 1}, "query_string": b""})
        web.save_account(req, site_username="40111111", site_password="pass1", display_name="معین")

        with db.tx() as c:
            acc = c.execute("SELECT * FROM accounts WHERE user_id = 1").fetchone()
            acc_id = acc["id"]
            # Test updating credit
            c.execute("UPDATE accounts SET credit = '3,500,000 ریال', credit_updated_at = 12345 WHERE id = ?", (acc_id,))

        # Test auto-reserve endpoint
        res = web.save_auto_reserve(req, account_id=acc_id, auto_reserve="1", meals=["ناهار", "شام"])
        self.assertEqual(res.status_code, 303)

        with db.tx() as c:
            updated = c.execute("SELECT credit, auto_reserve, auto_reserve_meals FROM accounts WHERE id = ?", (acc_id,)).fetchone()
            self.assertEqual(updated["credit"], "3,500,000 ریال")
            self.assertEqual(updated["auto_reserve"], 1)
            self.assertEqual(updated["auto_reserve_meals"], "ناهار,شام")


    def test_admin_settings_change_limits_live(self):
        from starlette.requests import Request
        from app import web, limits

        config.SECRET_KEY = "test-secret"
        with db.tx() as c:
            c.execute("DELETE FROM users")
            c.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'boss', 'x$y', 1, 0)")
            c.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (2, 'joe', 'x$y', 0, 0)")
        t = Throttle()
        try:
            # Non-admins can't change anything.
            web.admin_save_settings(Request({"type": "http", "session": {"uid": 2}}), MAX_REQUESTS_PER_HOUR="7")
            self.assertEqual(t.per_hour, limits.DEFAULTS["MAX_REQUESTS_PER_HOUR"])

            req = Request({"type": "http", "session": {"uid": 1}})
            web.admin_save_settings(req, MAX_REQUESTS_PER_HOUR="۹۰۰", POLL_SECONDS="2", POLL_JITTER="10")
            self.assertEqual((t.per_hour, t.min_interval, t.jitter), (900, 2.0, 0.1))

            # "No cap": the hourly window never raises.
            web.admin_save_settings(req, no_request_cap="1")
            self.assertEqual(t.per_hour, 0)
            t._hits.extend([time.monotonic()] * 5000)
            Throttle._check_window(t._hits, t.per_hour)
            self.assertFalse(t.stats()["is_limited"])

            # Stored settings survive a restart.
            config.MAX_REQUESTS_PER_HOUR = 123
            limits.load()
            self.assertEqual(config.MAX_REQUESTS_PER_HOUR, 0)
        finally:
            limits.reset()
        self.assertEqual(t.per_hour, limits.DEFAULTS["MAX_REQUESTS_PER_HOUR"])

    def test_leaked_default_admin_is_locked(self):
        from starlette.requests import Request
        from app import security, web

        with db.tx() as c:
            c.execute("DELETE FROM users")
            c.execute(
                "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (5, 'admin', ?, 1, 0)",
                (security.hash_password(db.LEAKED_ADMIN_PASSWORD),),
            )
            c.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (1, 'real', 'x$y', 0, 0)")
        db.init()
        with db.tx() as c:
            rows = {r["username"]: r for r in c.execute("SELECT * FROM users")}
        self.assertEqual(rows["admin"]["password_hash"], db.LOCKED)
        self.assertEqual(rows["real"]["is_admin"], 1)  # someone usable is admin
        # An old session cookie of the locked user stops working.
        req = Request({"type": "http", "session": {"uid": 5}})
        self.assertIsNone(web._uid(req))
        self.assertFalse(security.verify_password(db.LEAKED_ADMIN_PASSWORD, db.LOCKED))

    def test_account_status_is_per_account(self):
        from app import web

        with db.tx() as c:
            c.execute("DELETE FROM users")
            c.execute("INSERT INTO users (id, username, password_hash, created_at) VALUES (1, 'u', 'x$y', 0)")
            c.execute("DELETE FROM accounts")
            c.execute("INSERT INTO accounts (id, user_id, site_username, site_password_enc) VALUES (21, 1, '1', 'e')")
            c.execute("INSERT INTO accounts (id, user_id, site_username, site_password_enc) VALUES (22, 1, '2', 'e')")
        db.set_login_state(21, False, "رمز اشتباه")
        db.set_login_state(22, True)
        with db.tx() as c:
            a, b = (c.execute("SELECT * FROM accounts WHERE id = ?", (i,)).fetchone() for i in (21, 22))
        self.assertEqual(web.account_status(a)["status"], "expired")
        self.assertEqual(web.account_status(b)["status"], "online")

    def test_matches_excludes_minus_words_and_milk_priorities(self):
        from app.worker import priorities

        self.assertTrue(matches("شیر/حلواشکری/نان", "شیر -طعم"))
        self.assertFalse(matches("کره/مربا/شیرطعم دار/نان", "شیر -طعم"))
        self.assertTrue(matches("کره/مربا/شیرطعم دار/نان", "شیر طعم دار"))
        self.assertFalse(matches("کباب", "-مرغ"))  # only exclusions match nothing
        acc = {"priority_breakfast": "عسل", "priority_lunch": "جوجه\nقیمه", "priority_dinner": "", "milk_pref": "plain"}
        self.assertEqual(priorities(acc), {"صبحانه": ["شیر -طعم", "عسل"], "ناهار": ["جوجه", "قیمه"], "شام": []})

    def test_templates_compile(self):
        from app import web

        for name in ("dashboard.html", "login.html", "admin.html"):
            web.templates.env.get_template(name)

    def test_dashboard_shows_reservations_and_blocks_reserved_meals(self):
        from starlette.requests import Request

        from app import jalali, security, web

        today = jalali.today_str()
        with db.tx() as c:
            uid = c.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES ('dash', ?, 0)",
                (security.hash_password("password1"),),
            ).lastrowid
            aid = c.execute(
                "INSERT INTO accounts (user_id, site_username, site_password_enc, login_state, priority_lunch, milk_pref) "
                "VALUES (?, '401', 'e', 'ok', 'جوجه', 'flavored')", (uid,),
            ).lastrowid
        db.save_week_menu({today: {"صبحانه": ["پنیر/نان"], "ناهار": ["چلو جوجه کباب,دوغ", "قیمه"], "شام": []}})
        db.save_reservations(aid, {today: {"صبحانه": "", "ناهار": "چلو جوجه کباب,دوغ", "شام": ""}})

        def req(query=b""):
            return Request({"type": "http", "method": "GET", "path": "/", "query_string": query, "headers": [],
                            "session": {"uid": uid}, "scheme": "http", "server": ("test", 80), "root_path": ""})

        page = asyncio.run(web.dashboard(req(f"account={aid}".encode())))
        self.assertEqual(page.status_code, 200)
        body = page.body.decode()
        self.assertIn("رزروهای من", body)
        self.assertIn('class="res-cell yes"', body)  # the reserved lunch in the week table
        self.assertIn("رزرو داری: چلو جوجه کباب,دوغ", body)  # and in the buy form
        self.assertIn('value="ناهار|قیمه" disabled', body)
        self.assertIn('value="flavored" class="sr-only" checked', body)
        self.assertNotIn("skip_if_reserved", body)
        # A job for the reserved meal is refused.
        res = web.create_job(req(), date=today, meal="", foods="", quantity=1, active_from="", active_to="",
                             poll_seconds=0, picks=["ناهار|قیمه"], account_id=aid)
        self.assertEqual(res.status_code, 303)
        with db.tx() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM jobs WHERE account_id = ?", (aid,)).fetchone()[0], 0)

    def test_week_menu_is_shared_and_filters_picks(self):
        from app import web

        with db.tx() as c:
            c.execute("DELETE FROM week_menu")
        db.save_week_menu({"1405/08/05": {"ناهار": ["قیمه", "کباب"], "شام": ["عدس پلو"]}})
        db.save_week_menu({"1405/08/05": {"ناهار": ["قورمه سبزی"]}, "1405/08/06": {"شام": ["ماکارونی"]}})
        self.assertEqual(db.week_menu("1405/08/05"), {
            "1405/08/05": {"ناهار": ["قورمه سبزی"]}, "1405/08/06": {"شام": ["ماکارونی"]},
        })
        self.assertEqual(list(db.week_menu("1405/08/06")), ["1405/08/06"])
        # Only foods on that day's menu are kept; duplicates and other days' foods are dropped.
        picks = ["ناهار|قورمه سبزی", "ناهار|قورمه سبزی", "شام|ماکارونی", "ناهار|کباب", "bad"]
        self.assertEqual(web._picked_foods("1405/08/05", picks), {"ناهار": ["قورمه سبزی"]})
        self.assertEqual(web._picked_foods("1405/08/06", picks), {"شام": ["ماکارونی"]})


if __name__ == "__main__":
    unittest.main()
