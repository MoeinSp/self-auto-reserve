"""A tiny imitation of the Cullinan login, food-basket and weekly reservation
pages, for local tests.

It mimics what matters, with the real control ids: a captcha login, the
basket grid (one row per meal; the food dropdown posts back the whole form,
like ASP.NET WebForms) and the reservation grids (one per meal, one row per
day, a ticked box = reserved, arrows to switch weeks).
"""
import asyncio
import html

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

CAPTCHA = "4821"
USER, PASSWORD = "40300000", "secret"

# date -> meal -> {food: stock}; tests mutate this to "release" food.
STOCK: dict[str, dict[str, dict[str, int]]] = {}
PURCHASES: list[tuple[str, str, str, int]] = []
state = {"basket_loads": 0}

app = FastAPI()

# Logged-in pages keep a request open (like the real site), so "wait until the
# network is quiet" must not wait for it.
LONG_POLL = "<script>fetch('/poll')</script>"
PROFILE_SNIPPET = '<div id="divUsername" class="profileTitle">معین فیروزآبادی</div><label id="lblCredit">3,345,000 ریال</label>'


@app.get("/poll")
async def poll():
    await asyncio.sleep(25)
    return {}


def _opts(values, selected):
    return "".join(
        f'<option value="{html.escape(v)}"{" selected" if v == selected else ""}>{html.escape(t)}</option>'
        for v, t in values
    )


@app.get("/")
def login_page(request: Request, error: str = ""):
    if request.cookies.get("auth") == "ok":
        return HTMLResponse(f"<html><body>{PROFILE_SNIPPET}داشبورد{LONG_POLL}</body></html>")
    msg = f'<span id="lblMessage">{error}</span>' if error else ""
    # Like the real site: inputs are readonly until focused, and the news box
    # embeds images from another host (which the bot should not wait for).
    ro = """readonly="" onfocus="this.removeAttribute('readonly');\""""
    return HTMLResponse(f"""<html><body><form method="post" action="/">
      <input id="txtUsernamePlain" name="u" {ro}><input id="txtPasswordPlain" name="p" type="text" {ro}>
      <img src="https://news.example.invalid/emoji.png" width="18" height="18">
      <img id="Img1" src="/captcha.png" width="80" height="30"><input id="txtCaptcha" name="c" maxlength="4">
      <input type="submit" id="btnEncript" value="ورود">{msg}</form></body></html>""")


@app.get("/captcha.png")
def captcha_png():
    # 1x1 PNG; tests answer the captcha manually.
    return Response(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    ), media_type="image/png")


@app.post("/")
async def do_login(request: Request):
    f = await request.form()
    if f.get("c") != CAPTCHA:
        return RedirectResponse("/?error=کد امنیتی اشتباه است", 303)
    if (f.get("u"), f.get("p")) != (USER, PASSWORD):
        return RedirectResponse("/?error=نام کاربری یا رمز عبور اشتباه است", 303)
    r = RedirectResponse("/", 303)
    r.set_cookie("auth", "ok")
    return r


MEALS = ["صبحانه", "ناهار", "شام"]
DISABLED = ' disabled="disabled"'


def _row_controls(prefix: str, i: int, foods: list[tuple[str, str]], food: str, count: str, enabled: bool,
                  autopost: bool) -> str:
    """The select/checkbox cells shared by both grids (ids like the real ASP.NET ones)."""
    dis = "" if enabled else DISABLED
    post = ' onchange="this.form.submit()"' if autopost else ""
    return f"""
      <select id="{prefix}_drpSelf_{i}" name="self{i}"{dis}>{_opts([("1", "مرکزی برادران")] if enabled else [("-1", "-")], "1")}</select>
    </td><td>
      <select id="{prefix}_drpFood_{i}" name="food{i}"{post}{dis}>{_opts(foods or [("-1", "-")], food)}</select>
    </td><td>
      <select id="{prefix}_drpFoodCount_{i}" name="count{i}"{dis}>{_opts([(str(n), str(n)) for n in range(1, 4)] if enabled else [("0", "0")], count)}</select>
    </td><td>0</td><td>0</td>"""


@app.api_route("/FoodBasket/FoodBasketBuy.aspx", methods=["GET", "POST"])
async def basket(request: Request):
    """Like the real basket page: one row per meal, labelled with a span; a meal
    with nothing in the basket is a disabled row; the date box changes only
    when the arrow (image button) next to it is pressed."""
    if request.cookies.get("auth") != "ok":
        return RedirectResponse("/", 302)
    f = dict(await request.form()) if request.method == "POST" else {}
    state["basket_loads"] += 1
    date = f.get("shown_date", "1405/08/02")
    if "go.x" in f or "go" in f:  # <input type=image name=go> posts go.x / go.y
        date, f = f.get("txtDate", date), {}
    message = ""
    if f.get("btnSave"):
        for i, meal in enumerate(MEALS):
            food, count = f.get(f"food{i}", "-1"), int(f.get(f"count{i}", "0"))
            if f.get(f"chk{i}") and food != "-1" and count > 0:
                left = STOCK.get(date, {}).get(meal, {}).get(food, 0)
                if left >= count:
                    STOCK[date][meal][food] -= count
                    PURCHASES.append((date, meal, food, count))
                    message = "خرید با موفقیت انجام شد"
                else:
                    message = "موجودی کافی نیست"
        f = {}
    rows = ""
    prefix = "cphMain_grdFoodBasketReservation"
    for i, meal in enumerate(MEALS):
        foods = [(k, f"{k} 250000 ریال ({v})") for k, v in STOCK.get(date, {}).get(meal, {}).items() if v > 0]
        enabled = bool(foods)
        food = f.get(f"food{i}", foods[0][0] if foods else "-1")
        rows += f"""<tr{'' if enabled else ' class="aspNetDisabled"'}>
          <td><input type="checkbox" id="{prefix}_chkReserve_{i}" name="chk{i}"{" checked" if f.get(f"chk{i}") else ""}{'' if enabled else DISABLED}></td>
          <td><span id="{prefix}_lblFoodType_{i}">{meal}</span></td><td>
          {_row_controls(prefix, i, foods, food, f.get(f"count{i}", "1"), enabled, True)}
          <td>{'' if enabled else f'<img id="{prefix}_imgStaus_{i}" title="غیرفعال">'}</td></tr>"""
    return HTMLResponse(f"""<html><body><form method="post">
      {PROFILE_SNIPPET}
      <label id="cphMain_messageBoxValue">{message}</label>
      <input type="hidden" name="shown_date" value="{date}">
      <input type="text" id="cphMain_txtDate" name="txtDate" value="{date}">
      <input type="image" id="cphMain_imgbtnGoToDate" name="go" src="/next.png" style="width:30px;height:30px">
      <table id="{prefix}"><tr><th>رزرو</th><th>وعده غذایی</th><th>سلف</th><th>غذا</th><th>تعداد</th><th>کالری</th><th>مبلغ خرید</th><th>وضعیت</th></tr>{rows}</table>
      <input type="submit" id="cphMain_btnSave" name="btnSave" value="ذخیره"></form>{LONG_POLL}</body></html>""")


# Weekly reservation page. WEEKS lists the weeks the site can show; tests fill
# MENU: date -> meal -> {"foods": [...], "reserved": food or None, "locked": bool}.
WEEKS = [
    [f"1405/08/{d:02d}" for d in range(2, 9)],
    [f"1405/08/{d:02d}" for d in range(9, 16)],
]
MENU: dict[str, dict[str, dict]] = {}
HIDE_GRIDS: set[str] = set()  # grid suffixes left out of the page, e.g. {"Lunch"}
WEEK_SAVES: list[tuple[str, str]] = []  # (date, meal) reserved with a weekly save button


@app.api_route("/Reservation/Reservation.aspx", methods=["GET", "POST"])
async def reservation(request: Request):
    if request.cookies.get("auth") != "ok":
        return RedirectResponse("/", 302)
    f = dict(await request.form()) if request.method == "POST" else {}
    week = int(f.get("week", "0"))
    if "next.x" in f or "next" in f:
        week = min(week + 1, len(WEEKS) - 1)
    if "prev.x" in f or "prev" in f:
        week = max(week - 1, 0)
    days = WEEKS[week]
    for meal, suffix in zip(MEALS, ("Breakfast", "Lunch", "Dinner")):
        if f.get(f"btnSave{suffix}"):  # save one meal's ticked days, like the real per-tab buttons
            for i, day in enumerate(days):
                info = MENU.get(day, {}).get(meal)
                if state.get("credit_days") is not None and state["credit_days"] <= 0:
                    break  # out of credit: the real site saves only the days it can pay for
                if f.get(f"{suffix}chk{i}") and info and info.get("foods") and not info.get("reserved"):
                    info["reserved"] = info["foods"][0]
                    WEEK_SAVES.append((day, meal))
                    if state.get("credit_days") is not None:
                        state["credit_days"] -= 1
    # Like the real page: a CSS tab strip, one meal per tab, lunch open by default.
    grids = '<style>.css3-tabstrip > div { display: none } .css3-tabstrip > input:checked + label + div { display: block }</style><div class="css3-tabstrip">'
    for n, (meal, suffix) in enumerate(zip(MEALS, ("Breakfast", "Lunch", "Dinner"))):
        if suffix in HIDE_GRIDS:
            continue
        prefix = f"cphMain_grdReservation{suffix}"
        rows = ""
        for i, day in enumerate(days):
            info = MENU.get(day, {}).get(meal, {})
            foods = [(str(n), f"{name} 250000 ریال") for n, name in enumerate(info.get("foods", []), 1)]
            reserved = info.get("reserved")
            chosen = next((v for v, t in foods if reserved and t.startswith(reserved)), foods[0][0] if foods else "-1")
            checked = ' checked="checked"' if reserved else ""
            status = '<img id="{prefix}_imgStaus_{i}" title="رزرو شده">' if reserved else ""
            rows += f"""<tr>
              <td><input type="checkbox" id="{prefix}_chkReserve_{i}" name="{suffix}chk{i}"{checked}{'' if not info.get('locked') else DISABLED}></td>
              <td>{day}</td><td>
              {_row_controls(prefix, i, foods, chosen, "1", not info.get('locked'), False)}
              <td>{status}</td></tr>"""
        grids += (f'<input type="radio" name="tabstrip" id="css3-tabstrip-0-{n}"{" checked" if suffix == "Lunch" else ""}>'
                  f'<label for="css3-tabstrip-0-{n}">{meal}</label><div id="tabs-{n + 1}"><table id="{prefix}">{rows}</table>'
                  f'<input type="submit" id="cphMain_btnSave{suffix}" name="btnSave{suffix}" value="ذخیره"></div>')
    grids += "</div>"
    return HTMLResponse(f"""<html><body><form method="post">
      {PROFILE_SNIPPET}
      <label id="cphMain_lblWeekDate">{days[0]} الی {days[-1]}</label>
      <input type="hidden" name="week" value="{week}">
      <input type="image" id="cphMain_imgbtnLastWeek" name="prev" src="/prev.png">
      <input type="image" id="cphMain_imgbtnNextWeek" name="next" src="/next.png">
      {grids}</form>{LONG_POLL}</body></html>""")
