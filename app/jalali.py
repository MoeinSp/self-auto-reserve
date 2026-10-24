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


# Iran has used a fixed UTC+03:30 since daylight saving was abolished in 2022.
# The server may run in another time zone (UTC), so convert explicitly.
IRAN_TZ = dt.timezone(dt.timedelta(hours=3, minutes=30))
MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
          "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")


def now_iran() -> dt.datetime:
    return dt.datetime.now(IRAN_TZ)


def today_str() -> str:
    y, m, d = to_jalali(now_iran().date())
    return f"{y:04d}/{m:02d}/{d:02d}"


def format_ts(timestamp: float) -> str:
    """Unix time -> e.g. '۲ آبان ۱۴:۴۰:۵۸' (Iran time); the year only if not this year."""
    t = dt.datetime.fromtimestamp(timestamp, IRAN_TZ)
    y, m, d = to_jalali(t.date())
    year = "" if y == to_jalali(now_iran().date())[0] else f" {y}"
    return f"{d} {MONTHS[m - 1]}{year} {t:%H:%M:%S}"


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
