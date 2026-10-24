# self-auto-reserve

## About
Bot + web panel that auto-buys meals from the "food basket" (سبد غذایی) of the
Cullinan feeding system at https://nut.uk.ac.ir/ (ASP.NET WebForms). Python,
FastAPI, Playwright, SQLite. See README.md (Persian) for architecture.

- The real site is not reachable from the cloud sandbox; test against
  `tests/fake_site.py`. Selectors for the real site live in `SEL` in
  `app/cullinan.py`; refine them from `tools/dump_pages.py` output.
- Never weaken the rate limits in `app/ratelimit.py` / `app/config.py`
  (the user's account can be banned).

## Conventions
- Communicate with the user in Persian (Farsi); keep code, identifiers and commit messages in English.
- Default branch: `main`.

## Commands
- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- Tests: `.venv/bin/python -m unittest tests.test_flow`
  (in the cloud sandbox set `CHROMIUM_PATH=/opt/pw-browsers/chromium-1243/chrome-linux64/chrome`)
- Run: `SECRET_KEY=... .venv/bin/uvicorn app.web:app --port 8000`
