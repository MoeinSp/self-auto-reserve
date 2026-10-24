"""Background loop: one task per student account that has active jobs."""
import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from . import captcha, db, jalali, security
from .cullinan import Attempt, CullinanClient, Job, LoginFailed, SiteError, launch_browser
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


class Worker:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}  # account_id -> task
        self.throttles: dict[int, Throttle] = {}  # account_id -> Throttle
        # One on-demand operation (login test / menu fetch) per user, shown on the dashboard.
        self.ops: dict[int, dict] = {}  # user_id -> op dict
        self.reservation_checked: dict[int, float] = {}  # job id -> monotonic time of last check
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
            except Exception:
                log.exception("supervisor error")
            await asyncio.sleep(5)

    def throttle(self, account_id: int) -> Throttle:
        # Kept across task restarts so limits survive a job being re-created.
        return self.throttles.setdefault(account_id, Throttle())

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
                db.log(user_id, f"{title}: {op['message']}", "success")
            except Exception as exc:
                if not isinstance(exc, (LoginFailed, RateLimited, SiteError, asyncio.TimeoutError)):
                    log.exception("%s failed for user %s (account %s)", title, user_id, account_id)
                op["state"], op["message"] = "error", _explain(exc)
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
            if name:
                with db.tx() as c:
                    c.execute("UPDATE accounts SET display_name = ? WHERE id = ?", (name, account_id))
            tries = f"، کپچا در تلاش {client.captcha_attempts} حل شد" if client.captcha_attempts > 1 else ""
            credit_str = f" · اعتبار: {profile['credit']}" if profile.get("credit") else ""
            return f"ورود موفق بود. نام در سامانه: «{name}» ({time.monotonic() - started:.0f} ثانیه{tries}{credit_str})."

        return self.start_op(user_id, "تست ورود", run, account_id=account_id)

    def fetch_menu(self, user_id: int, account_id: int, date: str, meal: str = "") -> bool:
        async def run(client: CullinanClient) -> str:
            found = await client.read_menu(date, meal)
            profile = await client.read_profile()
            if profile.get("name"):
                with db.tx() as c:
                    c.execute("UPDATE accounts SET display_name = ? WHERE id = ?", (profile["name"], account_id))
            with db.tx() as c:
                row = c.execute(
                    "SELECT data FROM menus WHERE user_id = ? AND account_id = ? AND date = ?",
                    (user_id, account_id, date),
                ).fetchone()
                menu = {**json.loads(row["data"]), **found} if row and meal else found
                c.execute(
                    "INSERT INTO menus (user_id, account_id, date, data, fetched_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, account_id, date) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
                    (user_id, account_id, date, json.dumps(menu, ensure_ascii=False), time.time()),
                )
            what = f"{meal} {date}" if meal else date
            if not any(info["foods"] for info in found.values()):
                return f"برای {what} در برنامهٔ غذایی غذایی تعریف نشده."
            reserved = [f"{m}: {info['reserved']}" for m, info in found.items() if info["reserved"]]
            note = f" رزرو فعلی تو — {'، '.join(reserved)}." if reserved else " برای این وعده‌ها رزروی نداری."
            return f"غذاهای {what} پیدا شد؛ تیک بزن و «شروع پیگیری» را بزن.{note}"

        return self.start_op(user_id, "دریافت لیست غذا", run, account_id=account_id)

    async def _run_account(self, account_id: int) -> None:
        with db.tx() as c:
            acc = c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not acc:
            return
        user_id = acc["user_id"]
        client = self.client_for(account_id)
        backoff = 30
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
                        # If profile name found and not saved, save it
                        if client.display_name and not acc["display_name"]:
                            with db.tx() as c:
                                c.execute("UPDATE accounts SET display_name = ? WHERE id = ?", (client.display_name, account_id))
                        self._handle(user_id, row, result, acc["display_name"] or acc["site_username"])
                    except LoginFailed as exc:
                        msg = _explain(exc)
                        for j in jobs:
                            db.set_job_status(j["id"], "failed", msg)
                        db.log(user_id, f"[{acc['display_name'] or acc['site_username']}]: {msg}", "error")
                        return
                    except RateLimited as exc:
                        db.set_job_message(row["id"], "سقف درخواست ساعتی پر شد؛ صبر می‌کنم")
                        await asyncio.sleep(exc.retry_after)
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
