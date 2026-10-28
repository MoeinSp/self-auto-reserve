"""Web panel: friends sign up, store their site logins, and queue basket jobs."""

import html
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import captcha, config, db, jalali, limits, security
from .text import norm, parse_foods
from .cullinan import LoginFailed
from .ratelimit import RateLimited
from .worker import PAYMENT_SESSIONS, worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["ts"] = jalali.format_ts
templates.env.filters["num"] = lambda x: f"{float(x or 0):g}"  # 20.0 -> 20, 1.5 -> 1.5

MEALS = ["", "صبحانه", "ناهار", "شام"]
STATUS_FA = {"active": "در حال پیگیری", "done": "خریداری شد", "stopped": "متوقف", "failed": "ناموفق"}
templates.env.globals.update(MEALS=MEALS, STATUS_FA=STATUS_FA)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.SECRET_KEY:
        raise RuntimeError("SECRET_KEY env var is required (see .env.example)")
    db.init()
    limits.load()
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
    uid = request.session.get("uid")
    if not uid:
        return None
    with db.tx() as c:
        row = c.execute("SELECT password_hash FROM users WHERE id = ?", (uid,)).fetchone()
    if not row or row["password_hash"] == db.LOCKED:  # deleted or locked user: drop the old cookie
        request.session.clear()
        return None
    return uid


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


SITE_USER_SUFFIX = "@self"  # panel users created by signing in with a site account: "<student number>@self"
SITE_ONLY = "site-login"  # their password_hash: no panel password, they sign in with the site's


@app.post("/login/site")
async def login_with_site(
    request: Request, site_username: str = Form(...), site_password: str = Form(...), invite: str = Form("")
):
    """Sign in with the university account only: the panel user and site account are made on first use."""
    student = norm(site_username).strip()
    if not student.isdigit() or not site_password:
        return _back("/login", "شماره دانشجویی و رمز سامانهٔ سلف را وارد کن", request)
    panel_name = student + SITE_USER_SUFFIX
    with db.tx() as c:
        user = c.execute("SELECT * FROM users WHERE username = ?", (panel_name,)).fetchone()
        first_user = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    if user and user["password_hash"] == db.LOCKED:
        return _back("/login", "این حساب در پنل قفل شده است", request)
    if not user and config.INVITE_CODE and not first_user and invite.strip() != config.INVITE_CODE:
        return _back("/login", "برای اولین ورود، کد دعوت لازم است", request)
    try:
        profile = await worker.verify_site_login(student, site_password)
    except LoginFailed:
        return _back("/login", "شماره دانشجویی یا رمز سامانهٔ سلف اشتباه است (یا کپچا خوانده نشد؛ دوباره امتحان کن)", request)
    except RateLimited as exc:
        return _back("/login", f"تلاش زیادی برای این شماره دانشجویی شده؛ حدود {int(exc.retry_after // 60) + 1} دقیقه دیگر امتحان کن", request)
    except Exception:
        logging.getLogger(__name__).exception("site sign-in failed for %s", student)
        return _back("/login", "سایت دانشگاه جواب نداد؛ کمی بعد دوباره امتحان کن", request)
    now = time.time()
    with db.tx() as c:
        if user:
            uid = user["id"]
        else:
            uid = c.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                (panel_name, SITE_ONLY, 1 if first_user else 0, now),
            ).lastrowid
        acc = c.execute("SELECT id FROM accounts WHERE user_id = ? AND site_username = ?", (uid, student)).fetchone()
        enc = security.encrypt(site_password)
        if acc:
            account_id = acc["id"]
            c.execute("UPDATE accounts SET site_password_enc = ? WHERE id = ?", (enc, account_id))
        else:
            account_id = c.execute(
                "INSERT INTO accounts (user_id, site_username, site_password_enc, display_name, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, student, enc, profile.get("name") or "", now),
            ).lastrowid
        if profile.get("name") or profile.get("credit"):
            c.execute(
                "UPDATE accounts SET display_name = COALESCE(NULLIF(?, ''), display_name), credit = ?, credit_updated_at = ? WHERE id = ?",
                (profile.get("name") or "", profile.get("credit", ""), now, account_id),
            )
    db.set_login_state(account_id, True)
    request.session.clear()
    request.session["uid"] = uid
    request.session["active_account_id"] = account_id
    # The dashboard reads this week's reservations right away (first visit after a login).
    with db.tx() as c:
        c.execute("UPDATE accounts SET reservations_at = 0 WHERE id = ?", (account_id,))
    return _back(f"/?account={account_id}#reserves", f"خوش آمدی{' ' + profile['name'] if profile.get('name') else ''}! رزروهای هفته در حال بررسی است.", request)


GUEST_PREFIX = "guest-"  # panel users made by «ورود بدون ثبت‌نام»; no password, kept by the session cookie
GUEST = "guest"  # their password_hash
GUESTS_PER_HOUR = 30
_guest_times: list[float] = []


@app.post("/login/guest")
def login_as_guest(request: Request, invite: str = Form("")):
    """Enter the panel without any details; the site account is added inside, as usual."""
    if _uid(request):
        return _back("/")
    if config.INVITE_CODE and invite.strip() != config.INVITE_CODE:
        return _back("/login", "برای ورود، کد دعوت لازم است", request)
    now = time.time()
    _guest_times[:] = [t for t in _guest_times if now - t < 3600]
    if len(_guest_times) >= GUESTS_PER_HOUR:
        return _back("/login", "ورودهای بدون ثبت‌نام زیاد شده؛ کمی بعد دوباره امتحان کن", request)
    _guest_times.append(now)
    with db.tx() as c:
        uid = c.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 0, ?)",
            (GUEST_PREFIX + secrets.token_hex(4), GUEST, now),
        ).lastrowid
    request.session.clear()
    request.session["uid"] = uid
    return _back("/#account", "خوش آمدی! شماره دانشجویی و رمز سامانهٔ سلف را وارد کن.", request)


def _claim_guest(c, uid: int, student: str) -> None:
    """A guest's first site account names the user "<student>@self", so «ورود با سلف» finds it again later."""
    user = c.execute("SELECT username, password_hash FROM users WHERE id = ?", (uid,)).fetchone()
    if not user or user["password_hash"] != GUEST or not student.isdigit():
        return
    if c.execute("SELECT 1 FROM users WHERE username = ?", (student + SITE_USER_SUFFIX,)).fetchone():
        return
    c.execute("UPDATE users SET username = ?, password_hash = ? WHERE id = ?", (student + SITE_USER_SUFFIX, SITE_ONLY, uid))


@app.post("/register")
def register(
    request: Request, username: str = Form(...), password: str = Form(...), invite: str = Form("")
):
    with db.tx() as c:
        first_user = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if config.INVITE_CODE and not first_user and invite.strip() != config.INVITE_CODE:
            return _back("/login", "کد دعوت نادرست است", request)
        if len(password) < 8 or not username.strip():
            return _back("/login", "رمز پنل حداقل ۸ کاراکتر باشد", request)
        try:
            cur = c.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                (username.strip(), security.hash_password(password), 1 if first_user else 0, time.time()),
            )
        except Exception:
            return _back("/login", "این نام کاربری قبلاً ثبت شده", request)
    request.session.clear()
    request.session["uid"] = cur.lastrowid
    return _back("/#account", "ثبت‌نام انجام شد. حالا اطلاعات ورود سامانهٔ سلف را وارد کن.", request)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return _back("/login")


# ------------------------------------------------------------- accounts helper
def _get_active_account(request: Request, accounts: list):
    acc_param = request.query_params.get("account")
    if acc_param == "new":
        return None, "new"
    if acc_param and acc_param.isdigit():
        target_id = int(acc_param)
        for acc in accounts:
            if acc["id"] == target_id:
                request.session["active_account_id"] = target_id
                return acc, target_id
    sess_id = request.session.get("active_account_id")
    if sess_id:
        for acc in accounts:
            if acc["id"] == sess_id:
                return acc, sess_id
    if accounts:
        request.session["active_account_id"] = accounts[0]["id"]
        return accounts[0], accounts[0]["id"]
    return None, None


def account_status(acc) -> dict:
    """Connection state of one site account, for the panel."""
    op = worker.ops.get(acc["user_id"])
    task = worker.tasks.get(acc["id"])
    if op and op.get("state") == "running" and op.get("account_id") == acc["id"]:
        status, text = "checking", f"در حال انجام: {op.get('title', 'عملیات')}"
    elif acc["login_state"] == "error":
        status, text = "expired", acc["login_message"] or "ورود ناموفق بود؛ رمز را بررسی کن"
    elif task is not None and not task.done():
        status, text = "online", "متصل · ربات در حال پیگیری سبد"
    elif acc["login_state"] == "ok":
        status, text = "online", f"آخرین ورود موفق: {jalali.format_ts(acc['login_at'])}"
    else:
        status, text = "unknown", "هنوز وارد سامانه نشده؛ «تست ورود» را بزن"
    return {"status": status, "text": text, "throttle": worker.get_throttle_stats(acc["id"])}


# ------------------------------------------------------------- dashboard
@app.get("/")
async def dashboard(request: Request):  # async: it may start a background op
    uid = _uid(request)
    if not uid:
        return _back("/login")
    with db.tx() as c:
        user = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not user:
            request.session.clear()
            return _back("/login")
        accounts = c.execute(
            "SELECT * FROM accounts WHERE user_id = ? ORDER BY id",
            (uid,),
        ).fetchall()
        active_account, active_id = _get_active_account(request, accounts)
        jobs = c.execute(
            "SELECT j.*, a.display_name AS account_name, a.site_username "
            "FROM jobs j LEFT JOIN accounts a ON a.id = j.account_id "
            "WHERE j.user_id = ? ORDER BY j.id DESC LIMIT 50", (uid,)
        ).fetchall()
        logs = c.execute("SELECT * FROM logs WHERE user_id = ? ORDER BY id DESC LIMIT 40", (uid,)).fetchall()
        menu_at = c.execute("SELECT MAX(fetched_at) FROM week_menu").fetchone()[0]
    statuses = {acc["id"]: account_status(acc) for acc in accounts}
    has_acc = bool(active_account) and active_id != "new"
    res_days = jalali.two_weeks_days()
    reserved_all = db.reservations(active_account["id"], res_days[0]["date"]) if has_acc else {}
    sales = db.sales(active_account["id"], res_days[0]["date"]) if has_acc else {}
    # Read the account's reservations right after its first successful login, once.
    if has_acc and not active_account["reservations_at"] and active_account["login_state"] == "ok" and not worker.busy(uid):
        worker.check_reservations(uid, active_account["id"])
    all_menu = db.week_menu(res_days[0]["date"])
    return _render(
        request, "dashboard.html", user=user, accounts=accounts,
        active_account=active_account, active_id=active_id,
        jobs=jobs, logs=logs,
        today=jalali.today_str(), captcha_pending=captcha.manual.pending(uid),
        poll_seconds=config.POLL_SECONDS, max_per_hour=config.MAX_REQUESTS_PER_HOUR,
        min_job_interval=config.MIN_JOB_INTERVAL, statuses=statuses,
        op=worker.ops.get(uid), now=time.time(),
        week_menu=db.week_menu(jalali.today_str()), menu_at=menu_at,
        res_days=res_days, reserved_all=reserved_all, all_menu=all_menu, sales=sales,
        reserved={d: m for d, m in reserved_all.items() if d >= jalali.today_str()},
        known_foods=_known_foods(all_menu),
        menu_refreshing=worker.menu_refreshing,
        upcoming_days=jalali.get_upcoming_days(14),
        next_week_days=jalali.get_next_week_days(),
        jalali=jalali,
    )


def _known_foods(menu: dict[str, dict[str, list[str]]]) -> dict[str, list[str]]:
    """Distinct main dishes per meal ("چلو جوجه کباب,لیمو ترش" -> "چلو جوجه کباب"), for priority suggestions."""
    out: dict[str, list[str]] = {}
    for meals in menu.values():
        for meal, foods in meals.items():
            for food in foods:
                name = food.split(",")[0].strip()
                if name and name not in out.setdefault(meal, []):
                    out[meal].append(name)
    return out


@app.post("/account")
def save_account(
    request: Request,
    site_username: str = Form(...),
    site_password: str = Form(""),
    display_name: str = Form(""),
    account_id: int = Form(0),
):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = int(account_id) if str(account_id).isdigit() else 0
    username = norm(site_username).strip()
    if not username:
        return _back("/", "شماره دانشجویی را وارد کن.", request)

    with db.tx() as c:
        if account_id > 0:
            acc = c.execute("SELECT * FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
            if not acc:
                return _back("/", "حساب پیدا نشد.", request)
            new_name = display_name.strip() or acc["display_name"]
            if site_password.strip():
                c.execute(
                    "UPDATE accounts SET site_username = ?, site_password_enc = ?, display_name = ? WHERE id = ?",
                    (username, security.encrypt(site_password.strip()), new_name, account_id)
                )
                flash = "اطلاعات حساب و رمز جدید ذخیره شد."
            else:
                c.execute(
                    "UPDATE accounts SET site_username = ?, display_name = ? WHERE id = ?",
                    (username, new_name, account_id)
                )
                flash = "اطلاعات حساب ذخیره شد (رمز قبلی حفظ شد)."
            saved_id = account_id
        else:
            if not site_password.strip():
                return _back("/?account=new", "برای حساب جدید، رمز سامانهٔ سلف الزامی است.", request)
            existing = c.execute(
                "SELECT id, display_name FROM accounts WHERE user_id = ? AND site_username = ?",
                (uid, username)
            ).fetchone()
            if existing:
                c.execute(
                    "UPDATE accounts SET site_password_enc = ?, display_name = ? WHERE id = ?",
                    (security.encrypt(site_password.strip()), display_name.strip() or existing["display_name"], existing["id"])
                )
                saved_id = existing["id"]
                flash = "رمز این حساب بروزرسانی شد."
            else:
                cur = c.execute(
                    "INSERT INTO accounts (user_id, site_username, site_password_enc, display_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (uid, username, security.encrypt(site_password.strip()), display_name.strip(), time.time())
                )
                saved_id = cur.lastrowid
                flash = "حساب جدید اضافه شد. با زدن «تست ورود»، نام دانشجو خودکار دریافت و ثبت می‌شود."
            _claim_guest(c, uid, username)

    request.session["active_account_id"] = saved_id
    task = worker.tasks.pop(saved_id, None)
    if task:
        task.cancel()
    return _back(f"/?account={saved_id}#account", flash, request)


@app.post("/account/test")
async def test_account(
    request: Request,
    account_id: int = Form(0),
    site_username: str = Form(""),
    site_password: str = Form(""),
):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = int(account_id) if str(account_id).isdigit() else 0
    with db.tx() as c:
        if account_id > 0:
            acc = c.execute("SELECT * FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
        else:
            acc = c.execute("SELECT * FROM accounts WHERE user_id = ? ORDER BY id LIMIT 1", (uid,)).fetchone()
            if acc:
                account_id = acc["id"]
    if not acc:
        return _back("/", "اول اطلاعات ورود یک حساب سلف را ذخیره کن.", request)

    if site_username.strip() or site_password.strip():
        with db.tx() as c:
            new_u = norm(site_username).strip() or acc["site_username"]
            if site_password.strip():
                c.execute(
                    "UPDATE accounts SET site_username = ?, site_password_enc = ? WHERE id = ?",
                    (new_u, security.encrypt(site_password.strip()), account_id)
                )
            else:
                c.execute("UPDATE accounts SET site_username = ? WHERE id = ?", (new_u, account_id))

    request.session["active_account_id"] = account_id
    if not worker.test_login(uid, account_id):
        return _back(f"/?account={account_id}", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}#account")


def _own_account(uid: int, account_id) -> int:
    account_id = int(account_id) if str(account_id).isdigit() else 0
    with db.tx() as c:
        acc = c.execute("SELECT id FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
    return acc["id"] if acc else 0


@app.post("/account/reservations")
async def check_reservations(request: Request, account_id: int = Form(...)):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = _own_account(uid, account_id)
    if not account_id:
        return _back("/", "حساب پیدا نشد.", request)
    if not worker.check_reservations(uid, account_id):
        return _back(f"/?account={account_id}#reserves", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}#reserves")


def _clean_list(raw: str) -> str:
    """One food per line, blank lines dropped, at most 30."""
    return "\n".join(parse_foods(raw)[:30])


@app.post("/account/priorities")
def save_priorities(
    request: Request,
    account_id: int = Form(...),
    priority_breakfast: str = Form(""),
    priority_lunch: str = Form(""),
    priority_dinner: str = Form(""),
    milk_pref: str = Form(""),
):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = _own_account(uid, account_id)
    if not account_id:
        return _back("/", "حساب پیدا نشد.", request)
    with db.tx() as c:
        c.execute(
            "UPDATE accounts SET priority_breakfast = ?, priority_lunch = ?, priority_dinner = ?, milk_pref = ? WHERE id = ?",
            (_clean_list(priority_breakfast), _clean_list(priority_lunch), _clean_list(priority_dinner),
             milk_pref if milk_pref in ("plain", "flavored") else "", account_id),
        )
    return _back(f"/?account={account_id}#reserves", "اولویت غذاها ذخیره شد؛ رزرو خودکار از این ترتیب استفاده می‌کند.", request)


@app.post("/account/delete")
def delete_account(request: Request, account_id: int = Form(...)):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    with db.tx() as c:
        acc = c.execute(
            "SELECT display_name, site_username FROM accounts WHERE id = ? AND user_id = ?",
            (account_id, uid)
        ).fetchone()
        if not acc:
            return _back("/", "حساب پیدا نشد.", request)
        name = acc["display_name"] or acc["site_username"]
        c.execute(
            "UPDATE jobs SET status = 'stopped', message = 'حساب سلف حذف شد' WHERE account_id = ? AND status = 'active'",
            (account_id,)
        )
        c.execute("DELETE FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid))
    task = worker.tasks.pop(account_id, None)
    if task:
        task.cancel()
    request.session.pop("active_account_id", None)
    return _back("/", f"حساب «{name}» حذف شد.", request)


@app.post("/account/sync-credit")
async def sync_credit(request: Request, account_id: int = Form(...)):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = int(account_id) if str(account_id).isdigit() else 0
    with db.tx() as c:
        acc = c.execute("SELECT id FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
    if not acc:
        return _back("/", "حساب پیدا نشد.", request)
    request.session["active_account_id"] = account_id
    if not worker.sync_account_profile(uid, account_id):
        return _back(f"/?account={account_id}", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}#account")


@app.post("/account/auto-reserve")
def save_auto_reserve(
    request: Request,
    account_id: int = Form(...),
    auto_reserve: str = Form(""),
    meals: list[str] = Form([]),
):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = int(account_id) if str(account_id).isdigit() else 0
    is_active = 1 if auto_reserve in ("1", "true", "on") else 0
    valid_meals = [m for m in meals if m in MEALS]
    meals_str = ",".join(valid_meals) if valid_meals else "ناهار,شام"
    with db.tx() as c:
        acc = c.execute("SELECT id, display_name FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
        if not acc:
            return _back("/", "حساب پیدا نشد.", request)
        c.execute(
            "UPDATE accounts SET auto_reserve = ?, auto_reserve_meals = ? WHERE id = ?",
            (is_active, meals_str, account_id),
        )
    status_str = "فعال شد (چهارشنبه‌ها برای هفته بعد)" if is_active else "غیرفعال شد"
    return _back(f"/?account={account_id}#tools", f"تنظیمات رزرو خودکار هفتگی {status_str}.", request)


@app.post("/account/auto-reserve/run-now")
async def run_auto_reserve_now(request: Request, account_id: int = Form(...)):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = int(account_id) if str(account_id).isdigit() else 0
    with db.tx() as c:
        acc = c.execute("SELECT id FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
    if not acc:
        return _back("/", "حساب پیدا نشد.", request)
    request.session["active_account_id"] = account_id
    if not worker.run_auto_reserve(uid, account_id):
        return _back(f"/?account={account_id}", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}#tools")


@app.post("/forgot-receipt")
async def get_forgot_receipt_endpoint(
    request: Request,
    account_id: int = Form(...),
    date: str = Form(...),
    meal: str = Form(""),
    reserve_type: str = Form("0"),
):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = int(account_id) if str(account_id).isdigit() else 0
    day = jalali.normalize_date(date)
    if not day:
        return _back(f"/?account={account_id}", "تاریخ را انتخاب کن.", request)
    with db.tx() as c:
        acc = c.execute("SELECT id FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
    if not acc:
        return _back("/", "حساب پیدا نشد.", request)
    request.session["active_account_id"] = account_id
    if not worker.fetch_forgot_receipt(uid, account_id, day, meal if meal in MEALS else "", str(reserve_type)):
        return _back(f"/?account={account_id}", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}#tools")


@app.get("/api/account/health")
def account_health(request: Request, account_id: int = 0):
    uid = _uid(request)
    if not uid:
        return {"status": "unknown", "status_text": "وارد پنل نشده‌اید"}
    with db.tx() as c:
        acc = c.execute("SELECT * FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
    if not acc:
        return {"status": "unknown", "status_text": "حسابی انتخاب نشده"}
    st = account_status(acc)
    return {
        "status": st["status"],
        "status_text": st["text"],
        "credit": acc["credit"],
        "credit_updated_at": jalali.format_ts(acc["credit_updated_at"]) if acc["credit_updated_at"] else "",
        "display_name": acc["display_name"] or acc["site_username"],
        "requests_hour": st["throttle"]["hits"],
        "max_requests_hour": st["throttle"]["max_hits"],
    }


@app.post("/account/pay")
async def request_pay(
    request: Request,
    account_id: int = Form(...),
    amount: str = Form(...),
    unit: str = Form("toman"),
    bank: int = Form(0),
):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = int(account_id) if str(account_id).isdigit() else 0
    clean_amt = re.sub(r"[^\d]", "", norm(amount))
    if not clean_amt or int(clean_amt) <= 0:
        return _back(f"/?account={account_id}", "مبلغ افزایش اعتبار را به درستی وارد کن.", request)
    amt_int = int(clean_amt)
    amount_rials = amt_int * 10 if unit == "toman" else amt_int
    if amount_rials < 10000:
        return _back(f"/?account={account_id}", "حداقل مبلغ پرداخت ۱۰,۰۰۰ ریال است.", request)

    with db.tx() as c:
        acc = c.execute("SELECT id FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
    if not acc:
        return _back("/", "حساب پیدا نشد.", request)

    request.session["active_account_id"] = account_id
    if not worker.request_payment_gateway(uid, account_id, amount_rials, int(bank)):
        return _back(f"/?account={account_id}", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}#account")


@app.get("/pay/gateway/{token}", response_class=HTMLResponse)
def gateway_redirect(token: str):
    sess = PAYMENT_SESSIONS.get(token) or getattr(worker, "PAYMENT_SESSIONS", {}).get(token)
    if not sess:
        with db.tx() as c:
            row = c.execute("SELECT * FROM payment_sessions WHERE token = ?", (token,)).fetchone()
            if row:
                sess = {
                    "action": row["action"],
                    "method": row["method"],
                    "fields": json.loads(row["fields"]) if row["fields"] else {},
                    "bank": row["bank"],
                    "amount_rials": row["amount_rials"],
                    "created_at": row["created_at"],
                }
    if not sess or (time.time() - sess.get("created_at", 0) > 1800):
        return HTMLResponse(
            """<!DOCTYPE html><html dir="rtl" lang="fa"><head><meta charset="utf-8">
            <style>body{font-family:sans-serif;text-align:center;padding:50px;background:#f7f3ec;color:#1d2125}</style>
            </head><body>
            <h2>لینک پرداخت منقضی شده یا نامعتبر است</h2>
            <p>لطفاً دوباره از پنل درخواست درگاه را ثبت کنید.</p>
            <a href="/" style="display:inline-block;padding:10px 20px;background:#d98a00;color:#fff;text-decoration:none;border-radius:8px">بازگشت به پنل</a>
            </body></html>""",
            status_code=404,
        )

    action = sess["action"]
    if not str(action).startswith(("https://", "http://")):
        return HTMLResponse("آدرس درگاه نامعتبر است", status_code=400)
    fields = sess.get("fields", {})
    bank = html.escape(str(sess.get("bank", "درگاه شاپرک")))
    amt_str = f"{sess.get('amount_rials', 0):,} ریال"

    # If gateway expects GET (like Sadad/Melli) or there are no POST form fields, redirect directly!
    if sess.get("method") == "GET" or not fields:
        return RedirectResponse(action, status_code=303)

    inputs_html = "\n".join(
        f'<input type="hidden" name="{html.escape(str(k), quote=True)}" value="{html.escape(str(v), quote=True)}">'
        for k, v in fields.items()
    )
    return HTMLResponse(f"""<!DOCTYPE html>
<html dir="rtl" lang="fa">
<head>
  <meta charset="utf-8">
  <title>انتقال امن به {bank}...</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ font-family: Tahoma, sans-serif; text-align: center; padding: 40px 16px; background: #f7f3ec; color: #1d2125; }}
    .box {{ max-width: 440px; margin: 40px auto; background: #fff; padding: 30px 24px; border-radius: 16px; box-shadow: 0 4px 20px rgba(0,0,0,.08); }}
    .spinner {{ width: 40px; height: 40px; border: 4px solid #e7dfd2; border-top-color: #10b981; border-radius: 50%; animation: spin 1s linear infinite; margin: 20px auto; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .btn {{ display: inline-block; padding: 12px 24px; background: #10b981; color: #fff; text-decoration: none; border-radius: 10px; font-weight: bold; border: none; cursor: pointer; font-family: inherit; }}
  </style>
</head>
<body onload="setTimeout(function(){{ document.getElementById('payForm').submit(); }}, 500)">
  <div class="box">
    <div class="spinner"></div>
    <h3>در حال اتصال امن به {bank} (شاپرک)</h3>
    <p>مبلغ: <strong>{amt_str}</strong></p>
    <p style="font-size:13px;color:#6b6f76">تا چند لحظه دیگر به درگاه پرداخت بانکی منتقل می‌شوید...</p>
    <form id="payForm" method="POST" action="{html.escape(action, quote=True)}">
      {inputs_html}
      <noscript><button type="submit" class="btn">ورود به درگاه بانکی</button></noscript>
    </form>
    <button type="button" class="btn" style="margin-top:10px" onclick="document.getElementById('payForm').submit()">اگر منتقل نشدید، اینجا کلیک کنید</button>
  </div>
</body>
</html>""")


@app.post("/week-menu")
async def refresh_week_menu(request: Request, account_id: int = Form(0)):
    """Read the whole week's foods once; the result is shared by every user."""
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = int(account_id) if str(account_id).isdigit() else 0
    with db.tx() as c:
        if account_id > 0:
            acc = c.execute("SELECT id FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
        else:
            acc = c.execute("SELECT id FROM accounts WHERE user_id = ? ORDER BY id LIMIT 1", (uid,)).fetchone()
            if acc:
                account_id = acc["id"]
    if not acc:
        return _back("/", "اول اطلاعات ورود سامانه را ذخیره کن", request)

    if worker.menu_refreshing:
        return _back(f"/?account={account_id}", "غذاهای هفته همین الان در حال بروزرسانی است؛ چند لحظه صبر کن.", request)
    if not worker.refresh_week_menu(uid, account_id):
        return _back(f"/?account={account_id}", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}#buy")


def _picked_foods(day: str, picks: list[str]) -> dict[str, list[str]]:
    """Map ticked "meal|food" boxes to {meal: [food, ...]}, keeping only foods on the shared menu of `day`."""
    menu = db.week_menu(day).get(day, {})
    chosen: dict[str, list[str]] = {}
    for pick in picks:
        meal, _, food = pick.partition("|")
        if food in menu.get(meal, []) and food not in chosen.get(meal, []):
            chosen.setdefault(meal, []).append(food)
    return chosen


@app.post("/jobs")
def create_job(
    request: Request,
    date: str = Form(...),
    meal: str = Form(""),
    foods: str = Form(""),
    quantity: int = Form(1),
    active_from: str = Form(""),
    active_to: str = Form(""),
    poll_seconds: float = Form(0),
    picks: list[str] = Form([]),
    account_id: int = Form(0),
):
    uid = _uid(request)
    if not uid:
        return _back("/login")

    account_id = int(account_id) if str(account_id).isdigit() else 0
    with db.tx() as c:
        if account_id > 0:
            acc = c.execute("SELECT id, display_name, site_username FROM accounts WHERE id = ? AND user_id = ?", (account_id, uid)).fetchone()
        else:
            acc = c.execute("SELECT id, display_name, site_username FROM accounts WHERE user_id = ? ORDER BY id LIMIT 1", (uid,)).fetchone()
            if acc:
                account_id = acc["id"]
    if not acc:
        return _back("/", "اول اطلاعات ورود سامانه را ذخیره کن", request)

    day = jalali.normalize_date(date)
    wanted = [(m, "\n".join(f)) for m, f in _picked_foods(day, picks).items()] if day and picks else []
    if not wanted and parse_foods(foods):
        wanted = [(meal if meal in MEALS else "", foods.strip())]
    if not day:
        return _back(f"/?account={account_id}", "تاریخ را به شکل ۱۴۰۵/۰۸/۰۵ وارد کن", request)
    # A meal already reserved can't be bought from the basket (the site refuses it).
    have = db.reservations(account_id, day).get(day, {})
    skipped = [m for m, _ in wanted if m in have]
    wanted = [(m, f) for m, f in wanted if m not in have]
    if skipped and not wanted:
        return _back(f"/?account={account_id}", f"برای {'، '.join(skipped)} این روز رزرو داری؛ از سبد نمی‌شود خرید.", request)
    if not wanted:
        return _back(f"/?account={account_id}", "حداقل یک غذا تیک بزن یا بنویس (یا * برای هر غذایی)", request)

    acc_name = acc["display_name"] or acc["site_username"]
    with db.tx() as c:
        now = time.time()
        for job_meal, job_foods in wanted:
            c.execute(
                "INSERT INTO jobs (user_id, account_id, date, meal, foods, quantity, active_from, active_to, poll_seconds, "
                "skip_if_reserved, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, account_id, day, job_meal, job_foods, max(1, min(quantity, 5)), active_from, active_to,
                 _poll_seconds(poll_seconds), 1, now, now),
            )
    label = f"کار جدید برای «{acc_name}» اضافه شد" if len(wanted) == 1 else f"{len(wanted)} کار جدید برای «{acc_name}» اضافه شد"
    return _back(f"/?account={account_id}#jobs", f"{label}؛ ربات شروع به پیگیری سبد می‌کند.", request)


@app.post("/sell")
async def sell_to_basket(  # async: it starts a background op
    request: Request, date: str = Form(...), sells: list[str] = Form([]), account_id: int = Form(0)
):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = _own_account(uid, account_id)
    if not account_id:
        return _back("/", "حساب پیدا نشد.", request)
    day = jalali.normalize_date(date)
    if not day:
        return _back(f"/?account={account_id}#buy", "تاریخ را به شکل ۱۴۰۵/۰۸/۰۵ وارد کن", request)
    # Only meals the panel knows are reserved and not in the basket yet; the site is asked again before selling.
    have = db.reservations(account_id, day).get(day, {})
    listed = db.sales(account_id, day).get(day, {})
    meals = [m for m in MEALS if m and m in sells and m in have and m not in listed]
    if not meals:
        return _back(f"/?account={account_id}#buy", "وعدهٔ رزروشده‌ای برای فروش تیک نزدی.", request)
    if not worker.sell_to_basket(uid, account_id, day, meals):
        return _back(f"/?account={account_id}#buy", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}#buy")


SALE_CHECK_GAP = 60  # seconds between two checks of one sale; checks are manual only


@app.post("/sales/{action}")
async def sale_action(  # async: it starts a background op
    request: Request, action: str, sale: str = Form(...), account_id: int = Form(0)
):
    """«بررسی» or «حذف از سبد» for one meal waiting in the basket (sale = "date|meal")."""
    uid = _uid(request)
    if not uid:
        return _back("/login")
    account_id = _own_account(uid, account_id)
    day, _, meal = sale.partition("|")
    row = db.sales(account_id, day).get(day, {}).get(meal) if account_id and day else None
    if not row or action not in ("check", "cancel"):
        return _back(f"/?account={account_id}#buy", "این وعده در سبد پیدا نشد.", request)
    if action == "check":
        wait = int(row["checked_at"] + SALE_CHECK_GAP - time.time())
        if wait > 0:
            return _back(f"/?account={account_id}#buy", f"همین الان بررسی شد؛ {wait} ثانیهٔ دیگر دوباره بزن.", request)
        started = worker.check_sale(uid, account_id, day, meal)
    else:
        if row["status"] != "listed":
            return _back(f"/?account={account_id}#buy", "این وعده دیگر در سبد نیست.", request)
        started = worker.cancel_sale(uid, account_id, day, meal)
    if not started:
        return _back(f"/?account={account_id}#buy", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}#buy")


def _poll_seconds(value: float) -> float:
    """Per-job check interval; 0 (or the default) means POLL_SECONDS."""
    if not value or value == config.POLL_SECONDS:
        return 0
    return min(max(value, config.MIN_JOB_INTERVAL), 3600)


@app.post("/jobs/{job_id}/{action}")
def job_action(request: Request, job_id: int, action: str, poll_seconds: float = Form(0)):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    with db.tx() as c:
        if action == "speed":
            c.execute(
                "UPDATE jobs SET poll_seconds = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (_poll_seconds(poll_seconds), time.time(), job_id, uid),
            )
        elif action == "delete":
            c.execute("DELETE FROM jobs WHERE id = ? AND user_id = ? AND status != 'active'", (job_id, uid))
        elif action in ("stop", "resume"):
            status = "stopped" if action == "stop" else "active"
            c.execute(
                "UPDATE jobs SET status = ?, message = '', updated_at = ? WHERE id = ? AND user_id = ?",
                (status, time.time(), job_id, uid),
            )
    return _back("/#jobs")


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



# ------------------------------------------------------------------ admin
def _is_admin(request: Request) -> bool:
    uid = _uid(request)
    if not uid:
        return False
    with db.tx() as c:
        user = c.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()
        return bool(user and user["is_admin"])


@app.get("/admin")
def admin_page(request: Request):
    if not _is_admin(request):
        return _back("/", "دسترسی به پنل ادمین مجاز نیست.", request)
    uid = _uid(request)
    with db.tx() as c:
        admin_user = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        users = c.execute("""
            SELECT u.id, u.username, u.is_admin, u.created_at,
                   COUNT(DISTINCT a.id) AS accounts_count,
                   COUNT(DISTINCT j.id) AS jobs_count
            FROM users u
            LEFT JOIN accounts a ON a.user_id = u.id
            LEFT JOIN jobs j ON j.user_id = u.id
            GROUP BY u.id
            ORDER BY u.id
        """).fetchall()

        raw_accounts = c.execute("""
            SELECT a.id, a.user_id, a.site_username, a.display_name, a.created_at,
                   u.username AS owner_username,
                   COUNT(CASE WHEN j.status = 'active' THEN 1 END) AS active_jobs_count
            FROM accounts a
            JOIN users u ON u.id = a.user_id
            LEFT JOIN jobs j ON j.account_id = a.id
            GROUP BY a.id
            ORDER BY a.id
        """).fetchall()

        accounts = []
        for acc in raw_accounts:
            st = worker.get_throttle_stats(acc["id"])
            accounts.append({**dict(acc), "stats": st})

        jobs = c.execute("""
            SELECT j.*, u.username AS owner_username,
                   a.display_name AS account_name, a.site_username
            FROM jobs j
            JOIN users u ON u.id = j.user_id
            LEFT JOIN accounts a ON a.id = j.account_id
            ORDER BY j.id DESC LIMIT 100
        """).fetchall()

        logs = c.execute("""
            SELECT l.*, u.username AS owner_username
            FROM logs l
            JOIN users u ON u.id = l.user_id
            ORDER BY l.id DESC LIMIT 50
        """).fetchall()

        total_users = len(users)
        total_accounts = len(accounts)
        active_jobs = sum(1 for j in jobs if j["status"] == "active")

    return _render(
        request, "admin.html", user=admin_user, users=users,
        accounts=accounts, jobs=jobs, logs=logs,
        total_users=total_users, total_accounts=total_accounts, active_jobs=active_jobs,
        max_requests_hour=config.MAX_REQUESTS_PER_HOUR,
        max_logins_hour=config.MAX_LOGINS_PER_HOUR,
        limits=limits.current(), limit_defaults=limits.DEFAULTS,
    )


@app.post("/admin/settings")
def admin_save_settings(
    request: Request,
    POLL_SECONDS: str = Form(""),
    MIN_JOB_INTERVAL: str = Form(""),
    STEP_SECONDS: str = Form(""),
    POLL_JITTER: str = Form(""),
    MAX_REQUESTS_PER_HOUR: str = Form(""),
    MAX_LOGINS_PER_HOUR: str = Form(""),
    no_request_cap: str = Form(""),
    no_login_cap: str = Form(""),
):
    if not _is_admin(request):
        return _back("/", "دسترسی مجاز نیست.", request)
    values = {
        "POLL_SECONDS": POLL_SECONDS, "MIN_JOB_INTERVAL": MIN_JOB_INTERVAL, "STEP_SECONDS": STEP_SECONDS,
        "POLL_JITTER": POLL_JITTER, "MAX_REQUESTS_PER_HOUR": MAX_REQUESTS_PER_HOUR,
        "MAX_LOGINS_PER_HOUR": MAX_LOGINS_PER_HOUR,
    }
    # Non-str values are FastAPI Form() defaults when called directly (tests).
    values = {k: norm(v).strip() for k, v in values.items() if isinstance(v, str) and norm(v).strip()}
    if "POLL_JITTER" in values:  # entered as a percentage
        try:
            values["POLL_JITTER"] = float(values["POLL_JITTER"]) / 100
        except ValueError:
            pass
    if no_request_cap is True or (isinstance(no_request_cap, str) and no_request_cap):
        values["MAX_REQUESTS_PER_HOUR"] = 0
    if no_login_cap is True or (isinstance(no_login_cap, str) and no_login_cap):
        values["MAX_LOGINS_PER_HOUR"] = 0
    try:
        limits.save(values)
    except ValueError:
        return _back("/admin#settings", "مقدارها باید عدد باشند.", request)
    return _back("/admin#settings", "تنظیمات محدودیت ذخیره شد و همین حالا اعمال شد.", request)


@app.post("/admin/settings/reset")
def admin_reset_settings(request: Request):
    if not _is_admin(request):
        return _back("/", "دسترسی مجاز نیست.", request)
    limits.reset()
    return _back("/admin#settings", "تنظیمات محدودیت به پیش‌فرض برگشت.", request)


@app.post("/admin/rate-limit/reset")
def admin_reset_rate_limit(request: Request, account_id: int = Form(0)):
    if not _is_admin(request):
        return _back("/", "دسترسی مجاز نیست.", request)
    account_id = int(account_id) if str(account_id).isdigit() else 0
    if account_id > 0:
        worker.reset_throttle(account_id)
        with db.tx() as c:
            c.execute(
                "UPDATE jobs SET message = '' WHERE account_id = ? AND message LIKE '%سقف%'",
                (account_id,),
            )
            acc = c.execute("SELECT display_name, site_username FROM accounts WHERE id = ?", (account_id,)).fetchone()
            name = (acc["display_name"] or acc["site_username"]) if acc else f"حساب #{account_id}"
        return _back("/admin#accounts", f"شمارندهٔ درخواست‌های «{name}» صفر شد.", request)
    else:
        count = worker.reset_throttle()
        with db.tx() as c:
            c.execute("UPDATE jobs SET message = '' WHERE message LIKE '%سقف%'")
        return _back("/admin#accounts", f"شمارندهٔ درخواست‌های هر {count} حساب صفر شد.", request)


@app.post("/admin/users/{target_uid}/role")
def admin_toggle_role(request: Request, target_uid: int):
    if not _is_admin(request):
        return _back("/", "دسترسی مجاز نیست.", request)
    current_uid = _uid(request)
    if current_uid == target_uid:
        return _back("/admin", "نمی‌توانید نقش خودتان را تغییر دهید.", request)
    with db.tx() as c:
        u = c.execute("SELECT is_admin, username FROM users WHERE id = ?", (target_uid,)).fetchone()
        if not u:
            return _back("/admin", "کاربر پیدا نشد.", request)
        new_role = 0 if u["is_admin"] else 1
        c.execute("UPDATE users SET is_admin = ? WHERE id = ?", (new_role, target_uid))
        role_label = "مدیر" if new_role else "کاربر عادی"
    return _back("/admin#users", f"نقش کاربر «{u['username']}» به {role_label} تغییر یافت.", request)


@app.post("/admin/users/{target_uid}/delete")
def admin_delete_user(request: Request, target_uid: int):
    if not _is_admin(request):
        return _back("/", "دسترسی مجاز نیست.", request)
    current_uid = _uid(request)
    if current_uid == target_uid:
        return _back("/admin", "نمی‌توانید حساب کاربری خودتان را حذف کنید.", request)
    with db.tx() as c:
        u = c.execute("SELECT username FROM users WHERE id = ?", (target_uid,)).fetchone()
        if not u:
            return _back("/admin", "کاربر پیدا نشد.", request)
        accs = c.execute("SELECT id FROM accounts WHERE user_id = ?", (target_uid,)).fetchall()
        for a in accs:
            t = worker.tasks.pop(a["id"], None)
            if t:
                t.cancel()
        c.execute("DELETE FROM users WHERE id = ?", (target_uid,))
    return _back("/admin#users", f"کاربر «{u['username']}» و تمام اطلاعات مربوطه حذف شدند.", request)


@app.post("/admin/jobs/{job_id}/{action}")
def admin_job_action(request: Request, job_id: int, action: str):
    if not _is_admin(request):
        return _back("/", "دسترسی مجاز نیست.", request)
    with db.tx() as c:
        if action == "delete":
            c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        elif action in ("stop", "resume"):
            status = "stopped" if action == "stop" else "active"
            c.execute("UPDATE jobs SET status = ?, message = '', updated_at = ? WHERE id = ?", (status, time.time(), job_id))
    return _back("/admin#jobs")
