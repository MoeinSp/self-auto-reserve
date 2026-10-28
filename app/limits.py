"""Rate-limit settings an admin can change at runtime.

Overrides are stored in the `settings` table and applied to the `config`
module, which Throttle reads on every request, so changes take effect at once.
"""
from . import config, db

# name: (type, minimum, maximum)
FIELDS = {
    "POLL_SECONDS": (float, 1.0, 3600.0),  # default gap between basket checks
    "MIN_JOB_INTERVAL": (float, 0.5, 60.0),  # fastest interval a user may pick for a job
    "STEP_SECONDS": (float, 0.5, 30.0),  # gap between postbacks inside one check
    "POLL_JITTER": (float, 0.0, 1.0),  # random extra delay, fraction of the gap
    "MAX_REQUESTS_PER_HOUR": (int, 0, 1_000_000),  # 0 = no cap
    "MAX_LOGINS_PER_HOUR": (int, 0, 1000),  # 0 = no cap
}
DEFAULTS = {name: getattr(config, name) for name in FIELDS}


def current() -> dict:
    return {name: getattr(config, name) for name in FIELDS}


def clean(name: str, value) -> float | int:
    kind, lo, hi = FIELDS[name]
    return kind(min(max(float(value), lo), hi))


def load() -> None:
    """Apply stored overrides (called on startup)."""
    with db.tx() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    for row in rows:
        if row["key"] in FIELDS:
            try:
                setattr(config, row["key"], clean(row["key"], row["value"]))
            except ValueError:
                pass


def save(values: dict) -> dict:
    """Validate, store and apply the given settings; returns what was applied."""
    applied = {name: clean(name, v) for name, v in values.items() if name in FIELDS}
    with db.tx() as c:
        for name, v in applied.items():
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (name, str(v)),
            )
    for name, v in applied.items():
        setattr(config, name, v)
    return applied


def reset() -> None:
    """Back to the defaults from the environment."""
    with db.tx() as c:
        c.executemany("DELETE FROM settings WHERE key = ?", [(name,) for name in FIELDS])
    for name, v in DEFAULTS.items():
        setattr(config, name, v)
