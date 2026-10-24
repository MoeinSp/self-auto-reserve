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
    created_at REAL NOT NULL
);
-- Credentials for the university site; one per panel user.
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    site_username TEXT NOT NULL,
    site_password_enc TEXT NOT NULL
);
-- A "buy from the food basket" job: keep watching one date/meal until a
-- preferred food shows up, then buy it.
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date TEXT NOT NULL,            -- Jalali, e.g. 1405/08/05
    meal TEXT NOT NULL DEFAULT '', -- e.g. ناهار (matched against row dropdowns)
    foods TEXT NOT NULL,           -- one per line, highest priority first; * = anything
    quantity INTEGER NOT NULL DEFAULT 1,
    active_from TEXT NOT NULL DEFAULT '', -- HH:MM daily window ('' = always)
    active_to TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', -- active | done | stopped | failed
    message TEXT NOT NULL DEFAULT '',
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


def init() -> None:
    with tx() as c:
        c.executescript(SCHEMA)


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
