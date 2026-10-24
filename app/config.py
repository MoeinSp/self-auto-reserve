"""Runtime settings, read from environment variables (see .env.example)."""
import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DATA_DIR = Path(_env("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"

# Base URL of the Cullinan feeding system (with trailing slash).
SITE_URL = _env("SITE_URL", "https://nut.uk.ac.ir/")
BASKET_PATH = _env("BASKET_PATH", "FoodBasket/FoodBasketBuy.aspx")

# Used for session cookies and for encrypting stored site passwords.
# Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"
SECRET_KEY = _env("SECRET_KEY", "")
INVITE_CODE = _env("INVITE_CODE", "")

# --- Rate limiting (protects the account from being blocked) ---
# Seconds between two polling rounds (each round = reload basket page, pick
# date/meal, read the food list) for one account. Clamped to the floor.
MIN_POLL_FLOOR = 5.0
POLL_SECONDS = max(MIN_POLL_FLOOR, float(_env("POLL_SECONDS", "20")))
# Seconds between postbacks inside one round (pick date -> pick meal -> save).
MIN_STEP_FLOOR = 1.0
STEP_SECONDS = max(MIN_STEP_FLOOR, float(_env("STEP_SECONDS", "1.5")))
# Random extra delay, as a fraction of the interval (0.4 => +0..40%).
POLL_JITTER = float(_env("POLL_JITTER", "0.4"))
# Hard cap on requests per account per hour (all kinds together).
MAX_REQUESTS_PER_HOUR = int(_env("MAX_REQUESTS_PER_HOUR", "500"))
# Login attempts (each one includes a captcha) per account per hour.
MAX_LOGINS_PER_HOUR = int(_env("MAX_LOGINS_PER_HOUR", "6"))

# Captcha: OCR attempts before falling back to asking the user in the panel.
CAPTCHA_OCR_TRIES = int(_env("CAPTCHA_OCR_TRIES", "3"))
CAPTCHA_MANUAL_TIMEOUT = int(_env("CAPTCHA_MANUAL_TIMEOUT", "300"))

HEADLESS = _env("HEADLESS", "1") != "0"
# Optional: use an already-installed Chromium instead of Playwright's own.
CHROMIUM_PATH = _env("CHROMIUM_PATH", "") or None
