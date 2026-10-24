"""Web panel: friends sign up, store their site logins, and queue basket jobs."""

import json
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
templates.env.filters["ts"] = jalali.format_ts

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
        if config.INVITE_CODE and not first_user and invite.strip() != config.INVITE_CODE:
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
        accounts = c.execute(
            "SELECT id, site_username, display_name, created_at FROM accounts WHERE user_id = ? ORDER BY id",
            (uid,),
        ).fetchall()
        active_account, active_id = _get_active_account(request, accounts)
        jobs = c.execute(
            "SELECT j.*, a.display_name AS account_name, a.site_username "
            "FROM jobs j LEFT JOIN accounts a ON a.id = j.account_id "
            "WHERE j.user_id = ? ORDER BY j.id DESC LIMIT 50", (uid,)
        ).fetchall()
        logs = c.execute("SELECT * FROM logs WHERE user_id = ? ORDER BY id DESC LIMIT 40", (uid,)).fetchall()
        target_aid = active_account["id"] if active_account else 0
        menu_row = c.execute(
            "SELECT * FROM menus WHERE user_id = ? AND account_id = ? AND date >= ? ORDER BY fetched_at DESC LIMIT 1",
            (uid, target_aid, jalali.today_str()),
        ).fetchone()
        if not menu_row and active_account:
            # Fallback to any recent menu for this user
            menu_row = c.execute(
                "SELECT * FROM menus WHERE user_id = ? AND date >= ? ORDER BY fetched_at DESC LIMIT 1",
                (uid, jalali.today_str()),
            ).fetchone()
    menu = json.loads(menu_row["data"]) if menu_row else None
    return _render(
        request, "dashboard.html", user=user, accounts=accounts,
        active_account=active_account, active_id=active_id,
        jobs=jobs, logs=logs,
        today=jalali.today_str(), captcha_pending=captcha.manual.pending(uid),
        poll_seconds=config.POLL_SECONDS, max_per_hour=config.MAX_REQUESTS_PER_HOUR,
        op=worker.ops.get(uid), now=time.time(),
        menu=menu, menu_date=menu_row["date"] if menu_row else None,
        menu_at=menu_row["fetched_at"] if menu_row else None,
    )


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

    request.session["active_account_id"] = saved_id
    task = worker.tasks.pop(saved_id, None)
    if task:
        task.cancel()
    return _back(f"/?account={saved_id}", flash, request)


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
    return _back(f"/?account={account_id}")


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


@app.post("/menu")
async def fetch_menu(
    request: Request,
    date: str = Form(...),
    meal: str = Form(""),
    account_id: int = Form(0),
):
    uid = _uid(request)
    if not uid:
        return _back("/login")
    day = jalali.normalize_date(date)
    if not day:
        return _back("/", "تاریخ را به شکل ۱۴۰۵/۰۸/۰۵ وارد کن", request)

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

    if not worker.fetch_menu(uid, account_id, day, meal if meal in MEALS else ""):
        return _back(f"/?account={account_id}", "یک عملیات دیگر در حال انجام است؛ صبر کن تمام شود.", request)
    return _back(f"/?account={account_id}")


def _picked_foods(uid: int, menu_date: str, picks: list[str], account_id: int = 0) -> dict[str, list[str]]:
    """Map ticked "meal_index:food_index" boxes back to {meal: [food, ...]}."""
    with db.tx() as c:
        row = None
        if account_id:
            row = c.execute(
                "SELECT data FROM menus WHERE user_id = ? AND account_id = ? AND date = ? ORDER BY fetched_at DESC LIMIT 1",
                (uid, account_id, menu_date),
            ).fetchone()
        if not row:
            row = c.execute(
                "SELECT data FROM menus WHERE user_id = ? AND date = ? ORDER BY fetched_at DESC LIMIT 1",
                (uid, menu_date),
            ).fetchone()
    if not row:
        return {}
    meals = list(json.loads(row["data"]).items())
    chosen: dict[str, list[str]] = {}
    for pick in picks:
        try:
            m, f = (int(x) for x in pick.split(":"))
            meal, info = meals[m]
            chosen.setdefault(meal, []).append(info["foods"][f])
        except (ValueError, IndexError):
            continue
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
    skip_if_reserved: str = Form(""),
    menu_date: str = Form(""),
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

    if picks and menu_date:
        day = menu_date
        wanted = [(m, "\n".join(f)) for m, f in _picked_foods(uid, menu_date, picks, account_id=account_id).items()]
    else:
        day = jalali.normalize_date(date)
        wanted = [(meal if meal in MEALS else "", foods.strip())] if parse_foods(foods) else []
    if not day:
        return _back(f"/?account={account_id}", "تاریخ را به شکل ۱۴۰۵/۰۸/۰۵ وارد کن", request)
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
                 _poll_seconds(poll_seconds), 1 if skip_if_reserved else 0, now, now),
            )
    label = f"کار جدید برای «{acc_name}» اضافه شد" if len(wanted) == 1 else f"{len(wanted)} کار جدید برای «{acc_name}» اضافه شد"
    return _back(f"/?account={account_id}", f"{label}؛ ربات شروع به پیگیری سبد می‌کند.", request)


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
    )


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
        return _back("/admin", f"محدودیت (Rate Limit) برای «{name}» صفر شد.", request)
    else:
        count = worker.reset_throttle()
        with db.tx() as c:
            c.execute("UPDATE jobs SET message = '' WHERE message LIKE '%سقف%'")
        return _back("/admin", f"محدودیت (Rate Limit) برای تمام {count} حساب صفر شد.", request)


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
    return _back("/admin", f"نقش کاربر «{u['username']}» به {role_label} تغییر یافت.", request)


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
    return _back("/admin", f"کاربر «{u['username']}» و تمام اطلاعات مربوطه حذف شدند.", request)


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
    return _back("/admin")
