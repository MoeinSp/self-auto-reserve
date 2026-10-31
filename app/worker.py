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
from .cullinan import RESERVATION_PATH, WEEK_GRIDS, Attempt, CullinanClient, Job, LoginFailed, SiteError, launch_browser
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
PRIORITY_COLUMNS = {"صبحانه": "priority_breakfast", "ناهار": "priority_lunch", "شام": "priority_dinner"}
AUTO_RESERVE_RETRY = 3600  # seconds before a failed scheduled auto-reserve is tried again
WEEK_MENU_AUTO_KEY = "week_menu_auto"  # settings key: date of the last scheduled week-menu refresh
CREDIT_LATER_KEY = "credit_later"  # settings key: JSON {account id: [due times]} of scheduled credit reads
SESSION_IDLE = 15 * 60  # seconds a kept site session is reused after its last run
CREDIT_LATER = (3600, 12 * 3600)  # after a meal goes in the basket: its sale (and the credit) comes later


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


def priorities(acc) -> dict[str, list[str]]:
    """The account's food priorities for auto-reserve: {meal: [wanted, ...]}, highest first.

    Breakfast's milk preference goes first: "flavored" -> شیر طعم دار, "plain" -> شیر (not flavored).
    """
    if not acc:
        return {}
    out = {meal: parse_foods(acc[col] or "") for meal, col in PRIORITY_COLUMNS.items()}
    milk = {"flavored": ["شیر طعم دار"], "plain": ["شیر -طعم"]}.get(acc["milk_pref"] or "", [])
    out["صبحانه"] = milk + out["صبحانه"]
    return out


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
        self.login_throttles: dict[str, Throttle] = {}  # student number -> Throttle, for sign-in with site account
        self.auto_reserve_tried: dict[int, float] = {}  # account id -> time of the last scheduled auto-reserve
        self.credit_reading: set[int] = set()  # accounts with a scheduled credit read running
        self.sessions: dict[int, tuple] = {}  # account id -> (logged-in BrowserContext, last use), see _lend_session
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
                self._due_credit_reads()
                self._drop_idle_sessions()

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

    async def verify_site_login(self, username: str, password: str) -> dict:
        """Log in to the university site with these credentials (panel sign-in without a panel
        account); returns the profile. Raises LoginFailed / RateLimited / SiteError."""
        throttle = self.login_throttles.setdefault(username, Throttle())

        async def solve(png: bytes, attempt: int) -> str | None:
            if attempt <= captcha.config.CAPTCHA_OCR_TRIES:
                return await asyncio.to_thread(captcha.ocr, png)
            return None  # nobody to ask yet: the user isn't signed in

        client = CullinanClient(self.browser, username, password, throttle.lane(), solve)
        try:
            await asyncio.wait_for(client.login(), 120)
            return await client.read_profile()
        finally:
            await client.close()

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

    # ---------------------------------------------------- kept site sessions
    # On-demand runs reuse the account's logged-in browser context, so they skip the
    # login (and its captcha): one page load instead of 10-45 s. The site drops idle
    # sessions after a while; then login() just signs in again.
    def _lend_session(self, account_id: int, client: CullinanClient) -> bool:
        client.keep_context = True
        kept = self.sessions.pop(account_id, None)
        if kept and time.time() - kept[1] < SESSION_IDLE:
            client.context = kept[0]
            return True
        if kept:
            asyncio.get_running_loop().create_task(self._close_context(kept[0]))
        return False

    async def _return_session(self, account_id: int, client: CullinanClient, keep: bool, reused: bool) -> None:
        await client.close()  # the page only
        context = client.context
        if context is None:
            return
        if keep and account_id not in self.sessions:
            self.sessions[account_id] = (context, time.time())
        else:
            await self._close_context(context)

    @staticmethod
    async def _close_context(context) -> None:
        try:
            await context.close()
        except Exception:
            pass

    def _drop_idle_sessions(self) -> None:
        now = time.time()
        for aid, (context, used) in list(self.sessions.items()):
            if now - used >= SESSION_IDLE:
                del self.sessions[aid]
                asyncio.get_running_loop().create_task(self._close_context(context))

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

        op["steps"] = []

        def step(text: str) -> None:
            op["steps"] = (op["steps"] + [text])[-8:]

        async def go():
            client = self.client_for(account_id, own_lane=True)
            client.interactive = True
            client.step = step
            reused = self._lend_session(account_id, client)
            keep = False
            try:
                op["message"] = await asyncio.wait_for(run(client), OP_TIMEOUT)
                op["state"] = "ok"
                keep = True
                db.set_login_state(account_id, True)
                db.log(user_id, f"{title}: {op['message']}", "success")
            except Exception as exc:
                if not isinstance(exc, (LoginFailed, RateLimited, SiteError, asyncio.TimeoutError)):
                    log.exception("%s failed for user %s (account %s)", title, user_id, account_id)
                op["state"], op["message"] = "error", _explain(exc)
                keep = isinstance(exc, SiteError)  # the session itself is still fine
                if isinstance(exc, LoginFailed):
                    db.set_login_state(account_id, False, op["message"])
                db.log(user_id, f"{title} ناموفق: {op['message']}", "error")
            finally:
                op["finished"] = time.time()
                await self._return_session(account_id, client, keep, reused)

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
            # Right after logging in, read what this account has bought this week and next.
            try:
                await self._sync_weeks(client, account_id)
                count = sum(len(m) for m in db.reservations(account_id, jalali.today_str()).values())
                res_str = f" · {count} وعدهٔ رزروشده" if count else " · وعدهٔ رزروشده‌ای نداری"
            except SiteError as exc:
                res_str = f" · رزروها خوانده نشد ({exc})"
            return f"ورود موفق بود. نام در سامانه: «{name}» ({time.monotonic() - started:.0f} ثانیه{tries}{credit_str}{res_str})."

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
                acc = c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            meals_str = acc["auto_reserve_meals"] if acc and acc["auto_reserve_meals"] else "ناهار,شام"
            meals = [m.strip() for m in meals_str.split(",") if m.strip()]

            target = target_week_date
            if not target:
                next_week = jalali.get_next_week_days()
                target = next_week[0]["date"] if next_week else jalali.today_str()

            results = await client.auto_reserve_week(target, meals, priorities(acc))
            with db.tx() as c:
                c.execute("UPDATE accounts SET last_auto_reserve = ? WHERE id = ?", (target, account_id))
            after = await self._after_change(client, account_id)  # new reservations, menu and credit in the panel

            summary = "؛ ".join(results) if results else "موردی برای رزرو جدید پیدا نشد یا تمام وعده‌ها از قبل رزرو شده بودند."
            summary += f" ({after})" if after else ""
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

    async def _sync_weeks(self, client: CullinanClient, account_id: int) -> dict[str, dict[str, list[str]]]:
        """Read this week and next week from the reservation page; store the shared menu
        and this account's reservations. Returns the stored menu {date: {meal: [food, ...]}}."""
        found: dict[str, dict[str, dict]] = {}
        next_week = jalali.get_next_week_days()
        for target in [jalali.today_str()] + ([next_week[0]["date"]] if next_week else []):
            if target in found:
                continue
            try:
                found.update(await client.read_week(target))
            except SiteError:
                if not found:
                    raise
                # next week not published yet
        stored = db.week_menu(min(found)) if found else {}
        week: dict[str, dict[str, list[str]]] = {}
        for day, meals in found.items():
            # Every meal is kept, empty when the site has nothing for it that day. A meal
            # whose grid was missing from the page keeps the foods saved before.
            old = stored.get(day, {})
            week[day] = {meal: meals[meal]["foods"] if meal in meals else old.get(meal, []) for meal in WEEK_GRIDS}
        if not any(foods for meals in week.values() for foods in meals.values()):
            raise SiteError("در برنامهٔ غذایی این هفته غذایی پیدا نشد.")
        db.save_week_menu(week)
        db.save_reservations(account_id, {
            day: {meal: info["reserved"] for meal, info in meals.items()} for day, meals in found.items()
        }, {day: [meal for meal, info in meals.items() if info["locked"]] for day, meals in found.items()})
        return week

    def check_reservations(self, user_id: int, account_id: int) -> bool:
        """Read which meals of this week and next week the account has bought."""
        async def run(client: CullinanClient) -> str:
            await self._sync_weeks(client, account_id)
            count = sum(len(m) for m in db.reservations(account_id, jalali.today_str()).values())
            return f"رزروها بررسی شد: {count} وعدهٔ رزروشده از امروز تا آخر هفتهٔ بعد." if count else                 "رزروها بررسی شد: از امروز تا آخر هفتهٔ بعد وعدهٔ رزروشده‌ای نداری."

        return self.start_op(user_id, "بررسی رزروهای هفته", run, account_id=account_id)

    @staticmethod
    def _save_credit(client: CullinanClient, account_id: int, profile: dict) -> str:
        credit = profile.get("credit", "")
        if credit:
            with db.tx() as c:
                c.execute("UPDATE accounts SET credit = ?, credit_updated_at = ? WHERE id = ?", (credit, time.time(), account_id))
        return credit

    async def _after_change(self, client: CullinanClient, account_id: int) -> str:
        """After something that changed reservations and credit (auto-reserve, a basket buy):
        re-read this week's and next week's reservations and menu, and the credit from the
        reservation page just opened (fresh, no extra request). Returns a note for the user."""
        notes = []
        try:
            client.step("به‌روزرسانی جدول رزروها و موجودی…")
            await client._open(RESERVATION_PATH)  # a fresh load: shows the credit after the change
            await self._sync_weeks(client, account_id)
            notes.append("جدول رزروها به‌روز شد")
        except Exception as exc:
            log.warning("reservations re-read after a change failed (account %s): %s", account_id, exc)
        try:
            credit = self._save_credit(client, account_id, await client.read_profile())
            if credit:
                notes.append(f"اعتبار جدید: {credit}")
        except Exception as exc:
            log.warning("credit read after a change failed (account %s): %s", account_id, exc)
        return " · ".join(notes)

    @staticmethod
    def _credit_later(account_id: int) -> None:
        """Read the credit again 1 h and 12 h from now (a meal in the basket sells later)."""
        plan = json.loads(db.get_setting(CREDIT_LATER_KEY) or "{}")
        now = time.time()
        plan[str(account_id)] = sorted(set(plan.get(str(account_id), []) + [now + d for d in CREDIT_LATER]))
        db.set_setting(CREDIT_LATER_KEY, json.dumps(plan))

    def _due_credit_reads(self) -> None:
        """Start the scheduled credit reads that are due (one browser session each, quietly)."""
        plan = json.loads(db.get_setting(CREDIT_LATER_KEY) or "{}")
        now, changed = time.time(), False
        for aid, times in list(plan.items()):
            if not any(t <= now for t in times) or int(aid) in self.credit_reading:
                continue
            plan[aid] = [t for t in times if t > now]
            if not plan[aid]:
                del plan[aid]
            changed = True
            self.credit_reading.add(int(aid))
            asyncio.get_running_loop().create_task(self._read_credit_quietly(int(aid)))
        if changed:
            db.set_setting(CREDIT_LATER_KEY, json.dumps(plan))

    async def _read_credit_quietly(self, account_id: int) -> None:
        client = None
        try:
            with db.tx() as c:
                acc = c.execute("SELECT user_id FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not acc:
                return
            client = self.client_for(account_id, own_lane=True)
            await client.login()
            credit = self._save_credit(client, account_id, await client.read_profile())
            if credit:
                db.log(acc["user_id"], f"موجودی سلف خودکار استعلام شد: {credit}", "info")
        except Exception as exc:
            log.warning("scheduled credit read failed (account %s): %s", account_id, exc)
        finally:
            self.credit_reading.discard(account_id)
            if client:
                await client.close()

    @staticmethod
    def _record_sale(account_id: int, date: str, meal: str, info: dict) -> str:
        """Store what the sale page showed; returns a line for the user."""
        food = info.get("food") or meal
        if info["state"] == "listed":
            db.save_sale(account_id, date, meal, "listed", info.get("food", ""), info.get("price", ""), info["status"])
            price = f" به قیمت {info['price']}" if info.get("price") else ""
            return f"«{food}» ({meal} {date}){price} داخل سبد است؛ در انتظار فروش"
        if info["state"] == "gone":
            db.save_sale(account_id, date, meal, "sold", info.get("food", ""), info.get("price", ""), info["status"])
            return f"«{food}» ({meal} {date}) فروخته شد{': ' + info['status'] if info['status'] else ''}"
        db.drop_sale(account_id, date, meal)  # back to a plain reservation
        return f"«{food}» ({meal} {date}) داخل سبد نیست و هنوز رزرو توست"

    def sell_to_basket(self, user_id: int, account_id: int, date: str, meals: list[str]) -> bool:
        """Put reserved meals of `date` in the food basket for sale (sold when someone buys them)."""
        async def run(client: CullinanClient) -> str:
            done, failed = [], []
            for meal in meals:
                try:
                    done.append(self._record_sale(account_id, date, meal, await client.sell_to_basket(date, meal)))
                except SiteError as exc:
                    failed.append(f"✗ {exc}")
            if done:
                self._credit_later(account_id)
                done.append("موجودی یک ساعت و ۱۲ ساعت بعد خودکار استعلام می‌شود")
            if failed and not done:
                raise SiteError(" · ".join(failed))
            return " · ".join(done + failed)

        return self.start_op(user_id, "فروش به سبد غذایی", run, account_id=account_id)

    def save_week_choices(self, user_id: int, account_id: int, changes: dict[str, dict[str, str]]) -> bool:
        """Reserve / change / cancel weekly reservations chosen in the panel, then refresh the table and credit."""
        async def run(client: CullinanClient) -> str:
            results = await client.save_week_choices(changes)
            after = await self._after_change(client, account_id)
            ok = sum(r.startswith("★") for r in results)
            head = f"{ok} مورد ثبت شد" if ok else "چیزی ثبت نشد"
            text = " · ".join([head] + results + ([after] if after else []))
            if not ok and any(r.startswith("✗") for r in results):
                raise SiteError(text)
            return text

        return self.start_op(user_id, "ثبت رزرو هفتگی", run, account_id=account_id)

    def check_sale(self, user_id: int, account_id: int, date: str, meal: str) -> bool:
        """Look once whether a meal waiting in the basket has been sold. Only on demand: never polled."""
        async def run(client: CullinanClient) -> str:
            info = await client.sale_status(date, meal)
            note = self._record_sale(account_id, date, meal, info)
            if info["state"] == "gone":  # sold: the page just opened shows the new credit
                credit = self._save_credit(client, account_id, await client.read_profile())
                note += f" · اعتبار جدید: {credit}" if credit else ""
            return note

        return self.start_op(user_id, "بررسی فروش سبد", run, account_id=account_id)

    def cancel_sale(self, user_id: int, account_id: int, date: str, meal: str) -> bool:
        """Take a meal waiting in the basket back out; it stays reserved."""
        async def run(client: CullinanClient) -> str:
            return self._record_sale(account_id, date, meal, await client.cancel_sale(date, meal))

        return self.start_op(user_id, "حذف از سبد غذایی", run, account_id=account_id)

    def refresh_week_menu(self, user_id: int, account_id: int, auto: bool = False) -> bool:
        """Read this week's and next week's menu once and store it for everyone."""
        if self.menu_refreshing:
            return False
        self.menu_refreshing = True

        async def run(client: CullinanClient) -> str:
            try:
                week = await self._sync_weeks(client, account_id)
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
                        if result.status == "bought":
                            after = await self._after_change(client, account_id)
                            if after:
                                db.log(user_id, f"[{acc['display_name'] or acc['site_username']}] بعد از خرید: {after}", "info", row["id"])
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
        """The food already reserved for the job's meal ("" if none): with a reservation the site
        refuses basket purchases, so the job stops. Saved reservations first, the site now and then."""
        if not row["meal"]:
            return ""
        saved = db.reservations(row["account_id"], row["date"]).get(row["date"], {}).get(row["meal"])
        if saved:
            return saved
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
