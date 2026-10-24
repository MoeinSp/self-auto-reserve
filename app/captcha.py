"""Captcha solving: local OCR first, then the user types it in the panel."""
import asyncio
import io
import logging
import re

from PIL import Image, ImageFilter, ImageOps

from . import config
from .text import norm

log = logging.getLogger(__name__)


def ocr(png: bytes) -> str | None:
    """Try to read a short numeric captcha. Returns None if unsure."""
    try:
        import pytesseract
    except ImportError:
        return None
    img = Image.open(io.BytesIO(png)).convert("L")
    img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    img = img.filter(ImageFilter.MedianFilter(3))
    candidates = []
    for threshold in (140, 110, 170):
        bw = img.point(lambda p, t=threshold: 255 if p > t else 0)
        for src in (bw, ImageOps.invert(bw)):
            try:
                raw = pytesseract.image_to_string(
                    src, config="--psm 7 -c tessedit_char_whitelist=0123456789"
                )
            except Exception as exc:  # tesseract binary missing, etc.
                log.warning("tesseract failed: %s", exc)
                return None
            digits = re.sub(r"\D", "", norm(raw))
            if 4 <= len(digits) <= 6:
                candidates.append(digits)
    if not candidates:
        return None
    # Most common reading across preprocessing variants.
    return max(set(candidates), key=candidates.count)


class ManualCaptchas:
    """Captchas waiting for a human, keyed by user id (shown on the dashboard)."""

    def __init__(self):
        self.images: dict[int, bytes] = {}
        self._futures: dict[int, asyncio.Future] = {}

    def pending(self, user_id: int) -> bool:
        return user_id in self._futures

    async def ask(self, user_id: int, png: bytes) -> str | None:
        fut = asyncio.get_running_loop().create_future()
        self.images[user_id] = png
        self._futures[user_id] = fut
        try:
            return await asyncio.wait_for(fut, config.CAPTCHA_MANUAL_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        finally:
            self.images.pop(user_id, None)
            self._futures.pop(user_id, None)

    def answer(self, user_id: int, text: str) -> bool:
        fut = self._futures.get(user_id)
        if fut and not fut.done():
            fut.set_result(norm(text))
            return True
        return False


manual = ManualCaptchas()
