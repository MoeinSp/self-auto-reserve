"""Minimal Gregorian <-> Jalali conversion and Persian calendar utilities."""
import datetime as dt
import re

PERSIAN_WEEKDAYS = ("شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه")


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


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> dt.date:
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += 186 + (jm - 7) * 30
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        days -= 1
        gy += (days // 365)
        days %= 365
    gd = days + 1
    sal_a = [0, 31, 29 if ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > sal_a[gm]:
        gd -= sal_a[gm]
        gm += 1
    return dt.date(gy, gm, gd)


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


def date_to_gregorian(date_str: str) -> dt.date | None:
    norm_d = normalize_date(date_str)
    if not norm_d:
        return None
    y, m, d = (int(x) for x in norm_d.split("/"))
    return jalali_to_gregorian(y, m, d)


def weekday_name(date_input: str | dt.date) -> str:
    """Return Persian day of week (شنبه تا جمعه)."""
    if isinstance(date_input, str):
        g = date_to_gregorian(date_input)
        if not g:
            return ""
    else:
        g = date_input
    # Python weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    # Persian index: Sat=0, Sun=1, Mon=2, Tue=3, Wed=4, Thu=5, Fri=6
    idx = (g.weekday() + 2) % 7
    return PERSIAN_WEEKDAYS[idx]


def format_ts(timestamp: float) -> str:
    """Unix time -> e.g. '۲ آبان ۱۴:۴۰:۵۸' (Iran time); the year only if not this year."""
    t = dt.datetime.fromtimestamp(timestamp, IRAN_TZ)
    y, m, d = to_jalali(t.date())
    year = "" if y == to_jalali(now_iran().date())[0] else f" {y}"
    return f"{d} {MONTHS[m - 1]}{year} {t:%H:%M:%S}"


def format_jalali_full(date_str: str) -> str:
    """e.g. '1405/08/02' -> 'شنبه ۲ آبان ۱۴۰۵'"""
    norm_d = normalize_date(date_str)
    if not norm_d:
        return date_str
    y, m, d = (int(x) for x in norm_d.split("/"))
    w = weekday_name(norm_d)
    return f"{w} {d} {MONTHS[m - 1]} {y}" if w else f"{d} {MONTHS[m - 1]} {y}"


def format_jalali_short(date_str: str) -> str:
    """e.g. '1405/08/02' -> 'شنبه ۲ آبان'"""
    norm_d = normalize_date(date_str)
    if not norm_d:
        return date_str
    y, m, d = (int(x) for x in norm_d.split("/"))
    w = weekday_name(norm_d)
    return f"{w} {d} {MONTHS[m - 1]}" if w else f"{d} {MONTHS[m - 1]}"


def get_upcoming_days(days_count: int = 14) -> list[dict]:
    """List of upcoming calendar days starting from today for convenient UI picker."""
    base_g = now_iran().date()
    result = []
    for offset in range(days_count):
        g = base_g + dt.timedelta(days=offset)
        jy, jm, jd = to_jalali(g)
        j_str = f"{jy:04d}/{jm:02d}/{jd:02d}"
        w_name = weekday_name(g)
        rel = "امروز" if offset == 0 else ("فردا" if offset == 1 else ("پس‌فردا" if offset == 2 else ""))
        label = f"{w_name} {jd} {MONTHS[jm - 1]}"
        if rel:
            label += f" ({rel})"
        result.append({
            "date": j_str,
            "day_name": w_name,
            "label": label,
            "short_label": f"{w_name} {jd} {MONTHS[jm - 1]}",
            "is_today": offset == 0,
            "offset": offset,
        })
    return result


def get_next_week_days() -> list[dict]:
    """Days of next Persian week (from upcoming Saturday to Thursday)."""
    base_g = now_iran().date()
    # Days until next Saturday (Sat=0 .. Fri=6)
    cur_p_idx = (base_g.weekday() + 2) % 7
    days_until_next_sat = 7 - cur_p_idx if cur_p_idx > 0 else 7
    next_sat = base_g + dt.timedelta(days=days_until_next_sat)
    result = []
    for i in range(6):  # Saturday to Thursday
        g = next_sat + dt.timedelta(days=i)
        jy, jm, jd = to_jalali(g)
        j_str = f"{jy:04d}/{jm:02d}/{jd:02d}"
        w_name = weekday_name(g)
        result.append({
            "date": j_str,
            "day_name": w_name,
            "label": f"{w_name} {jd} {MONTHS[jm - 1]}",
        })
    return result


def two_weeks_days() -> list[dict]:
    """This week (from Saturday) and next week, for the reservations table."""
    today = now_iran().date()
    start = today - dt.timedelta(days=(today.weekday() + 2) % 7)
    result = []
    for i in range(14):
        g = start + dt.timedelta(days=i)
        jy, jm, jd = to_jalali(g)
        result.append({
            "date": f"{jy:04d}/{jm:02d}/{jd:02d}",
            "day_name": weekday_name(g),
            "short_label": f"{jd} {MONTHS[jm - 1]}",
            "is_today": g == today,
            "is_past": g < today,
            "week": i // 7,
        })
    return result
