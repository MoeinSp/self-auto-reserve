"""Minimal Gregorian -> Jalali conversion (avoids an extra dependency)."""
import datetime as dt
import re


def to_jalali(g: dt.date) -> tuple[int, int, int]:
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy, gm, gd = g.year, g.month, g.day
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + gdm[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def today_str() -> str:
    y, m, d = to_jalali(dt.date.today())
    return f"{y:04d}/{m:02d}/{d:02d}"


def normalize_date(text: str) -> str | None:
    """Accept 1405/8/5, 1405-08-05, Persian digits... -> '1405/08/05'."""
    from .text import norm

    m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", norm(text))
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}/{mo:02d}/{d:02d}"
