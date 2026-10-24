"""A tiny imitation of the Cullinan login + food-basket pages, for local tests.

It mimics what matters: a captcha login, a date box with AutoPostBack, and a
grid whose dropdowns post back the whole form (like ASP.NET WebForms).
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
        return HTMLResponse(f"<html><body>داشبورد{LONG_POLL}</body></html>")
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


@app.api_route("/FoodBasket/FoodBasketBuy.aspx", methods=["GET", "POST"])
async def basket(request: Request):
    if request.cookies.get("auth") != "ok":
        return RedirectResponse("/", 302)
    f = dict(await request.form()) if request.method == "POST" else {}
    state["basket_loads"] += 1
    date = f.get("txtDate", "1405/08/02")
    alert = ""
    if f.get("btnSave"):
        for i in range(3):
            meal, food, count = f.get(f"ddlMeal{i}", ""), f.get(f"ddlFood{i}", "-"), int(f.get(f"ddlCount{i}", "0"))
            if f.get(f"chk{i}") and food != "-" and count > 0:
                left = STOCK.get(date, {}).get(meal, {}).get(food, 0)
                if left >= count:
                    STOCK[date][meal][food] -= count
                    PURCHASES.append((date, meal, food, count))
                    alert = "خرید با موفقیت انجام شد"
                else:
                    alert = "موجودی کافی نیست"
        f = {"txtDate": date}
    rows = ""
    for i in range(3):
        meal = f.get(f"ddlMeal{i}", "-")
        foods = {k: v for k, v in STOCK.get(date, {}).get(meal, {}).items() if v > 0}
        food = f.get(f"ddlFood{i}", "-")
        rows += f"""<tr>
          <td><input type="checkbox" id="chk{i}" name="chk{i}"{" checked" if f.get(f"chk{i}") else ""}></td>
          <td><select id="ddlMeal{i}" name="ddlMeal{i}" onchange="this.form.submit()">{_opts([("-", "-"), ("صبحانه", "صبحانه"), ("ناهار", "ناهار"), ("شام", "شام")], meal)}</select></td>
          <td><select id="ddlFood{i}" name="ddlFood{i}" onchange="this.form.submit()">{_opts([("-", "-")] + [(k, f"{k} ({v})") for k, v in foods.items()], food)}</select></td>
          <td><select id="ddlCount{i}" name="ddlCount{i}">{_opts([(str(n), str(n)) for n in range(4)], f.get(f"ddlCount{i}", "0"))}</select></td>
          <td>0</td><td>0</td><td>🔒</td></tr>"""
    script = f"<script>alert({alert!r})</script>" if alert else ""
    return HTMLResponse(f"""<html><body><form method="post">
      <input type="text" id="txtDate" name="txtDate" value="{date}" onchange="this.form.submit()">
      <table><tr><th>رزرو</th><th>وعده</th><th>غذا</th><th>تعداد</th><th>کالری</th><th>مبلغ خرید</th><th>وضعیت</th></tr>{rows}</table>
      <input type="submit" name="btnSave" value="ذخیره"></form>{script}{LONG_POLL}</body></html>""")
