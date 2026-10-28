"""SQLite storage. Small scale (a handful of friends), so plain sqlite3 is enough."""
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
-- Credentials for the university site; multiple allowed per panel user.
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    site_username TEXT NOT NULL,
    site_password_enc TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    credit TEXT NOT NULL DEFAULT '',
    credit_updated_at REAL NOT NULL DEFAULT 0,
    auto_reserve INTEGER NOT NULL DEFAULT 0,
    auto_reserve_meals TEXT NOT NULL DEFAULT 'ناهار,شام',
    last_auto_reserve TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    UNIQUE (user_id, site_username)
);
-- A "buy from the food basket" job: keep watching one date/meal until a
-- preferred food shows up, then buy it.
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    date TEXT NOT NULL,            -- Jalali, e.g. 1405/08/05
    meal TEXT NOT NULL DEFAULT '', -- e.g. ناهار (matched against row dropdowns)
    foods TEXT NOT NULL,           -- one per line, highest priority first; * = anything
    quantity INTEGER NOT NULL DEFAULT 1,
    active_from TEXT NOT NULL DEFAULT '', -- HH:MM daily window ('' = always)
    active_to TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', -- active | done | stopped | failed
    message TEXT NOT NULL DEFAULT '',
    poll_seconds REAL NOT NULL DEFAULT 0,  -- seconds between checks (0 = POLL_SECONDS)
    attempts INTEGER NOT NULL DEFAULT 0,   -- basket checks done so far
    last_check REAL NOT NULL DEFAULT 0,
    skip_if_reserved INTEGER NOT NULL DEFAULT 1, -- don't buy if the meal is already reserved
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    job_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_user ON logs(user_id, id);
-- Menu of a date from the weekly reservation page, read on demand so users can tick foods.
CREATE TABLE IF NOT EXISTS menus (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL DEFAULT 0,
    date TEXT NOT NULL,
    data TEXT NOT NULL,            -- JSON: {meal: {"foods": [...], "reserved": food or "", "locked": bool}}
    fetched_at REAL NOT NULL,
    PRIMARY KEY (user_id, account_id, date)
);
-- Admin-editable runtime settings (rate limits), see app/limits.py.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_sessions (
    token TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    method TEXT NOT NULL,
    fields TEXT NOT NULL,
    bank TEXT NOT NULL,
    amount_rials INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release: (table, column, definition).
MIGRATIONS = [
    ("jobs", "poll_seconds", "REAL NOT NULL DEFAULT 0"),
    ("jobs", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "last_check", "REAL NOT NULL DEFAULT 0"),
    ("jobs", "skip_if_reserved", "INTEGER NOT NULL DEFAULT 1"),
    ("jobs", "account_id", "INTEGER REFERENCES accounts(id) ON DELETE CASCADE"),
    ("accounts", "display_name", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "credit", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "credit_updated_at", "REAL NOT NULL DEFAULT 0"),
    ("accounts", "auto_reserve", "INTEGER NOT NULL DEFAULT 0"),
    ("accounts", "auto_reserve_meals", "TEXT NOT NULL DEFAULT 'ناهار,شام'"),
    ("accounts", "last_auto_reserve", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "created_at", "REAL NOT NULL DEFAULT 0"),
    ("menus", "account_id", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
    # Result of the last login to the university site: '' (never) | ok | error.
    ("accounts", "login_state", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "login_message", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "login_at", "REAL NOT NULL DEFAULT 0"),
]

# An earlier version created user "admin" with this password, published in the
# public repo. Accounts still using it are locked on startup.
LEAKED_ADMIN_PASSWORD = "Admin@Self2026"
LOCKED = "!"  # password_hash value that never verifies


def init() -> None:
    with tx() as c:
        # Check if old accounts table has user_id INTEGER UNIQUE
        acc_info = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()
        if acc_info and ("user_id INTEGER UNIQUE" in acc_info[0] or "user_id INT UNIQUE" in acc_info[0]):
            c.execute("""
                CREATE TABLE accounts_migrated (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    site_username TEXT NOT NULL,
                    site_password_enc TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0,
                    UNIQUE (user_id, site_username)
                )
            """)
            old_cols = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
            if "display_name" in old_cols:
                c.execute("""
                    INSERT OR IGNORE INTO accounts_migrated (id, user_id, site_username, site_password_enc, display_name, created_at)
                    SELECT id, user_id, site_username, site_password_enc, display_name, created_at FROM accounts
                """)
            else:
                c.execute("""
                    INSERT OR IGNORE INTO accounts_migrated (id, user_id, site_username, site_password_enc, display_name, created_at)
                    SELECT id, user_id, site_username, site_password_enc, '', 0 FROM accounts
                """)
            c.execute("DROP TABLE accounts")
            c.execute("ALTER TABLE accounts_migrated RENAME TO accounts")

        # Check if old menus table has PRIMARY KEY without account_id
        menu_info = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='menus'").fetchone()
        if menu_info:
            pk_cols = [r["name"] for r in sorted(c.execute("PRAGMA table_info(menus)").fetchall(), key=lambda r: r["pk"]) if r["pk"] > 0]
            if "account_id" not in pk_cols:
                c.execute("""
                    CREATE TABLE menus_migrated (
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        account_id INTEGER NOT NULL DEFAULT 0,
                        date TEXT NOT NULL,
                        data TEXT NOT NULL,
                        fetched_at REAL NOT NULL,
                        PRIMARY KEY (user_id, account_id, date)
                    )
                """)
                menu_cols = {r["name"] for r in c.execute("PRAGMA table_info(menus)")}
                if "account_id" in menu_cols:
                    c.execute("""
                        INSERT OR IGNORE INTO menus_migrated (user_id, account_id, date, data, fetched_at)
                        SELECT user_id, COALESCE(account_id, 0), date, data, fetched_at FROM menus
                    """)
                else:
                    c.execute("""
                        INSERT OR IGNORE INTO menus_migrated (user_id, account_id, date, data, fetched_at)
                        SELECT user_id, 0, date, data, fetched_at FROM menus
                    """)
                c.execute("DROP TABLE menus")
                c.execute("ALTER TABLE menus_migrated RENAME TO menus")

        c.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            if column not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        # Update existing jobs without an account_id
        c.execute("""
            UPDATE jobs
            SET account_id = (SELECT id FROM accounts WHERE accounts.user_id = jobs.user_id ORDER BY id LIMIT 1)
            WHERE (account_id IS NULL OR account_id = 0)
              AND EXISTS (SELECT 1 FROM accounts WHERE accounts.user_id = jobs.user_id)
        """)

        # Update existing menus without an account_id
        c.execute("""
            UPDATE menus
            SET account_id = (SELECT id FROM accounts WHERE accounts.user_id = menus.user_id ORDER BY id LIMIT 1)
            WHERE (account_id IS NULL OR account_id = 0)
              AND EXISTS (SELECT 1 FROM accounts WHERE accounts.user_id = menus.user_id)
        """)

        _secure_admins(c)


def _secure_admins(c: sqlite3.Connection) -> None:
    from . import security

    row = c.execute("SELECT id, password_hash FROM users WHERE username = 'admin'").fetchone()
    if row and security.verify_password(LEAKED_ADMIN_PASSWORD, row["password_hash"]):
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (LOCKED, row["id"]))

    if config.ADMIN_USERNAME and config.ADMIN_PASSWORD:
        row = c.execute("SELECT id, password_hash FROM users WHERE username = ?", (config.ADMIN_USERNAME,)).fetchone()
        if not row:
            c.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                (config.ADMIN_USERNAME, security.hash_password(config.ADMIN_PASSWORD), time.time()),
            )
        else:
            if not security.verify_password(config.ADMIN_PASSWORD, row["password_hash"]):
                c.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (security.hash_password(config.ADMIN_PASSWORD), row["id"]),
                )
            c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (row["id"],))

    # Nobody who can log in is an admin: promote the oldest usable user.
    if not c.execute("SELECT 1 FROM users WHERE is_admin = 1 AND password_hash != ?", (LOCKED,)).fetchone():
        c.execute(
            "UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users WHERE password_hash != ?)",
            (LOCKED,),
        )


def set_login_state(account_id: int, ok: bool, message: str = "") -> None:
    """Remember whether the last login to the university site worked (shown in the panel)."""
    with tx() as c:
        c.execute(
            "UPDATE accounts SET login_state = ?, login_message = ?, login_at = ? WHERE id = ?",
            ("ok" if ok else "error", message, time.time(), account_id),
        )


def count_attempt(job_id: int, message: str) -> int:
    """Record one basket check for a job; returns the attempt number."""
    with tx() as c:
        c.execute(
            "UPDATE jobs SET attempts = attempts + 1, last_check = ?, message = ?, updated_at = ? WHERE id = ?",
            (time.time(), message, time.time(), job_id),
        )
        return c.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def log(user_id: int, message: str, level: str = "info", job_id: int | None = None) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO logs (user_id, job_id, level, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, job_id, level, message, time.time()),
        )
        # Keep the log table small.
        c.execute(
            "DELETE FROM logs WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM logs WHERE user_id = ? ORDER BY id DESC LIMIT 500)",
            (user_id, user_id),
        )


def set_job_status(job_id: int, status: str, message: str = "") -> None:
    with tx() as c:
        c.execute(
            "UPDATE jobs SET status = ?, message = ?, updated_at = ? WHERE id = ?",
            (status, message, time.time(), job_id),
        )


def set_job_message(job_id: int, message: str) -> None:
    with tx() as c:
        c.execute("UPDATE jobs SET message = ?, updated_at = ? WHERE id = ?", (message, time.time(), job_id))
