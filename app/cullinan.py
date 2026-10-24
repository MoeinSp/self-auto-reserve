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
    "save_button": "input[type=submit][value*='ذخیره'], button:has-text('ذخیره'), a:has-text('ذخیره')",
    "page_message": "[id*='lblMessage' i], [id*='lblMsg' i], [id*='lblError' i], .alert, .toast-message",
}

# Dropdown entries that mean "nothing selected".
PLACEHOLDERS = {"", "-", "--", "0", "انتخاب", "انتخاب کنید", "---"}
MEAL_NAMES = ("صبحانه", "ناهار", "شام", "سحری", "افطار")
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


# Collects the basket grid into plain data. The grid is found by its header
# row containing "غذا"; each data row with a checkbox is a purchase slot.
READ_GRID_JS = r"""
() => {
  const txt = el => (el ? el.innerText || el.textContent || '' : '').trim();
  const tables = [...document.querySelectorAll('table')].filter(t =>
    [...t.querySelectorAll('th, tr:first-child td')].some(h => txt(h).includes('غذا')));
  if (!tables.length) return null;
  const table = tables[tables.length - 1];
  const headRow = table.querySelector('tr');
  const headers = [...headRow.children].map(txt);
  const rows = [];
  for (const tr of [...table.querySelectorAll('tr')].slice(1)) {
    const cells = [...tr.children].map((td, i) => ({
      header: headers[i] || '',
      text: txt(td),
      selects: [...td.querySelectorAll('select')].map(s => ({
        id: s.id, name: s.name, disabled: s.disabled,
        options: [...s.options].map(o => ({value: o.value, text: txt(o), selected: o.selected})),
      })),
      checkboxes: [...td.querySelectorAll('input[type=checkbox]')].map(c => ({id: c.id, name: c.name, checked: c.checked, disabled: c.disabled})),
    }));
    if (cells.some(c => c.selects.length || c.checkboxes.length)) rows.push(cells);
  }
  return {headers, rows};
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

FIND_DATE_INPUT_JS = r"""
() => {
  const re = /^[0-9۰-۹]{4}\/[0-9۰-۹]{1,2}\/[0-9۰-۹]{1,2}$/;
  const inputs = [...document.querySelectorAll('input[type=text], input:not([type])')];
  const el = inputs.find(i => re.test((i.value || '').trim())) ||
             inputs.find(i => /date|tarikh/i.test(i.id + ' ' + i.name));
  return el ? (el.id ? '#' + CSS.escape(el.id) : `[name="${el.name}"]`) : null;
}
"""


def _css(item: dict) -> str:
    return f"#{item['id']}" if item.get("id") else f"[name=\"{item['name']}\"]"


def _real_options(select: dict) -> list[dict]:
    return [o for o in select["options"] if norm(o["text"]) not in PLACEHOLDERS]


def _food_names(select: dict) -> list[str]:
    """Option texts without a trailing stock/price note like "(3)"."""
    names = [re.sub(r"\s*\(\s*[\d,٬]+\s*\)\s*$", "", o["text"]).strip() for o in _real_options(select)]
    return list(dict.fromkeys(n for n in names if n))


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

    # --------------------------------------------------------------- basket
    async def open_basket(self, date: str) -> Page:
        url = urljoin(config.SITE_URL, config.BASKET_PATH)
        page = await self._goto(url)
        if await self._on_login_page(page) or "login" in page.url.lower():
            await self.login()
            page = await self._goto(url, step=True)
            if await self._on_login_page(page):
                raise SiteError("بعد از ورود هم صفحهٔ سبد باز نشد")
        await self._set_date(page, date)
        return page

    async def _set_date(self, page: Page, date: str) -> None:
        selector = await page.evaluate(FIND_DATE_INPUT_JS)
        if not selector:
            raise SiteError("فیلد تاریخ در صفحهٔ سبد پیدا نشد")
        current = norm(await page.locator(selector).input_value())
        if current == date:
            return
        await self.throttle.wait(step=True)
        # Set the value like the date picker does, then fire change/blur so a
        # WebForms AutoPostBack (if any) runs.
        await self._postback(page, lambda: page.locator(selector).evaluate(
            """(el, v) => { el.value = v;
                 el.dispatchEvent(new Event('input', {bubbles: true}));
                 el.dispatchEvent(new Event('change', {bubbles: true}));
                 if (document.contains(el)) el.dispatchEvent(new Event('blur', {bubbles: true})); }""",
            date,
        ))

    async def read_grid(self, page: Page) -> dict:
        grid = await page.evaluate(READ_GRID_JS)
        if not grid or not grid["rows"]:
            raise SiteError("جدول سبد غذایی در صفحه پیدا نشد")
        return grid

    @staticmethod
    def _role(header: str) -> str:
        h = norm(header)
        if "غذا" in h:
            return "food"
        if "تعداد" in h:
            return "count"
        if "رزرو" in h:
            return "check"
        return "filter"  # e.g. meal / self columns that narrow the food list

    def _slot(self, grid: dict, index: int) -> dict:
        row = grid["rows"][index]
        slot = {"food": None, "count": None, "check": None, "filters": []}
        for cell in row:
            role = self._role(cell["header"])
            for sel in cell["selects"]:
                if role == "food" and slot["food"] is None:
                    slot["food"] = sel
                elif role == "count" and slot["count"] is None:
                    slot["count"] = sel
                else:
                    slot["filters"].append(sel)
            for box in cell["checkboxes"]:
                slot["check"] = slot["check"] or box
        if slot["food"] is None:
            raise SiteError("ستون «غذا» در جدول سبد پیدا نشد")
        return slot

    def _free_slot_index(self, grid: dict) -> int:
        for i in range(len(grid["rows"])):
            slot = self._slot(grid, i)
            selected = [o for o in slot["food"]["options"] if o["selected"]]
            chosen = selected and norm(selected[0]["text"]) not in PLACEHOLDERS
            if not chosen and not (slot["check"] and slot["check"]["checked"]):
                return i
        return 0

    async def _select(self, page: Page, select: dict, value: str) -> None:
        await self.throttle.wait(step=True)
        await self._postback(page, lambda: page.select_option(_css(select), value=value))

    async def check_basket(self) -> None:
        """Log in and make sure the basket page opens (used by the login test)."""
        url = urljoin(config.SITE_URL, config.BASKET_PATH)
        await self.login()
        started = time.monotonic()
        page = await self._goto(url, step=True)
        log.info("basket page loaded in %.1fs", time.monotonic() - started)
        if await self._on_login_page(page):
            raise SiteError("ورود انجام شد ولی صفحهٔ سبد غذایی باز نشد")

    async def read_menu(self, date: str, meal: str = "") -> dict[str, list[str]]:
        """Foods offered on `date`, per meal (only `meal` if given). Read-only: nothing is ticked or saved."""
        self.dialogs.clear()
        page = await self.open_basket(date)
        grid = await self.read_grid(page)
        index = self._free_slot_index(grid)
        slot = self._slot(grid, index)
        meal_filter = next(
            (f for f in slot["filters"] if any(matches(o["text"], m) for o in f["options"] for m in MEAL_NAMES)),
            None,
        )
        if meal_filter is None:
            return {"": _food_names(slot["food"])}
        menu = {}
        for opt in _real_options(meal_filter):
            if meal and not matches(opt["text"], meal):
                continue
            if not opt["selected"]:
                await self._select(page, meal_filter, opt["value"])
            slot = self._slot(await self.read_grid(page), index)
            menu[norm(opt["text"])] = _food_names(slot["food"])
        return menu

    async def attempt(self, job: Job) -> Attempt:
        """One polling round: look at the basket and buy if a wanted food is there."""
        self.dialogs.clear()
        page = await self.open_basket(job.date)
        grid = await self.read_grid(page)
        index = self._free_slot_index(grid)
        slot = self._slot(grid, index)

        # 1) Narrow down by meal (and similar filter dropdowns), if requested.
        if job.meal:
            for flt in slot["filters"]:
                hit = next((o for o in flt["options"] if matches(o["text"], job.meal)), None)
                if hit and not hit["selected"]:
                    await self._select(page, flt, hit["value"])
                    grid = await self.read_grid(page)
                    slot = self._slot(grid, index)

        # 2) Pick the best food by the user's priority list.
        offered = _real_options(slot["food"])
        seen = [o["text"] for o in offered]
        if not offered:
            return Attempt("empty", "فعلاً غذایی در سبد نیست", seen)
        choice = None
        for wanted in job.foods:
            choice = next((o for o in offered if wanted == "*" or matches(o["text"], wanted)), None)
            if choice:
                break
        if choice is None:
            return Attempt("no_match", "غذای دلخواه در سبد نیست", seen)

        if not choice["selected"]:
            await self._select(page, slot["food"], choice["value"])
            grid = await self.read_grid(page)
            slot = self._slot(grid, index)

        # 3) Quantity.
        if slot["count"]:
            counts = _real_options(slot["count"])
            want = str(job.quantity)
            opt = next((o for o in counts if norm(o["text"]) == want or o["value"] == want), None)
            opt = opt or (counts[0] if counts else None)
            if opt and not opt["selected"]:
                await self._select(page, slot["count"], opt["value"])
                grid = await self.read_grid(page)
                slot = self._slot(grid, index)

        # 4) Tick the row and save.
        if slot["check"] and not slot["check"]["checked"]:
            await self.throttle.wait(step=True)
            await self._postback(page, page.locator(_css(slot["check"])).check)
        save = page.locator(SEL["save_button"]).first
        if await save.count() == 0:
            raise SiteError("دکمهٔ «ذخیره» پیدا نشد")
        await self.throttle.wait(step=True)
        await self._postback(page, save.click)

        message = norm(" ".join(self.dialogs) + " " + await self._text(page, SEL["page_message"]))
        if any(h in message.lower() for h in ERROR_HINTS):
            return Attempt("error", f"خرید «{choice['text']}» ناموفق: {message}", seen)
        return Attempt("bought", f"«{choice['text']}» خریداری شد. {message}".strip(), seen)


async def launch_browser(playwright) -> Browser:
    return await playwright.chromium.launch(headless=config.HEADLESS, executable_path=config.CHROMIUM_PATH)


def describe_grid(grid: dict) -> str:
    """Human-readable dump of the grid, used by the dump tool and logs."""
    lines = [" | ".join(grid["headers"])]
    for r, row in enumerate(grid["rows"]):
        for cell in row:
            for sel in cell["selects"]:
                opts = ", ".join(re.sub(r"\s+", " ", o["text"]) for o in sel["options"][:15])
                lines.append(f"row {r} [{cell['header']}] #{sel['id']}: {opts}")
            for box in cell["checkboxes"]:
                lines.append(f"row {r} [{cell['header']}] checkbox #{box['id']} checked={box['checked']}")
    return "\n".join(lines)
