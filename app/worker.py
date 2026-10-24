"""Background loop: one task per user that has active jobs."""
import asyncio
import datetime as dt
import logging

from playwright.async_api import async_playwright

from . import captcha, db, jalali, security
from .cullinan import Attempt, CullinanClient, Job, LoginFailed, launch_browser
from .ratelimit import RateLimited, Throttle
from .text import parse_foods

log = logging.getLogger(__name__)

IDLE_SECONDS = 30
MAX_BACKOFF = 600


def _in_window(job_row) -> bool:
    start, end = job_row["active_from"], job_row["active_to"]
    if not start or not end:
        return True
    now = dt.datetime.now().strftime("%H:%M")
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end  # window passes midnight


def _active_jobs(user_id: int) -> list:
    with db.tx() as c:
        return c.execute(
            "SELECT * FROM jobs WHERE user_id = ? AND status = 'active' ORDER BY id", (user_id,)
        ).fetchall()


class Worker:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}
        self.throttles: dict[int, Throttle] = {}
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
                    users = [
                        r["user_id"]
                        for r in c.execute(
                            "SELECT DISTINCT j.user_id FROM jobs j JOIN accounts a ON a.user_id = j.user_id "
                            "WHERE j.status = 'active'"
                        )
                    ]
                for uid in users:
                    task = self.tasks.get(uid)
                    if task is None or task.done():
                        self.tasks[uid] = asyncio.create_task(self._run_user(uid))
            except Exception:
                log.exception("supervisor error")
            await asyncio.sleep(5)

    def throttle(self, user_id: int) -> Throttle:
        # Kept across task restarts so limits survive a job being re-created.
        return self.throttles.setdefault(user_id, Throttle())

    def client_for(self, user_id: int) -> CullinanClient:
        with db.tx() as c:
            acc = c.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()

        async def solve(png: bytes, attempt: int) -> str | None:
            if attempt <= captcha.config.CAPTCHA_OCR_TRIES:
                code = await asyncio.to_thread(captcha.ocr, png)
                if code:
                    return code
                if attempt < captcha.config.CAPTCHA_OCR_TRIES:
                    return None
            db.log(user_id, "کپچا خوانده نشد؛ لطفاً در پنل واردش کن", "warn")
            return await captcha.manual.ask(user_id, png)

        return CullinanClient(
            self.browser, acc["site_username"], security.decrypt(acc["site_password_enc"]),
            self.throttle(user_id), solve,
        )

    async def test_login(self, user_id: int) -> str:
        client = self.client_for(user_id)
        try:
            await client.login()
            return "ورود موفق بود ✅"
        finally:
            await client.close()

    async def _run_user(self, user_id: int) -> None:
        client = self.client_for(user_id)
        backoff = 30
        try:
            while True:
                jobs = _active_jobs(user_id)
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
                    try:
                        result = await client.attempt(job)
                        backoff = 30
                        self._handle(user_id, row, result)
                    except LoginFailed as exc:
                        msg = f"ورود ناموفق: {exc}. رمز رو در پنل اصلاح کن."
                        for j in jobs:
                            db.set_job_status(j["id"], "failed", msg)
                        db.log(user_id, msg, "error")
                        return
                    except RateLimited as exc:
                        db.set_job_message(row["id"], "سقف درخواست ساعتی پر شد؛ صبر می‌کنم")
                        await asyncio.sleep(exc.retry_after)
                    except Exception as exc:
                        log.exception("job %s failed", row["id"])
                        db.log(user_id, f"خطا در کار #{row['id']}: {exc}", "error", row["id"])
                        db.set_job_message(row["id"], f"خطا، {backoff} ثانیه دیگر دوباره: {exc}")
                        await client.close()
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                if not ran:
                    await asyncio.sleep(IDLE_SECONDS)
        finally:
            await client.close()

    @staticmethod
    def _handle(user_id: int, row, result: Attempt) -> None:
        seen = "، ".join(result.seen) if result.seen else "-"
        if result.status == "bought":
            db.set_job_status(row["id"], "done", result.message)
            db.log(user_id, result.message, "success", row["id"])
        elif result.status == "error":
            # The save may still have gone through; stop to avoid buying twice.
            db.set_job_status(row["id"], "stopped", result.message + " — در سایت بررسی کن")
            db.log(user_id, result.message, "error", row["id"])
        else:
            msg = f"{result.message} (موجود در سبد: {seen})"
            if msg != row["message"]:
                db.set_job_message(row["id"], msg)


worker = Worker()
