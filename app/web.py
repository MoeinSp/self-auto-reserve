"""Web panel: friends sign up, store their site login, and queue basket jobs."""
import asyncio
import datetime as dt
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import captcha, config, db, jalali, security
from .text import norm, parse_foods
from .worker import worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["ts"] = lambda t: dt.datetime.fromtimestamp(t).strftime("%m/%d %H:%M:%S")

MEALS = ["", "صبحانه", "ناهار", "شام"]
STATUS_FA = {"active": "در حال پیگیری", "done": "خریداری شد", "stopped": "متوقف", "failed": "ناموفق"}
templates.env.globals.update(MEALS=MEALS, STATUS_FA=STATUS_FA)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.SECRET_KEY:
        raise RuntimeError("SECRET_KEY env var is required (see .env.example)")
    db.init()
    await worker.start()
    yield
    await worker.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware, secret_key=config.SECRET_KEY or "unset", same_site="strict",
    max_age=30 * 24 * 3600, https_only=False,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def _uid(request: Request) -> int | None:
    return request.session.get("uid")


def _back(path: str = "/", flash: str | None = None, request: Request | None = None) -> RedirectResponse:
    if flash and request is not None:
        request.session["flash"] = flash
    return RedirectResponse(path, status_code=303)


def _render(request: Request, name: str, **ctx):
    ctx["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, ctx)


# ------------------------------------------------------------------ auth
@app.get("/login")
def login_page(request: Request):
    return _render(request, "login.html", invite=bool(config.INVITE_CODE))


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with db.tx() as c:
        user = c.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if not user or not security.verify_password(password, user["password_hash"]):
        time.sleep(1)
        return _back("/login", "نام کاربری یا رمز پنل اشتباه است", request)
    request.session.clear()
    request.session["uid"] = user["id"]
    return _back("/")


@app.post("/register")
def register(
    request: Request, username: str = Form(...), password: str = Form(...), invite: str = Form("")
):
    with db.tx() as c:
        first_user = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if not first_user and (not config.INVITE_CODE or invite.strip() != config.INVITE_CODE):
            return _back("/login", "کد دعوت نادرست است", request)
        if len(password) < 8 or not username.strip():
            return _back("/login", "رمز پنل حداقل ۸ کاراکتر باشد", request)
        try:
            cur = c.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username.strip(), security.hash_password(password), time.time()),
            )
        except Exception:
            return _back("/login", "این نام کاربری قبلاً ثبت شده", request)
    request.session.clear()
    request.session["uid"] = cur.lastrowid
    return _back("/", "ثبت‌نام انجام شد. حالا اطلاعات ورود سامانهٔ سلف را وارد کن.", request)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return _back("/login")


# ------------------------------------------------------------- dashboard
@app.get("/")
def dashboard(request: Request):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    with db.tx() as c:
        user = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not user:
            request.session.clear()
            return _back("/login")
        account = c.execute("SELECT site_username FROM accounts WHERE user_id = ?", (uid,)).fetchone()
        jobs = c.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
        logs = c.execute("SELECT * FROM logs WHERE user_id = ? ORDER BY id DESC LIMIT 40", (uid,)).fetchall()
    return _render(
        request, "dashboard.html", user=user, account=account, jobs=jobs, logs=logs,
        today=jalali.today_str(), captcha_pending=captcha.manual.pending(uid),
        poll_seconds=config.POLL_SECONDS,
    )


@app.post("/account")
def save_account(request: Request, site_username: str = Form(...), site_password: str = Form(...)):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    with db.tx() as c:
        c.execute(
            "INSERT INTO accounts (user_id, site_username, site_password_enc) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET site_username = excluded.site_username, "
            "site_password_enc = excluded.site_password_enc",
            (uid, norm(site_username), security.encrypt(site_password)),
        )
    task = worker.tasks.pop(uid, None)  # restart with the new credentials
    if task:
        task.cancel()
    return _back("/", "اطلاعات ورود ذخیره شد (رمزنگاری‌شده).", request)


@app.post("/account/test")
async def test_account(request: Request):
    uid = _uid(request)
    if not uid:
        return _back("/login")

    async def run():
        try:
            db.log(uid, await worker.test_login(uid), "success")
        except Exception as exc:
            db.log(uid, f"تست ورود ناموفق: {exc}", "error")

    # Runs in the background: it may wait for a manual captcha from this page.
    asyncio.create_task(run())
    return _back("/", "تست ورود شروع شد؛ چند ثانیه بعد صفحه را تازه کن.", request)


@app.post("/jobs")
def create_job(
    request: Request,
    date: str = Form(...),
    meal: str = Form(""),
    foods: str = Form(...),
    quantity: int = Form(1),
    active_from: str = Form(""),
    active_to: str = Form(""),
):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    day = jalali.normalize_date(date)
    if not day:
        return _back("/", "تاریخ را به شکل ۱۴۰۵/۰۸/۰۵ وارد کن", request)
    if not parse_foods(foods):
        return _back("/", "حداقل یک غذا وارد کن (یا * برای هر غذایی)", request)
    if meal not in MEALS:
        meal = ""
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM accounts WHERE user_id = ?", (uid,)).fetchone():
            return _back("/", "اول اطلاعات ورود سامانه را ذخیره کن", request)
        now = time.time()
        c.execute(
            "INSERT INTO jobs (user_id, date, meal, foods, quantity, active_from, active_to, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, day, meal, foods.strip(), max(1, min(quantity, 5)), active_from, active_to, now, now),
        )
    return _back("/", "کار جدید اضافه شد؛ ربات شروع به پیگیری سبد می‌کند.", request)


@app.post("/jobs/{job_id}/{action}")
def job_action(request: Request, job_id: int, action: str):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    with db.tx() as c:
        if action == "delete":
            c.execute("DELETE FROM jobs WHERE id = ? AND user_id = ? AND status != 'active'", (job_id, uid))
        elif action in ("stop", "resume"):
            status = "stopped" if action == "stop" else "active"
            c.execute(
                "UPDATE jobs SET status = ?, message = '', updated_at = ? WHERE id = ? AND user_id = ?",
                (status, time.time(), job_id, uid),
            )
    return _back("/")


# --------------------------------------------------------------- captcha
@app.get("/captcha.png")
def captcha_image(request: Request):
    png = captcha.manual.images.get(_uid(request) or 0)
    if not png:
        return Response(status_code=404)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/captcha")
def captcha_answer(request: Request, code: str = Form(...)):
    uid = _uid(request)
    if uid and captcha.manual.answer(uid, code):
        return _back("/", "کد ارسال شد.", request)
    return _back("/", "کپچایی در انتظار نبود.", request)
