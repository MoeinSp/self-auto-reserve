"""Background loop: one task per student account that has active jobs."""
import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from . import captcha, config, db, jalali, security
from .cullinan import WEEK_GRIDS, Attempt, CullinanClient, Job, LoginFailed, SiteError, launch_browser
from .ratelimit import RateLimited, Throttle
from .text import parse_foods

log = logging.getLogger(__name__)

IDLE_SECONDS = 30
MAX_BACKOFF = 600


def _in_window(job_row) -> bool:
    start, end = job_row["active_from"], job_row["active_to"]
    if not start or not end:
        return True
    now = jalali.now_iran().strftime("%H:%M")  # the panel's time fields are Iran time
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end  # window passes midnight


OP_TIMEOUT = 600  # covers a manual captcha wait (CAPTCHA_MANUAL_TIMEOUT)
RESERVATION_RECHECK = 600  # seconds between "is this meal already reserved?" checks per job
AUTO_RESERVE_RETRY = 3600  # seconds before a failed scheduled auto-reserve is tried again
WEEK_MENU_AUTO_KEY = "week_menu_auto"  # settings key: date of the last scheduled week-menu refresh


def _explain(exc: Exception) -> str:
    """Turn an error from the site into advice the user can act on."""
    if isinstance(exc, LoginFailed):
        return f"شماره دانشجویی یا رمز سامانه اشتباه است ({str(exc)[:120]}). اطلاعات را اصلاح و دوباره ذخیره کن."
    if isinstance(exc, RateLimited):
        return f"سقف تلاش ساعتی برای محافظت از حساب پر شده؛ حدود {int(exc.retry_after // 60) + 1} دقیقه دیگر دوباره امتحان کن."
    if isinstance(exc, asyncio.TimeoutError):
        return "زمان تمام شد (کپچا وارد نشد یا سایت جواب نداد)."
    if isinstance(exc, SiteError):
        return str(exc)
    if isinstance(exc, PlaywrightTimeout):
        return "سایت دانشگاه دیر جواب داد یا صفحه‌اش با انتظار ربات فرق داشت؛ کمی بعد دوباره امتحان کن."
    if isinstance(exc, PlaywrightError) and "net::" in str(exc):
        return "سایت دانشگاه در دسترس نیست؛ کمی بعد دوباره امتحان کن."
    # Details go to the server log (log.exception at the call site), not the panel.
    return "خطای غیرمنتظره رخ داد؛ اگر تکرار شد به مدیر پنل خبر بده."


PAYMENT_SESSIONS: dict[str, dict] = {}


class Worker:
    PAYMENT_SESSIONS = PAYMENT_SESSIONS

    def __init__(self):
        self.PAYMENT_SESSIONS = PAYMENT_SESSIONS
        self.tasks: dict[int, asyncio.Task] = {}  # account_id -> task
        self.throttles: dict[int, Throttle] = {}  # account_id -> Throttle
        # One on-demand operation (login test / menu fetch) per user, shown on the dashboard.
        self.ops: dict[int, dict] = {}  # user_id -> op dict
        self.reservation_checked: dict[int, float] = {}  # job id -> monotonic time of last check
        self.menu_refreshing = False  # one shared week-menu refresh at a time
        self.menu_auto_tried = 0.0  # time of the last scheduled refresh attempt
        self.auto_reserve_tried: dict[int, float] = {}  # account id -> time of the last scheduled auto-reserve
        self._pw = None
        self.browser = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self.browser = await launch_browser(self._pw)
        self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._supervisor.cancel()
        for task in self.tasks.values():
            task.cancel()
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def _supervise(self) -> None:
        while True:
            try:
                with db.tx() as c:
                    active_accounts = [
                        r["account_id"]
                        for r in c.execute(
                            "SELECT DISTINCT j.account_id FROM jobs j JOIN accounts a ON a.id = j.account_id "
                            "WHERE j.status = 'active' AND j.account_id IS NOT NULL"
                        )
                    ]
                for aid in active_accounts:
                    task = self.tasks.get(aid)
                    if task is None or task.done():
                        self.tasks[aid] = asyncio.create_task(self._run_account(aid))

                self._auto_refresh_week_menu()

                # Check weekly auto-reserve on Wednesdays
                today_str = jalali.today_str()
                if jalali.weekday_name(today_str) == "چهارشنبه":
                    next_week = jalali.get_next_week_days()
                    if next_week:
                        target_week = next_week[0]["date"]
                        with db.tx() as c:
                            auto_accounts = c.execute(
                                "SELECT * FROM accounts WHERE auto_reserve = 1 AND (last_auto_reserve != ? OR last_auto_reserve IS NULL)",
                                (target_week,),
                            ).fetchall()
                        for acc_row in auto_accounts:
                            aid = acc_row["id"]
                            uid = acc_row["user_id"]
                            # A failed run is retried at most once an hour, not on every loop (logins can get the account banned).
                            if time.time() - self.auto_reserve_tried.get(aid, 0) < AUTO_RESERVE_RETRY:
                                continue
                            if not self.busy(uid) and self.run_auto_reserve(uid, aid, target_week):
                                self.auto_reserve_tried[aid] = time.time()
            except Exception:
                log.exception("supervisor error")
            await asyncio.sleep(5)

    def throttle(self, account_id: int) -> Throttle:
        # Kept across task restarts so limits survive a job being re-created.
        return self.throttles.setdefault(account_id, Throttle())

    def reset_throttle(self, account_id: int | None = None) -> int:
        """Reset rate limits for one account or all accounts."""
        if account_id is not None:
            t = self.throttles.get(account_id)
            if t:
                t.reset()
            else:
                self.throttle(account_id).reset()
            return 1
        count = 0
        for t in self.throttles.values():
            t.reset()
            count += 1
        return count

    def get_throttle_stats(self, account_id: int) -> dict:
        t = self.throttles.get(account_id)
        return (t or Throttle()).stats()

    def client_for(self, account_id: int, own_lane: bool = False) -> CullinanClient:
        with db.tx() as c:
            acc = c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not acc:
            raise SiteError("اطلاعات حساب سامانه سلف پیدا نشد")

        user_id = acc["user_id"]
        acc_label = acc["display_name"] or acc["site_username"]

        async def solve(png: bytes, attempt: int) -> str | None:
            if attempt <= captcha.config.CAPTCHA_OCR_TRIES:
                code = await asyncio.to_thread(captcha.ocr, png)
                if code:
                    return code
                if attempt < captcha.config.CAPTCHA_OCR_TRIES:
                    return None
            db.log(user_id, f"کپچای حساب «{acc_label}» خوانده نشد؛ لطفاً در پنل واردش کن", "warn")
            return await captcha.manual.ask(user_id, png)

        throttle = self.throttle(account_id)
        return CullinanClient(
            self.browser, acc["site_username"], security.decrypt(acc["site_password_enc"]),
            throttle.lane() if own_lane else throttle, solve,
        )

    def busy(self, user_id: int) -> bool:
        op = self.ops.get(user_id)
        return bool(op and op["state"] == "running")

    def start_op(self, user_id: int, title: str, run: Callable[[CullinanClient], Awaitable[str]], account_id: int = 0) -> bool:
        """Run `run` in the background with its own browser session; False if one is already running."""
        if self.busy(user_id):
            return False
        op = self.ops[user_id] = {
            "title": title,
            "state": "running",
            "message": "",
            "started": time.time(),
            "account_id": account_id,
        }

        async def go():
            client = self.client_for(account_id, own_lane=True)
            client.interactive = True
            try:
                op["message"] = await asyncio.wait_for(run(client), OP_TIMEOUT)
                op["state"] = "ok"
                db.set_login_state(account_id, True)
                db.log(user_id, f"{title}: {op['message']}", "success")
            except Exception as exc:
                if not isinstance(exc, (LoginFailed, RateLimited, SiteError, asyncio.TimeoutError)):
                    log.exception("%s failed for user %s (account %s)", title, user_id, account_id)
                op["state"], op["message"] = "error", _explain(exc)
                if isinstance(exc, LoginFailed):
                    db.set_login_state(account_id, False, op["message"])
                db.log(user_id, f"{title} ناموفق: {op['message']}", "error")
            finally:
                op["finished"] = time.time()
                await client.close()

        try:
            asyncio.get_running_loop().create_task(go())
        except RuntimeError:  # not called from the event loop: don't leave a stuck "running" op
            del self.ops[user_id]
            raise
        return True

    def test_login(self, user_id: int, account_id: int) -> bool:
        async def run(client: CullinanClient) -> str:
            started = time.monotonic()
            await client.login()
            profile = await client.read_profile()
            name = profile.get("name") or client.username
            credit = profile.get("credit", "")
            with db.tx() as c:
                c.execute(
                    "UPDATE accounts SET display_name = ?, credit = ?, credit_updated_at = ? WHERE id = ?",
                    (name or "", credit, time.time(), account_id),
                )
            tries = f"، کپچا در تلاش {client.captcha_attempts} حل شد" if client.captcha_attempts > 1 else ""
            credit_str = f" · اعتبار سلف: {credit}" if credit else ""
            return f"ورود موفق بود. نام در سامانه: «{name}» ({time.monotonic() - started:.0f} ثانیه{tries}{credit_str})."

        return self.start_op(user_id, "تست ورود", run, account_id=account_id)

    def sync_account_profile(self, user_id: int, account_id: int) -> bool:
        async def run(client: CullinanClient) -> str:
            started = time.monotonic()
            await client.login()
            profile = await client.read_profile()
            name = profile.get("name") or client.username
            credit = profile.get("credit", "")
            with db.tx() as c:
                c.execute(
                    "UPDATE accounts SET display_name = ?, credit = ?, credit_updated_at = ? WHERE id = ?",
                    (name or "", credit, time.time(), account_id),
                )
            if credit:
                return f"موجودی سلف «{name}» استعلام شد: {credit} ({time.monotonic() - started:.0f} ثانیه)."
            return f"اطلاعات حساب «{name}» به‌روزرسانی شد ({time.monotonic() - started:.0f} ثانیه)."

        return self.start_op(user_id, "استعلام موجودی سلف", run, account_id=account_id)

    def fetch_forgot_receipt(self, user_id: int, account_id: int, date: str, meal: str, reserve_type: str = "0") -> bool:
        async def run(client: CullinanClient) -> str:
            started = time.monotonic()
            await client.login()
            res = await client.get_forgot_receipt(date, meal, reserve_type)
            day_name = jalali.weekday_name(date)
            date_label = f"{day_name} {date}" if day_name else date
            meal_label = f"وعدهٔ {meal}" if meal else "وعده انتخابی"
            if res.get("code"):
                code = res["code"]
                msg = f"کد فیش فراموشی برای {meal_label} ({date_label}): {code}"
                db.log(user_id, f"🎫 {msg}", "info")
                return msg
            if res.get("success"):
                msg = res.get("message") or f"فیش فراموشی برای {meal_label} ({date_label}) با موفقیت صادر شد."
                db.log(user_id, f"🎫 {msg}", "info")
                return msg
            err_msg = res.get("message") or "فیش فراموشی صادر نشد"
            raise SiteError(f"دریافت فیش برای {meal_label} ({date_label}) ناموفق: {err_msg}")

        return self.start_op(user_id, "دریافت فیش فراموشی", run, account_id=account_id)

    def run_auto_reserve(self, user_id: int, account_id: int, target_week_date: str = "") -> bool:
        async def run(client: CullinanClient) -> str:
            await client.login()
            with db.tx() as c:
                acc = c.execute("SELECT auto_reserve_meals FROM accounts WHERE id = ?", (account_id,)).fetchone()
            meals_str = acc["auto_reserve_meals"] if acc and acc["auto_reserve_meals"] else "ناهار,شام"
            meals = [m.strip() for m in meals_str.split(",") if m.strip()]

            target = target_week_date
            if not target:
                next_week = jalali.get_next_week_days()
                target = next_week[0]["date"] if next_week else jalali.today_str()

            results = await client.auto_reserve_week(target, meals)
            with db.tx() as c:
                c.execute("UPDATE accounts SET last_auto_reserve = ? WHERE id = ?", (target, account_id))

            summary = "؛ ".join(results) if results else "موردی برای رزرو جدید پیدا نشد یا تمام وعده‌ها از قبل رزرو شده بودند."
            db.log(user_id, f"🤖 رزرو خودکار هفته {target}: {summary}", "info")
            return f"رزرو خودکار هفته {target} انجام شد: {summary}"

        return self.start_op(user_id, "رزرو خودکار هفته بعد", run, account_id=account_id)

    def request_payment_gateway(self, user_id: int, account_id: int, amount_rials: int, bank_idx: int = 0) -> bool:
        async def run(client: CullinanClient) -> str:
            await client.login()
            data = await client.create_payment_session(amount_rials, bank_idx)
            import secrets
            token = secrets.token_urlsafe(16)
            import json
            now_ts = time.time()
            PAYMENT_SESSIONS[token] = {
                "action": data["action"],
                "method": data.get("method", "POST"),
                "fields": data.get("fields", {}),
                "bank": data.get("bank", "درگاه پرداخت"),
                "amount_rials": amount_rials,
                "created_at": now_ts,
            }
            with db.tx() as c:
                c.execute(
                    "INSERT OR REPLACE INTO payment_sessions (token, action, method, fields, bank, amount_rials, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (token, data["action"], data.get("method", "POST"), json.dumps(data.get("fields", {})), data.get("bank", "درگاه پرداخت"), amount_rials, now_ts)
                )
            gateway_url = f"/pay/gateway/{token}"
            db.log(user_id, f"💳 لینک درگاه {data.get('bank')} برای مبلغ {amount_rials:,} ریال ایجاد شد.", "info")
            return f"PAYMENT_READY:{gateway_url}:{data.get('bank')}:{amount_rials}"

        return self.start_op(user_id, "ایجاد لینک درگاه پرداخت", run, account_id=account_id)

    def refresh_week_menu(self, user_id: int, account_id: int, auto: bool = False) -> bool:
        """Read this week's and next week's menu once and store it for everyone."""
        if self.menu_refreshing:
            return False
        self.menu_refreshing = True

        async def run(client: CullinanClient) -> str:
            try:
                week: dict[str, dict[str, list[str]]] = {}
                next_week = jalali.get_next_week_days()
                for target in [jalali.today_str()] + ([next_week[0]["date"]] if next_week else []):
                    if target in week:
                        continue
                    try:
                        found = await client.read_week(target)
                    except SiteError:
                        if not week:
                            raise
                        continue  # next week not published yet
                    stored = db.week_menu(min(found)) if found else {}
                    for day, meals in found.items():
                        # Every meal is kept, empty when the site has nothing for it that day. A meal
                        # whose grid was missing from the page keeps the foods saved before.
                        old = stored.get(day, {})
                        week[day] = {
                            meal: meals[meal]["foods"] if meal in meals else old.get(meal, [])
                            for meal in WEEK_GRIDS
                        }
                if not any(foods for meals in week.values() for foods in meals.values()):
                    raise SiteError("در برنامهٔ غذایی این هفته غذایی پیدا نشد.")
                db.save_week_menu(week)
                if auto:
                    db.set_setting(WEEK_MENU_AUTO_KEY, jalali.today_str())
                days = sorted(week)
                return f"غذاهای {len(days)} روز ({days[0]} تا {days[-1]}) برای همه ذخیره شد."
            finally:
                self.menu_refreshing = False

        title = "بروزرسانی خودکار غذاهای هفته" if auto else "بروزرسانی غذاهای هفته"
        if not self.start_op(user_id, title, run, account_id=account_id):
            self.menu_refreshing = False
            return False
        return True

    def _auto_refresh_week_menu(self) -> None:
        """Every Tuesday from 18:00 (Iran time), refresh the shared menu once, retrying hourly on failure."""
        now = jalali.now_iran()
        today = jalali.today_str()
        if jalali.weekday_name(today) != "سه‌شنبه" or now.hour < 18 or self.menu_refreshing:
            return
        if db.get_setting(WEEK_MENU_AUTO_KEY) == today or time.time() - self.menu_auto_tried < 3600:
            return
        with db.tx() as c:
            accounts = c.execute(
                "SELECT id, user_id FROM accounts WHERE login_state = 'ok' ORDER BY login_at DESC"
            ).fetchall()
        acc = next((a for a in accounts if not self.busy(a["user_id"])), None)
        if acc:
            self.menu_auto_tried = time.time()
            self.refresh_week_menu(acc["user_id"], acc["id"], auto=True)

    async def _run_account(self, account_id: int) -> None:
        with db.tx() as c:
            acc = c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not acc:
            return
        user_id = acc["user_id"]
        client = self.client_for(account_id)
        backoff = 30
        login_ok = False  # login state already recorded for this run
        try:
            while True:
                with db.tx() as c:
                    jobs = c.execute(
                        "SELECT * FROM jobs WHERE account_id = ? AND status = 'active' ORDER BY id",
                        (account_id,),
                    ).fetchall()
                if not jobs:
                    return
                today = jalali.today_str()
                ran = False
                for row in jobs:
                    if row["date"] < today:
                        db.set_job_status(row["id"], "failed", "تاریخ گذشت و غذا پیدا نشد")
                        db.log(user_id, f"کار #{row['id']} منقضی شد", "warn", row["id"])
                        continue
                    if not _in_window(row):
                        continue
                    ran = True
                    job = Job(row["id"], row["date"], row["meal"], parse_foods(row["foods"]), row["quantity"])
                    client.poll_interval = row["poll_seconds"] or None
                    try:
                        reserved = await self._reserved_food(client, row)
                        if reserved:
                            msg = f"برای {row['meal']} {row['date']} رزرو داری ({reserved})؛ از سبد نخریدم."
                            db.set_job_status(row["id"], "stopped", msg)
                            db.log(user_id, f"کار #{row['id']}: {msg}", "warn", row["id"])
                            continue
                        result = await client.attempt(job)
                        backoff = 30
                        if not login_ok:
                            db.set_login_state(account_id, True)
                            login_ok = True
                        # If profile name found and not saved, save it
                        if client.display_name and not acc["display_name"]:
                            with db.tx() as c:
                                c.execute("UPDATE accounts SET display_name = ? WHERE id = ?", (client.display_name, account_id))
                        self._handle(user_id, row, result, acc["display_name"] or acc["site_username"])
                    except LoginFailed as exc:
                        msg = _explain(exc)
                        db.set_login_state(account_id, False, msg)
                        for j in jobs:
                            db.set_job_status(j["id"], "failed", msg)
                        db.log(user_id, f"[{acc['display_name'] or acc['site_username']}]: {msg}", "error")
                        return
                    except RateLimited as exc:
                        db.set_job_message(row["id"], "سقف درخواست ساعتی پر شد؛ صبر می‌کنم")
                        # Short naps: the admin may raise the cap or reset the counter meanwhile.
                        await asyncio.sleep(min(exc.retry_after, 30))
                    except Exception as exc:
                        log.exception("job %s failed", row["id"])
                        reason = _explain(exc)
                        db.log(user_id, f"خطا در کار #{row['id']}: {reason}", "error", row["id"])
                        db.set_job_message(row["id"], f"{reason} ({backoff} ثانیه دیگر دوباره امتحان می‌کنم)")
                        await client.close()
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                if not ran:
                    await asyncio.sleep(IDLE_SECONDS)
        finally:
            await client.close()

    async def _reserved_food(self, client: CullinanClient, row) -> str:
        """The food already reserved for the job's meal ("" if none), checked now and then."""
        if not row["skip_if_reserved"] or not row["meal"]:
            return ""
        last = self.reservation_checked.get(row["id"])
        if last is not None and time.monotonic() - last < RESERVATION_RECHECK:
            return ""
        menu = await client.read_menu(row["date"], row["meal"])
        self.reservation_checked[row["id"]] = time.monotonic()
        return next((info["reserved"] for info in menu.values() if info["reserved"]), "")

    @staticmethod
    def _handle(user_id: int, row, result: Attempt, account_name: str = "") -> None:
        seen = "، ".join(result.seen) if result.seen else "-"
        prefix = f"[{account_name}] " if account_name else ""
        if result.status == "bought":
            n = db.count_attempt(row["id"], result.message)
            msg = f"{prefix}{result.message} (در تلاش {n})"
            db.set_job_status(row["id"], "done", msg)
            db.log(user_id, msg, "success", row["id"])
        elif result.status == "error":
            n = db.count_attempt(row["id"], result.message)
            # The save may still have gone through; stop to avoid buying twice.
            db.set_job_status(row["id"], "stopped", f"{prefix}{result.message} (در تلاش {n}) — در سایت بررسی کن")
            db.log(user_id, f"{prefix}{result.message}", "error", row["id"])
        else:
            db.count_attempt(row["id"], f"{result.message} (موجود در سبد: {seen})")


worker = Worker()
