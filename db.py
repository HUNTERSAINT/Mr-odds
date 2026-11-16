"""SQLite storage for slips and picks, so the bot can track real results."""
import os
import sqlite3
import time
from datetime import datetime, timezone

import config
import markets

os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
_db = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS slips(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, chat_id INTEGER,
        created_at TEXT, target REAL, total_odds REAL,
        notified INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS picks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slip_id INTEGER REFERENCES slips(id),
        league TEXT, home TEXT, away TEXT,
        kickoff_utc TEXT, pick TEXT, odds REAL, confidence INTEGER,
        status TEXT DEFAULT 'pending',   -- pending | won | lost | void
        score TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_picks_status ON picks(status);
    """
)
_db.commit()


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return _utc_iso(datetime.now(timezone.utc))


def save_slip(user_id: int, chat_id: int, target: float, total: float, legs) -> int:
    cur = _db.execute(
        "INSERT INTO slips(user_id, chat_id, created_at, target, total_odds) VALUES(?,?,?,?,?)",
        (user_id, chat_id, now_iso(), target, round(total, 2)),
    )
    slip_id = cur.lastrowid
    for l in legs:
        m, p = l["match"], l["cand"]
        _db.execute(
            "INSERT INTO picks(slip_id, league, home, away, kickoff_utc, pick, odds, confidence) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (slip_id, m["league"], m["home"], m["away"], _utc_iso(m["kickoff"]),
             p["market"], l["odds"], p["confidence"]),
        )
    _db.commit()
    return slip_id


def matches_to_settle(older_than_iso: str, limit: int = 20):
    """Distinct matches with pending picks whose kickoff is old enough to be finished."""
    return _db.execute(
        "SELECT DISTINCT home, away, kickoff_utc, league FROM picks "
        "WHERE status='pending' AND kickoff_utc < ? ORDER BY kickoff_utc LIMIT ?",
        (older_than_iso, limit),
    ).fetchall()


def settle_match(home: str, away: str, kickoff_utc: str, hg: int, ag: int):
    """Settle every pending pick on this match from the full-time score."""
    rows = _db.execute(
        "SELECT id, pick FROM picks WHERE status='pending' AND home=? AND away=? AND kickoff_utc=?",
        (home, away, kickoff_utc),
    ).fetchall()
    for r in rows:
        _db.execute(
            "UPDATE picks SET status=?, score=? WHERE id=?",
            (markets.evaluate(r["pick"], hg, ag), f"{hg}-{ag}", r["id"]),
        )
    _db.commit()


def void_stale(older_than_iso: str):
    """Give up on picks that still have no result long after kickoff."""
    _db.execute(
        "UPDATE picks SET status='void', score='no result found' "
        "WHERE status='pending' AND kickoff_utc < ?",
        (older_than_iso,),
    )
    _db.commit()


def slip_status(legs) -> str:
    st = [l["status"] for l in legs]
    if "lost" in st:
        return "lost"
    if "pending" in st:
        return "pending"
    if all(s == "void" for s in st):
        return "void"
    return "won"


def legs_of(slip_id: int):
    return _db.execute(
        "SELECT * FROM picks WHERE slip_id=? ORDER BY kickoff_utc", (slip_id,)
    ).fetchall()


def finished_unnotified_slips():
    rows = _db.execute("SELECT * FROM slips WHERE notified=0").fetchall()
    out = []
    for s in rows:
        legs = legs_of(s["id"])
        status = slip_status(legs)
        # a slip with one lost leg is already decided, so it is reported straight away
        if status != "pending":
            out.append((s, legs, status))
    return out


def mark_notified(slip_id: int):
    _db.execute("UPDATE slips SET notified=1 WHERE id=?", (slip_id,))
    _db.commit()


def stats(user_id: int | None = None) -> dict:
    where, args = ("WHERE s.user_id=?", (user_id,)) if user_id else ("", ())
    picks = _db.execute(
        f"SELECT p.status, COUNT(*) c FROM picks p JOIN slips s ON s.id=p.slip_id {where} "
        "GROUP BY p.status",
        args,
    ).fetchall()
    pc = {r["status"]: r["c"] for r in picks}
    slips = _db.execute(f"SELECT id FROM slips s {where}", args).fetchall()
    sc = {"won": 0, "lost": 0, "pending": 0, "void": 0}
    for s in slips:
        sc[slip_status(legs_of(s["id"]))] += 1
    return {"picks": pc, "slips": sc}


def recent_slips(user_id: int, n: int = 5):
    rows = _db.execute(
        "SELECT * FROM slips WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, n)
    ).fetchall()
    return [(s, legs_of(s["id"])) for s in rows]


_db.execute("CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, ts REAL, value TEXT)")
_db.commit()


def cache_get(key: str, max_age: float):
    r = _db.execute("SELECT ts, value FROM cache WHERE key=?", (key,)).fetchone()
    if r and time.time() - r["ts"] < max_age:
        return r["value"]
    return None


def cache_set(key: str, value: str):
    _db.execute("INSERT OR REPLACE INTO cache(key, ts, value) VALUES(?,?,?)", (key, time.time(), value))
    _db.commit()
