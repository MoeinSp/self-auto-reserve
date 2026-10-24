"""Log in to the real site and save the basket page for inspection.

Run on a machine that can reach the university site:

    python -m tools.dump_pages --date 1405/08/05

It writes HTML + screenshots into ./dumps/. Nothing is bought. The files
contain your name and student number but not your password.
"""
import argparse
import asyncio
import getpass
from pathlib import Path

from playwright.async_api import async_playwright

from app import captcha, config
from app.cullinan import CullinanClient, _real_options, describe_grid, launch_browser
from app.ratelimit import Throttle

OUT = Path("dumps")


async def ask_captcha(png: bytes, attempt: int) -> str | None:
    if attempt == 1:
        code = await asyncio.to_thread(captcha.ocr, png)
        if code:
            print(f"OCR read captcha as {code}")
            return code
    (OUT / "captcha.png").write_bytes(png)
    return input(f"Open {OUT / 'captcha.png'} and type the captcha: ").strip()


async def save(page, name: str) -> None:
    (OUT / f"{name}.html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    print(f"saved {name}.html / .png")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Jalali date, e.g. 1405/08/05")
    parser.add_argument("--user", help="student number")
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    username = args.user or input("Student number: ").strip()
    password = getpass.getpass("Site password: ")

    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        client = CullinanClient(browser, username, password, Throttle(), ask_captcha)
        page = await client._goto(config.SITE_URL)
        await save(page, "1_login")
        await client.login()
        await save(client.page, "2_after_login")
        page = await client.open_basket(args.date)
        await save(page, "3_basket")
        try:
            grid = await client.read_grid(page)
        except Exception as exc:
            print("could not read grid:", exc)
            await browser.close()
            return
        report = [describe_grid(grid)]
        # Pick each meal-like option of the first row once, to see how the food list fills.
        slot = client._slot(grid, 0)
        for flt in slot["filters"]:
            for i, opt in enumerate(_real_options(flt)[:4]):
                await client._select(page, flt, opt["value"])
                await save(page, f"4_filter_{flt['id']}_{i}")
                report.append(f"\n--- after choosing {opt['text']!r} in #{flt['id']}\n")
                report.append(describe_grid(await client.read_grid(page)))
        report.append("\ndialogs: " + repr(client.dialogs))
        (OUT / "report.txt").write_text("\n".join(report), encoding="utf-8")
        print("\n".join(report))
        await browser.close()
    print(f"\nDone. Zip the {OUT}/ folder and send it.")


if __name__ == "__main__":
    asyncio.run(main())
