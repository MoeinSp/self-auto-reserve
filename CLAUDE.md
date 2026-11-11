# self-auto-reserve

## About
Bot + web panel that auto-buys meals from the "food basket" (سبد غذایی) of the
Cullinan feeding system at https://nut.uk.ac.ir/ (ASP.NET WebForms). Python,
FastAPI, Playwright, SQLite. See README.md (Persian) for architecture.

- The real site is not reachable from the cloud sandbox; test against
  `tests/fake_site.py`. Selectors for the real site live in `SEL` in
  `app/cullinan.py`; refine them from `tools/dump_pages.py` output.
- Never weaken the default rate limits in `app/config.py` / `app/ratelimit.py`
  (the user's account can be banned). Admins tune them at runtime from
  `/admin`; that goes through `app/limits.py` (stored in the `settings` table).
- Never ship default credentials; admins come from the first sign-up or
  `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
- UI: Jinja templates + `app/static/style.css` + `app/static/app.js` (tabs,
  live refresh of `[data-live]` regions). Mobile-first; check at 390px width.
- On-demand site operations are queued (`Worker.start_op`): one at a time per
  account (they share its site session), different accounts in parallel.
- Meals of today and the next `jalali.LOCK_DAYS` days can't be reserved,
  changed or cancelled; only sold to / bought from the basket.
- If the site shows another page than the one requested (e.g. the messages
  page after sign-in), `CullinanClient._open` asks again and, failing that,
  saves the page under `DATA_DIR/detours/` for tuning selectors.

## Conventions
- Communicate with the user in Persian (Farsi); keep code, identifiers and commit messages in English.
- Default branch: `main`.

## Commands
- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- Tests: `.venv/bin/python -m unittest tests.test_flow`
  (in the cloud sandbox set `CHROMIUM_PATH=/opt/pw-browsers/chromium-1243/chrome-linux64/chrome`)
- Run: `SECRET_KEY=... .venv/bin/uvicorn app.web:app --port 8000`
