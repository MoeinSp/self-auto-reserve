"""Browser automation for the Cullinan feeding system (ASP.NET WebForms).

A real browser (Playwright) is used instead of raw HTTP because the login
page encrypts the password in JavaScript and every dropdown triggers a
WebForms postback. All selectors live in SEL so they are easy to adjust
after looking at a page dump (see tools/dump_pages.py).
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin

from playwright.async_api import Browser, BrowserContext, Page, Route

from . import config
from .ratelimit import Throttle
from .text import matches, norm

log = logging.getLogger(__name__)

SEL = {
    "username": "#txtUsernamePlain, input[name$='txtUsernamePlain'], input[id*='Username' i]",
    "password": "#txtPasswordPlain, input[name$='txtPasswordPlain'], input[type=password]",
    "captcha_input": "#txtCaptcha, input[id*='captcha' i]",
    "captcha_img": "#Img1, img[id*='captcha' i], img[src*='captcha' i]",
    "login_button": "#btnEncript, input[type=submit][id*='login' i], button[id*='login' i]",
    "login_error": "[id*='lblMessage' i], [id*='lblError' i], .alert, .error, .validation-summary-errors",
    # Food basket page (FoodBasket/FoodBasketBuy.aspx): one row per meal.
    "basket_grid": "[id$='grdFoodBasketReservation']",
    "basket_date": "[id$='txtDate']",
    "basket_go": "[id$='imgbtnGoToDate']",
    "basket_save": "[id$='btnSave'], input[type=submit][value*='ذخیره']",
    "page_message": "[id$='messageBoxValue'], [id$='alertBoxValue'], .toast-message, [id*='lblMessage' i]",
    # Weekly reservation page (Reservation/Reservation.aspx): one grid per meal, one row per day.
    "week_label": "[id$='lblWeekDate']",
    "week_next": "[id$='imgbtnNextWeek']",
    "week_prev": "[id$='imgbtnLastWeek']",
    "profile_name": "#divUsername, [id$='divUsername'], #cphMain_titleBoxValue, [id$='titleBoxValue']",
    "profile_id": "#lblIdNumber, [id$='lblIdNumber']",
    "profile_credit": "#lblCredit, [id$='lblCredit']",
}
RESERVATION_PATH = "Reservation/Reservation.aspx"
WEEK_GRIDS = {"صبحانه": "Breakfast", "ناهار": "Lunch", "شام": "Dinner"}

# Dropdown entries that mean "nothing selected".
PLACEHOLDERS = {"", "-", "--", "0", "انتخاب", "انتخاب کنید", "---"}
LONG_REQUEST = 2.5  # seconds; see _postback
WRONG_PASSWORD_HINTS = ("رمز", "کلمه عبور", "نام کاربری")
ERROR_HINTS = ("خطا", "ناموفق", "کافی نیست", "موجود نیست", "امکان", "مجاز نیست", "error")


class LoginFailed(Exception):
    """Wrong credentials: stop immediately, retrying could lock the account."""


class SiteError(Exception):
    pass


CaptchaSolver = Callable[[bytes, int], Awaitable[str | None]]


@dataclass
class Job:
    id: int
    date: str
    meal: str
    foods: list[str]
    quantity: int = 1


@dataclass
class Attempt:
    status: str  # empty | no_match | bought | error
    message: str
    seen: list[str] = field(default_factory=list)


# Reads the rows of a reservation grid into plain data. Both pages use the same
# row layout, with control ids generated from fixed names: chkReserve (the
# "reserve" box), drpSelf, drpFood, drpFoodCount, lblFoodType (meal, basket
# page) and imgStaus (status icon). Returns null if the table is missing.
READ_ROWS_JS = r"""
(tableSel) => {
  const txt = el => (el ? el.innerText || el.textContent || '' : '').trim();
  const table = document.querySelector(tableSel);
  if (!table) return null;
  const sel = (tr, name) => {
    const s = tr.querySelector(`select[id*="${name}_"]`);
    return s && {id: s.id, name: s.name, disabled: s.disabled,
                 options: [...s.options].map(o => ({value: o.value, text: txt(o), selected: o.selected}))};
  };
  return [...table.querySelectorAll('tr')].filter(tr => tr.querySelector('input[id*="chkReserve"]')).map(tr => {
    const c = tr.querySelector('input[id*="chkReserve"]');
    const meal = tr.querySelector('span[id*="lblFoodType"]');
    const status = tr.querySelector('img[id*="imgStaus"], img[id*="imgStatus"]');
    return {
      label: meal ? txt(meal) : txt(tr.children[1]),
      check: {id: c.id, name: c.name, checked: c.checked, disabled: c.disabled},
      self: sel(tr, 'drpSelf'), food: sel(tr, 'drpFood'), count: sel(tr, 'drpFoodCount'),
      status: status ? status.title || '' : '',
    };
  });
}
"""

# True while the page is still being parsed or an ASP.NET AJAX postback is
# running. Not "complete": some subresource never finishes loading from the
# server, so the load event comes 15-30 s late; the form works long before.
STATE_JS = r"""
() => {
  if (!window.__botMark) return document.readyState === 'loading';
  const prm = window.Sys && Sys.WebForms && Sys.WebForms.PageRequestManager;
  return document.readyState === 'loading' ||
         !!(prm && prm.getInstance().get_isInAsyncPostBack());
}
"""

DATE_RE = re.compile(r"\d{4}/\d{2}/\d{2}")


def _css(item: dict) -> str:
    return f"#{item['id']}" if item.get("id") else f"[name=\"{item['name']}\"]"


def _real_options(select: dict | None) -> list[dict]:
    if not select:
        return []
    return [o for o in select["options"] if norm(o["text"]) not in PLACEHOLDERS and o["value"] != "-1"]


def food_name(text: str) -> str:
    """Option text without a trailing price or stock note ("... 250000 ریال", "(3)")."""
    text = re.sub(r"\s*\(\s*[\d,٬]+\s*\)\s*$", "", norm(text))
    return re.sub(r"\s*[\d,٬]+\s*(ریال|تومان)\s*$", "", text).strip()


def _food_names(select: dict | None) -> list[str]:
    return list(dict.fromkeys(n for n in (food_name(o["text"]) for o in _real_options(select)) if n))


def _usable(row: dict) -> bool:
    """A grid row whose food can still be chosen (not locked / past)."""
    return not row["check"]["disabled"] and bool(row["food"]) and not row["food"]["disabled"]


class CullinanClient:
    def __init__(
        self,
        browser: Browser,
        username: str,
        password: str,
        throttle: Throttle,
        solve_captcha: CaptchaSolver,
    ):
        self.browser = browser
        self.username = username
        self.password = password
        self.throttle = throttle
        self.solve_captcha = solve_captcha
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.dialogs: list[str] = []
        # On-demand runs (login test, menu fetch) a user is waiting for: space
        # requests by STEP_SECONDS only, instead of the full polling interval.
        self.interactive = False
        self.poll_interval: float | None = None  # per-job polling interval (seconds), None = default
        self.captcha_attempts = 0  # of the last successful login, for the login test report
        self.display_name = ""  # real name from site portal profile

    async def _page(self) -> Page:
        if self.page is None or self.page.is_closed():
            self.context = await self.browser.new_context(locale="fa-IR", viewport={"width": 1366, "height": 900})
            await self.context.route("**/*", self._route)
            self._watch_slow_requests(self.context)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(15_000)
            self.page.set_default_navigation_timeout(30_000)
            self.page.on("dialog", self._on_dialog)
        return self.page

    @staticmethod
    def _watch_slow_requests(context: BrowserContext) -> None:
        """Log requests slower than 3 s (without query strings) to find what stalls pages."""
        started: dict = {}

        def done(req):
            t = started.pop(req, None)
            if t is not None and time.monotonic() - t > 3:
                log.info("slow request (%.1fs, %s): %s", time.monotonic() - t, req.resource_type, req.url.split("?")[0][:150])

        context.on("request", lambda req: started.__setitem__(req, time.monotonic()))
        context.on("requestfinished", done)
        context.on("requestfailed", done)

    @staticmethod
    async def _route(route: Route) -> None:
        # Images/fonts (e.g. the news box embeds dozens from other sites) only slow
        # every page load and the "network idle" wait; the captcha is the exception.
        req = route.request
        if req.resource_type in ("image", "media", "font") and "captcha" not in req.url.lower():
            await route.abort()
        else:
            await route.continue_()

    async def _on_dialog(self, dialog) -> None:
        self.dialogs.append(dialog.message)
        await dialog.accept()

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        self.context = self.page = None

    async def _settle(self, page: Page) -> None:
        # Pages are server-rendered: once parsed (domcontentloaded, which goto
        # already waited for) the form is usable. "Network idle" is only a short
        # best effort, because a subresource or a poll may never finish.
        try:
            await page.wait_for_load_state("networkidle", timeout=1_500)
        except Exception:
            pass

    async def _postback(self, page: Page, action: Callable[[], Awaitable]) -> None:
        """Run an action that may post back, and wait until the page is stable.

        Handles both full-page postbacks (the old document is replaced) and
        UpdatePanel/AJAX postbacks (no navigation, just XHR traffic).
        """
        pending: dict = {}  # request -> start time
        kinds = ("document", "xhr", "fetch", "script")

        def on_request(req):
            if req.resource_type in kinds:
                pending[req] = time.monotonic()

        def on_done(req):
            pending.pop(req, None)

        def busy_requests() -> bool:
            # A request open for longer than LONG_REQUEST is a long poll, not our postback.
            now = time.monotonic()
            return any(now - started < LONG_REQUEST for started in pending.values())

        page.on("request", on_request)
        page.on("requestfinished", on_done)
        page.on("requestfailed", on_done)
        try:
            await page.evaluate("window.__botMark = 1")
            try:
                await action()
            except Exception as exc:
                # The element can vanish mid-call when the page navigates away.
                if not any(k in str(exc) for k in ("context was destroyed", "detached", "not attached", "navigat")):
                    raise
            quiet = 0
            for _ in range(150):  # at most ~30 s
                await asyncio.sleep(0.2)
                try:
                    busy = await page.evaluate(STATE_JS)
                except Exception:  # navigating right now
                    quiet = 0
                    continue
                quiet = quiet + 1 if not busy and not busy_requests() else 0
                if quiet >= 2:
                    break
        finally:
            page.remove_listener("request", on_request)
            page.remove_listener("requestfinished", on_done)
            page.remove_listener("requestfailed", on_done)

    async def _goto(self, url: str, step: bool = False) -> Page:
        page = await self._page()
        await self.throttle.wait(step=step or self.interactive, interval=self.poll_interval)
        await page.goto(url, wait_until="domcontentloaded")
        await self._settle(page)
        return page

    async def _on_login_page(self, page: Page) -> bool:
        return await page.locator(SEL["username"]).count() > 0

    @staticmethod
    async def _fill(page: Page, selector: str, value: str) -> None:
        # The login inputs are readonly until focused (an anti-autofill trick).
        field = page.locator(selector).first
        await field.evaluate("el => el.removeAttribute('readonly')")
        await field.fill(value)

    async def read_profile(self) -> dict:
        """Read student profile details (name, student number, credit) after login."""
        page = self.page or await self._page()
        name = ""
        for sel in ("#divUsername", "[id$='divUsername']", "#cphMain_titleBoxValue", "[id$='titleBoxValue']"):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    txt = norm(await loc.inner_text())
                    if "کاربر:" in txt:
                        m = re.search(r"کاربر:\s*([^-]+)", txt)
                        if m:
                            name = m.group(1).strip()
                            break
                    elif txt and len(txt) > 2 and not txt.isdigit():
                        name = txt
                        break
            except Exception:
                pass
        credit = ""
        for sel in ("#lblCredit", "[id$='lblCredit']", "#lblMojoodi", "[id$='lblMojoodi']", ".credit", "[id*='Credit' i]"):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    val = norm(await loc.inner_text()).strip()
                    if val and (any(ch.isdigit() for ch in val) or "ریال" in val or "تومان" in val):
                        credit = val
                        break
            except Exception:
                pass
        if name:
            self.display_name = name
        return {"name": name, "credit": credit}

    # ---------------------------------------------------------------- login
    async def login(self) -> None:
        self.throttle.login_attempt()
        started = time.monotonic()
        page = await self._goto(config.SITE_URL)
        log.info("login page loaded in %.1fs", time.monotonic() - started)
        if not await self._on_login_page(page):
            return
        for attempt in range(1, config.CAPTCHA_OCR_TRIES + 2):
            await self._fill(page, SEL["username"], self.username)
            await self._fill(page, SEL["password"], self.password)
            png = await page.locator(SEL["captcha_img"]).first.screenshot()
            t = time.monotonic()
            code = await self.solve_captcha(png, attempt)
            log.info("captcha attempt %d: %s in %.1fs", attempt, "read" if code else "unreadable", time.monotonic() - t)
            if not code:
                if attempt > config.CAPTCHA_OCR_TRIES:
                    break
                page = await self._goto(config.SITE_URL, step=True)  # fresh captcha
                continue
            await self._fill(page, SEL["captcha_input"], code)
            await self.throttle.wait(step=True)
            t = time.monotonic()
            await self._postback(page, page.locator(SEL["login_button"]).first.click)
            log.info("login postback took %.1fs", time.monotonic() - t)
            if not await self._on_login_page(page):
                self.captcha_attempts = attempt
                log.info("logged in as %s in %.1fs (captcha attempt %d)", self.username, time.monotonic() - started, attempt)
                await self.read_profile()
                return
            error = norm(await self._text(page, SEL["login_error"]) + " " + " ".join(self.dialogs[-1:]))
            if any(h in error for h in WRONG_PASSWORD_HINTS) and "امنیتی" not in error and "تصویر" not in error:
                raise LoginFailed(error or "نام کاربری یا رمز اشتباه است")
            log.info("login attempt %d failed (%s), retrying", attempt, error or "captcha?")
        raise SiteError("ورود ناموفق بود (کپچا حل نشد)")

    async def _text(self, page: Page, selector: str) -> str:
        loc = page.locator(selector)
        texts = []
        for i in range(min(await loc.count(), 5)):
            if await loc.nth(i).is_visible():
                texts.append((await loc.nth(i).inner_text()).strip())
        return " ".join(t for t in texts if t)

    async def _open(self, path: str) -> Page:
        """Open a page of the site, logging in first if the session is gone."""
        url = urljoin(config.SITE_URL, path)
        page = await self._goto(url)
        if await self._on_login_page(page) or "login" in page.url.lower():
            await self.login()
            page = await self._goto(url, step=True)
            if await self._on_login_page(page):
                raise SiteError("بعد از ورود هم صفحه باز نشد")
        return page

    async def _rows(self, page: Page, table: str) -> list[dict] | None:
        return await page.evaluate(READ_ROWS_JS, table)

    # --------------------------------------------------------------- basket
    async def open_basket(self, date: str) -> Page:
        page = await self._open(config.BASKET_PATH)
        await self._set_date(page, date)
        return page

    async def _set_date(self, page: Page, date: str) -> None:
        field = page.locator(SEL["basket_date"]).first
        if await field.count() == 0:
            raise SiteError("فیلد تاریخ در صفحهٔ سبد پیدا نشد")
        if norm(await field.input_value()) == date:
            return
        await self.throttle.wait(step=True)
        go = page.locator(SEL["basket_go"]).first
        if await go.count():
            # The date box has no postback of its own: set it, then press the arrow next to it.
            await field.evaluate("(el, v) => { el.value = v; }", date)
            await self._postback(page, go.click)
        else:
            await self._postback(page, lambda: field.evaluate(
                """(el, v) => { el.value = v; el.dispatchEvent(new Event('change', {bubbles: true})); }""", date))
        if norm(await page.locator(SEL["basket_date"]).first.input_value()) != date:
            raise SiteError(f"سایت تاریخ سبد را روی {date} نبرد")

    async def basket_rows(self, page: Page) -> list[dict]:
        rows = await self._rows(page, SEL["basket_grid"])
        if rows is None:
            raise SiteError("جدول سبد غذایی در صفحه پیدا نشد")
        return rows

    async def _select(self, page: Page, select: dict, value: str) -> None:
        await self.throttle.wait(step=True)
        await self._postback(page, lambda: page.select_option(_css(select), value=value))

    async def check_basket(self) -> None:
        """Log in and make sure the basket page opens."""
        await self.login()
        started = time.monotonic()
        page = await self._goto(urljoin(config.SITE_URL, config.BASKET_PATH), step=True)
        log.info("basket page loaded in %.1fs", time.monotonic() - started)
        if await self._on_login_page(page):
            raise SiteError("ورود انجام شد ولی صفحهٔ سبد غذایی باز نشد")

    async def attempt(self, job: Job) -> Attempt:
        """One polling round: look at the basket and buy if a wanted food is there."""
        self.dialogs.clear()
        page = await self.open_basket(job.date)
        rows = await self.basket_rows(page)
        # One row per meal; rows of meals with nothing in the basket are locked.
        mine = [i for i, r in enumerate(rows) if not job.meal or matches(r["label"], job.meal)]
        open_rows = [i for i in mine if _usable(rows[i]) and _real_options(rows[i]["food"])]
        seen = [f"{rows[i]['label']}: {food_name(o['text'])}" for i in open_rows for o in _real_options(rows[i]["food"])]
        if not open_rows:
            return Attempt("empty", "فعلاً غذایی در سبد نیست", seen)

        # 1) Best food by the user's priority list (across meals if no meal was given).
        choice = None
        for wanted in job.foods:
            for i in open_rows:
                hit = next((o for o in _real_options(rows[i]["food"]) if wanted == "*" or matches(o["text"], wanted)), None)
                if hit:
                    choice = (i, hit)
                    break
            if choice:
                break
        if choice is None:
            return Attempt("no_match", "غذای دلخواه در سبد نیست", seen)
        index, food = choice
        name = food_name(food["text"])

        if not food["selected"]:
            await self._select(page, rows[index]["food"], food["value"])
            rows = await self.basket_rows(page)

        # 2) Quantity.
        counts = _real_options(rows[index]["count"])
        if counts:
            want = str(job.quantity)
            opt = next((o for o in counts if norm(o["text"]) == want or o["value"] == want), counts[0])
            if not opt["selected"]:
                await self._select(page, rows[index]["count"], opt["value"])
                rows = await self.basket_rows(page)

        # 3) Tick the row and save.
        check = rows[index]["check"]
        if check["disabled"]:
            return Attempt("no_match", f"«{name}» همین الان از سبد برداشته شد", seen)
        if not check["checked"]:
            await page.locator(_css(check)).check()
        save = page.locator(SEL["basket_save"]).first
        if await save.count() == 0:
            raise SiteError("دکمهٔ «ذخیره» پیدا نشد")
        await self.throttle.wait(step=True)
        await self._postback(page, save.click)

        message = norm(" ".join(self.dialogs) + " " + await self._text(page, SEL["page_message"]))
        if any(h in message.lower() for h in ERROR_HINTS):
            return Attempt("error", f"خرید «{name}» ناموفق: {message}", seen)
        return Attempt("bought", f"«{name}» ({rows[index]['label']}) خریداری شد. {message}".strip(), seen)

    # ---------------------------------------------------- weekly reservation
    async def read_week(self, date: str) -> dict[str, dict[str, dict]]:
        """Menu and own reservations of the week containing `date`, from the reservation page.

        Returns {date: {meal: {"foods": [...], "reserved": food or "", "locked": bool}}}.
        Read-only: the page is only navigated (week arrows), never saved.
        """
        page = await self._open(RESERVATION_PATH)
        for _ in range(4):
            label = norm(await self._text(page, SEL["week_label"]))
            days = DATE_RE.findall(label)
            if len(days) < 2:
                raise SiteError("بازهٔ هفته در صفحهٔ رزرو پیدا نشد")
            if days[0] <= date <= days[-1]:
                break
            arrow = page.locator(SEL["week_next"] if date > days[-1] else SEL["week_prev"]).first
            if await arrow.count() == 0:
                raise SiteError("دکمهٔ هفتهٔ بعد/قبل در صفحهٔ رزرو پیدا نشد")
            await self.throttle.wait(step=True)
            await self._postback(page, arrow.click)
        else:
            raise SiteError(f"هفتهٔ {date} در صفحهٔ رزرو در دسترس نیست")

        week: dict[str, dict[str, dict]] = {}
        for meal, suffix in WEEK_GRIDS.items():
            for row in await self._rows(page, f"[id$='grdReservation{suffix}']") or []:
                day = DATE_RE.search(norm(row["label"]))
                if not day:
                    continue
                chosen = next((o for o in _real_options(row["food"]) if o["selected"]), None)
                reserved = row["check"]["checked"] or "رزرو شده" in row["status"]
                week.setdefault(day.group(0), {})[meal] = {
                    "foods": _food_names(row["food"]),
                    "reserved": food_name(chosen["text"]) if reserved and chosen else "",
                    "locked": row["check"]["disabled"],
                }
        return week

    async def read_menu(self, date: str, meal: str = "") -> dict[str, dict]:
        """Foods and reservation of `date`, per meal (only `meal` if given)."""
        day = (await self.read_week(date)).get(date, {})
        return {m: info for m, info in day.items() if not meal or matches(m, meal)}

    async def auto_reserve_week(self, target_date: str, wanted_meals: list[str]) -> list[str]:
        """Automatically reserve meals for the week containing `target_date`.
        Processes meals in order (e.g. صبحانه, ناهار, شام) and days in order (شنبه تا چهارشنبه).
        Clicks the respective save button for each meal.
        """
        page = await self._open(RESERVATION_PATH)
        for _ in range(4):
            label = norm(await self._text(page, SEL["week_label"]))
            days = DATE_RE.findall(label)
            if len(days) < 2:
                raise SiteError("بازهٔ هفته در صفحهٔ رزرو پیدا نشد")
            if days[0] <= target_date <= days[-1]:
                break
            arrow = page.locator(SEL["week_next"] if target_date > days[-1] else SEL["week_prev"]).first
            if await arrow.count() == 0:
                raise SiteError("دکمهٔ هفتهٔ بعد/قبل در صفحهٔ رزرو پیدا نشد")
            await self.throttle.wait(step=True)
            await self._postback(page, arrow.click)
        else:
            raise SiteError(f"هفتهٔ {target_date} در صفحهٔ رزرو در دسترس نیست")

        results = []
        save_buttons = {
            "صبحانه": "#cphMain_btnSaveBreakfast, [id$='btnSaveBreakfast']",
            "ناهار": "#cphMain_btnSaveLunch, [id$='btnSaveLunch']",
            "شام": "#cphMain_btnSaveDinner, [id$='btnSaveDinner']",
        }

        # Process each meal in order (صبحانه, ناهار, شام)
        for meal in ["صبحانه", "ناهار", "شام"]:
            if wanted_meals and meal not in wanted_meals:
                continue
            suffix = WEEK_GRIDS.get(meal)
            if not suffix:
                continue
            grid_sel = f"[id$='grdReservation{suffix}']"
            rows = await self._rows(page, grid_sel) or []
            if not rows:
                continue

            changed = False
            for idx, row in enumerate(rows):
                day_m = DATE_RE.search(norm(row["label"]))
                day_str = day_m.group(0) if day_m else f"روز {idx+1}"
                check = row["check"]

                if check["checked"] or "رزرو شده" in row["status"]:
                    chosen = next((o for o in _real_options(row["food"]) if o["selected"]), None)
                    c_name = food_name(chosen["text"]) if chosen else "غذا"
                    results.append(f"✓ {day_str} ({meal}): از قبل رزرو شده («{c_name}»)")
                    continue

                if check["disabled"]:
                    continue

                foods = _real_options(row["food"])
                if not foods:
                    continue

                first_food = foods[0]
                if not first_food["selected"]:
                    await self._select(page, row["food"], first_food["value"])
                    rows = await self._rows(page, grid_sel) or []
                    if idx < len(rows):
                        check = rows[idx]["check"]

                if not check["disabled"] and not check["checked"]:
                    await page.locator(_css(check)).check()
                    changed = True
                    results.append(f"★ {day_str} ({meal}): ثبت شد («{food_name(first_food['text'])}»)")

            if changed:
                btn_sel = save_buttons.get(meal)
                save_btn = page.locator(btn_sel).first if btn_sel else None
                if save_btn and await save_btn.count() > 0:
                    await self.throttle.wait(step=True)
                    await self._postback(page, save_btn.click)
                    msg = norm(" ".join(self.dialogs[-1:]) + " " + await self._text(page, SEL["page_message"]))
                    if msg:
                        results.append(f"پیام سامانه برای {meal}: {msg}")

        return results

    async def get_forgot_receipt(self, date: str, meal: str = "", reserve_type: str = "0") -> dict:
        """Fetch forgot card receipt code from Reservation/ForgotReceipt.aspx.
        Handles two-step confirmation (cphMain_btnOk -> cphMain_btnAccept) with resilient polling.
        """
        page = await self._open("Reservation/ForgotReceipt.aspx")

        # 1) Set date in #cphMain_txtDate
        date_input = page.locator("#cphMain_txtDate, input[name$='txtDate']").first
        if await date_input.count() > 0:
            await date_input.fill(date)
            await date_input.evaluate("""(el, v) => {
                el.value = v;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""", date)

        # 2) Set reserve type (0 = هفتگی, 2 = سبد غذایی)
        res_type = page.locator("#cphMain_drpReserveType, select[name$='drpReserveType']").first
        if await res_type.count() > 0:
            val = str(reserve_type) if str(reserve_type) in ("0", "1", "2") else "0"
            await res_type.select_option(value=val)

        # 3) Set meal dropdown (1=صبحانه, 2=ناهار, 3=شام)
        meal_sel = page.locator("#cphMain_drpFoodType, select[name$='drpFoodType']").first
        if await meal_sel.count() > 0:
            meal_val = "1" if "صبحانه" in meal else ("3" if "شام" in meal else "2")
            await meal_sel.select_option(value=meal_val)

        # 4) Submit with #cphMain_btnOk (value="تأیید")
        btn = page.locator("#cphMain_btnOk, input[name$='btnOk'], input[type=submit][value*='تأیید']").first
        if await btn.count() == 0:
            raise SiteError("دکمهٔ تأیید فیش فراموشی پیدا نشد")

        await self.throttle.wait(step=True)
        await self._postback(page, btn.click)

        # 5) Step 2: Poll for UpdatePanel result (either btnAccept 'موافقم', remind code, or error alert)
        btn_accept = None
        for _ in range(30):
            # Check for direct remind code
            remind_loc = page.locator("#cphMain_lblRemindId, [id$='lblRemindId']").first
            if await remind_loc.count() > 0:
                t = norm(await remind_loc.inner_text()).strip()
                if t and any(ch.isdigit() for ch in t):
                    break

            acc_loc = page.locator("#cphMain_btnAccept, input[name$='btnAccept'], input[value*='موافقم']").first
            if await acc_loc.count() > 0 and await acc_loc.is_visible():
                btn_accept = acc_loc
                break

            alert_loc = page.locator("#cphMain_alertBoxValue").first
            if await alert_loc.count() > 0:
                t = norm(await alert_loc.inner_text()).strip()
                if t:
                    break

            toast_loc = page.locator(".toast-message").first
            if await toast_loc.count() > 0:
                t = norm(await toast_loc.inner_text()).strip()
                if t:
                    break

            await asyncio.sleep(0.2)

        # If confirmation button 'موافقم' appeared, click it to issue the receipt!
        if btn_accept:
            await self.throttle.wait(step=True)
            await self._postback(page, btn_accept.click)
            for _ in range(30):
                remind_loc = page.locator("#cphMain_lblRemindId, [id$='lblRemindId']").first
                if await remind_loc.count() > 0:
                    t = norm(await remind_loc.inner_text()).strip()
                    if t and any(ch.isdigit() for ch in t):
                        break
                alert_loc = page.locator("#cphMain_alertBoxValue").first
                if await alert_loc.count() > 0:
                    t = norm(await alert_loc.inner_text()).strip()
                    if t:
                        break
                toast_loc = page.locator(".toast-message").first
                if await toast_loc.count() > 0:
                    t = norm(await toast_loc.inner_text()).strip()
                    if t:
                        break
                res_loc = page.locator("[id$='lblBarcode'], [id$='lblReceiptCode'], [id$='lblFish'], [id$='lblCode'], #cphMain_lblResult").first
                if await res_loc.count() > 0:
                    t = norm(await res_loc.inner_text()).strip()
                    if any(ch.isdigit() for ch in t):
                        break
                await asyncio.sleep(0.2)

        # 6) Collect all feedback texts from the site
        alert_text = ""
        for sel in ("#cphMain_alertBoxValue", ".toast-message", "#toast-container"):
            loc = page.locator(sel).first
            if await loc.count() > 0:
                txt = norm(await loc.inner_text()).strip()
                if txt:
                    alert_text = txt
                    break

        if not alert_text and self.dialogs:
            alert_text = norm(self.dialogs[-1]).strip()

        # 7) Extract code from receipt labels or alert_text
        code = ""
        used_count = ""
        used_loc = page.locator("#cphMain_lblUsedRemindId, [id$='lblUsedRemindId']").first
        if await used_loc.count() > 0:
            used_count = norm(await used_loc.inner_text()).strip()

        code_selectors = [
            "#cphMain_lblRemindId",
            "[id$='lblRemindId']",
            "[id$='lblBarcode']",
            "[id$='lblReceiptCode']",
            "[id$='lblFish']",
            "[id$='lblCode']",
            ".receipt-code",
            "#cphMain_lblResult",
        ]
        for code_sel in code_selectors:
            loc = page.locator(code_sel).first
            if await loc.count() > 0:
                c_text = norm(await loc.inner_text()).strip()
                m = re.search(r"(\d{3,12})", c_text)
                if m:
                    code = m.group(1)
                    break

        patterns = [
            r"(?:کد|شماره)\s*(?:فیش|فراموشی|ژتون|بارکد)\s*[:=\-]?\s*(\d{3,12})",
            r"(\d{4,10})",
        ]
        has_error = any(h in alert_text for h in ("غیرفعال", "خطا", "یافت نشد", "ندارید", "خارج از بازه", "امکان", "سقف", "ناموفق", "پایان", "مجاز نیست"))
        if not code and alert_text and not has_error:
            for pat in patterns:
                m = re.search(pat, alert_text)
                if m:
                    code = m.group(1)
                    break

        success = bool(code) or ("موفق" in alert_text and not has_error)
        msg = alert_text
        if not msg:
            if code:
                msg = f"کد فیش فراموشی: {code}"
                if used_count:
                    msg += f" (دفعات استفاده‌شده در این ماه: {used_count})"
            else:
                msg = "پاسخی از سامانه سلف دریافت نشد یا این وعده فاقد فیش است."

        return {
            "success": success,
            "code": code,
            "message": msg,
            "date": date,
            "meal": meal,
        }

    async def create_payment_session(self, amount_rials: int, bank_idx: int = 0) -> dict:
        """Navigate to ePay/ePay_Payment.aspx, enter amount, click bank logo,
        confirm on Step 2 (#cphMain_btnPay), and capture direct bank gateway data.
        bank_idx: 0 = Sadad (Melli), 1 = BehPardakht (Mellat)
        """
        page = await self._open("ePay/ePay_Payment.aspx")

        captured_data: dict = {}

        def handle_request(req):
            url = req.url
            if any(domain in url for domain in ("shaparak.ir", "cbinasim.ir", "hadaf.cfu.ac.ir")):
                post_data = req.post_data or ""
                fields = {}
                if post_data:
                    from urllib.parse import parse_qs
                    try:
                        parsed = parse_qs(post_data)
                        fields = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
                    except Exception:
                        pass
                bank_name = "به‌پرداخت ملت" if "bpm.shaparak" in url else ("سداد (بانک ملی)" if "sadad" in url else "درگاه شاپرک")
                captured_data.update({
                    "action": url,
                    "method": req.method,
                    "fields": fields,
                    "bank": bank_name,
                })

        page.on("request", handle_request)

        # Route interceptor: catch any request heading towards Shaparak / payment gateways
        async def on_route(route, request):
            url = request.url
            if any(domain in url for domain in ("shaparak.ir", "cbinasim.ir", "hadaf.cfu.ac.ir")):
                post_data = request.post_data or ""
                fields = {}
                if post_data:
                    from urllib.parse import parse_qs
                    try:
                        parsed = parse_qs(post_data)
                        fields = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
                    except Exception:
                        pass
                bank_name = "به‌پرداخت ملت" if "bpm.shaparak" in url else ("سداد (بانک ملی)" if "sadad" in url else "درگاه شاپرک")
                captured_data.update({
                    "action": url,
                    "method": request.method,
                    "fields": fields,
                    "bank": bank_name,
                })
                try:
                    await route.abort()
                except Exception:
                    pass
            else:
                try:
                    await route.continue_()
                except Exception:
                    pass

        await page.route("**/*", on_route)

        # Step 1: Set payment amount
        amount_str = f"{amount_rials:,}"
        amt_input = page.locator("#cphMain_txtPaymentAmount, input[name$='txtPaymentAmount']").first
        if await amt_input.count() == 0:
            raise SiteError("فیلد مبلغ در صفحه پرداخت پیدا نشد")

        await amt_input.fill(amount_str)
        await amt_input.evaluate("""(el, v) => {
            el.value = v;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('keyup', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""", amount_str)

        # Select bank button: #cphMain_dataLstBank_imgBankLogo_0 (Sadad) or _1 (Mellat)
        bank_sel = f"#cphMain_dataLstBank_imgBankLogo_{bank_idx}"
        btn = page.locator(bank_sel).first
        if await btn.count() == 0:
            btn = page.locator("#cphMain_dataLstBank input[type=image]").first
        if await btn.count() == 0:
            raise SiteError("درگاه بانکی در صفحه یافت نشد")

        await self.throttle.wait(step=True)
        await self._postback(page, btn.click)

        # Step 2: Now poll for confirmation screen (#cphMain_btnPay 'پرداخت' or alert)
        btn_pay = None
        for _ in range(30):
            pay_loc = page.locator("#cphMain_btnPay, input[name$='btnPay'], input[value*='پرداخت']").first
            if await pay_loc.count() > 0 and await pay_loc.is_visible():
                btn_pay = pay_loc
                break
            alert_text = norm(await self._text(page, "#cphMain_alertBoxValue, #cphMain_messageBoxValue")).strip()
            if alert_text:
                raise SiteError(f"خطای سامانه سلف در ثبت پرداخت: {alert_text}")
            await asyncio.sleep(0.2)

        if not btn_pay:
            raise SiteError("دکمهٔ تأیید پرداخت نهایی در صفحه پیدا نشد")

        # Inject interceptors into the confirmation page before clicking btnPay
        await page.evaluate("""
            () => {
                window.__paymentData = null;
                const setPay = (action, method, fields, bank) => {
                    window.__paymentData = { action, method, fields, bank };
                };
                window.postToBehPardakht = function(refId) {
                    setPay("https://bpm.shaparak.ir/pgwchannel/startpay.mellat", "POST", { RefId: refId }, "به‌پرداخت ملت");
                };
                window.postToSadad = function(merchantId, terminalId, timeStamp, amount, paymentId, FP, revertURL) {
                    setPay("https://sadad.shaparak.ir/purchase", "POST", {
                        CardAcqID: merchantId,
                        AmountTrans: amount,
                        ORDERID: paymentId,
                        TerminalID: terminalId,
                        TimeStamp: timeStamp,
                        FP: FP,
                        RedirectURL: revertURL
                    }, "سداد (بانک ملی)");
                };
                window.postToSamanKish = function(token) {
                    setPay("https://sep.shaparak.ir/OnlinePG/OnlinePG", "POST", { Token: token, GetMethod: "" }, "سامان کیش");
                };
                window.postToIranKish = function(merchantId, token) {
                    setPay("https://ikc.shaparak.ir/TPayment/Payment/index", "POST", { merchantId: merchantId, token: token }, "ایران کیش");
                };
                window.postToIranKishNew = function(merchantId, token) {
                    setPay("https://ikc.shaparak.ir/iuiv3/IPG/Index", "POST", { tokenIdentity: token }, "ایران کیش");
                };
                window.postToOmidPay = function(token, language) {
                    setPay("https://say.shaparak.ir/_ipgw_/MainTemplate/payment/", "POST", { token: token, language: language || "fa" }, "امید پی");
                };
                window.postToSepehrSaderat = function(token, terminalID) {
                    setPay("https://sepehr.shaparak.ir:8080/Pay", "POST", { token: token, terminalID: terminalID }, "سپهر صادرات");
                };
                const _origSubmit = HTMLFormElement.prototype.submit;
                HTMLFormElement.prototype.submit = function() {
                    const action = this.action || window.location.href;
                    if (action.includes("shaparak.ir") || action.includes("cbinasim.ir") || action.includes("cfu.ac.ir")) {
                        const fd = new FormData(this);
                        const fields = {};
                        for (const [k, v] of fd.entries()) { fields[k] = v; }
                        setPay(action, (this.method || "POST").toUpperCase(), fields, "درگاه شاپرک");
                        return;
                    }
                    return _origSubmit.apply(this, arguments);
                };
            }
        """)

        # Ensure txtPaymentAmount is in form when btnPay is submitted
        await page.evaluate("""(amt_val) => {
            const form = document.getElementById("MainForm") || document.forms[0];
            if (form) {
                let amt = form.querySelector('input[name="ctl00$cphMain$txtPaymentAmount"], input[name$="txtPaymentAmount"]');
                if (!amt) {
                    amt = document.createElement("input");
                    amt.type = "hidden";
                    amt.name = "ctl00$cphMain$txtPaymentAmount";
                    amt.id = "cphMain_txtPaymentAmount";
                    form.appendChild(amt);
                }
                amt.value = amt_val;
            }
        }""", amount_str)

        # Click payment button (#cphMain_btnPay)
        await self.throttle.wait(step=True)
        try:
            await self._postback(page, btn_pay.click)
        except Exception:
            # Navigation or abort may happen when clicking pay
            pass

        # Wait for either route interception or JS variable
        for _ in range(40):
            if captured_data.get("action"):
                captured_data["amount_rials"] = amount_rials
                return captured_data
            try:
                data = await page.evaluate("() => window.__paymentData")
                if data and data.get("action"):
                    data["amount_rials"] = amount_rials
                    return data
            except Exception:
                pass
            await asyncio.sleep(0.25)

        alert = norm(await self._text(page, "#cphMain_alertBoxValue, #cphMain_messageBoxValue"))
        if alert:
            raise SiteError(f"خطای سامانه در اتصال به درگاه: {alert}")
        raise SiteError("درگاه بانکی پاسخی ارسال نکرد؛ لطفاً دقایقی دیگر امتحان کنید")


async def launch_browser(playwright) -> Browser:
    return await playwright.chromium.launch(headless=config.HEADLESS, executable_path=config.CHROMIUM_PATH)


def describe_rows(rows: list[dict]) -> str:
    """Human-readable dump of grid rows, used by the dump tool."""
    lines = []
    for r in rows:
        foods = ", ".join(("*" if o["selected"] else "") + o["text"] for o in (r["food"] or {}).get("options", []))
        lines.append(f"{r['label']} | checked={r['check']['checked']} disabled={r['check']['disabled']} "
                     f"status={r['status']!r} | {foods}")
    return "\n".join(lines)
