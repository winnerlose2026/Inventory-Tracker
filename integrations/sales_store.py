"""Toast sales rows in SQLite (data/sales.sqlite3) instead of data/sales.json.

Why: sales.json reached 52 MB / ~169k rows by 11/2026 and grows ~1,900 rows a
day. Every reader parsed the whole file; the nightly Toast pull plus the
hourly backup parsing it again ran the 512 MB web instance out of memory
(restarts at 08:20 UTC on 11/14, 11/15, 11/16). Here each reader asks for the
date window it needs, and a pull replaces only the (store, day) pairs it got.

One row per (restaurant_guid, business_date, item_guid), the same key the
JSON list used. The nine Toast fields are columns; any other key a caller
passes rides along in ``extra`` (JSON) so nothing is dropped.

The database sits on the same persistent disk as the JSON files. WAL mode +
a busy timeout lets both gunicorn workers read while one writes. Writers that
read-modify-write still hold inventory_tracker.data_lock, as before.

Migration: the first call in a process that finds sales.json and an empty
table imports it (under data_lock) and renames the file to
sales.json.migrated, so nothing re-imports it and a roll-back can still read
it. Backups copy the database with SQLite's online backup (integrations/
backups.py); restoring an OLD snapshot's sales.json imports it here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing

COLUMNS = ("restaurant_guid", "location", "business_date", "item_guid", "item",
           "menu_group", "qty", "gross", "net")
_KEY = ("restaurant_guid", "business_date", "item_guid")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
    restaurant_guid TEXT NOT NULL,
    business_date   TEXT NOT NULL,
    item_guid       TEXT NOT NULL,
    location        TEXT NOT NULL DEFAULT '',
    item            TEXT NOT NULL DEFAULT '',
    menu_group      TEXT NOT NULL DEFAULT '',
    qty             REAL,
    gross           REAL,
    net             REAL,
    extra           TEXT,
    PRIMARY KEY (restaurant_guid, business_date, item_guid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sales_by_date ON sales (business_date);
"""

_ready: set = set()          # db paths already schema-checked + migrated
_ready_lock = threading.Lock()


def _it():
    import inventory_tracker as it
    return it


def db_path():
    return _it().SALES_DB_FILE


def json_path():
    return _it().SALES_FILE


def _connect(path=None) -> sqlite3.Connection:
    path = str(path or db_path())
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _to_record(r: dict) -> tuple:
    extra = {k: v for k, v in r.items() if k not in COLUMNS}
    return (str(r.get("restaurant_guid") or ""), str(r.get("business_date") or "")[:10],
            str(r.get("item_guid") or ""), str(r.get("location") or ""),
            str(r.get("item") or ""), str(r.get("menu_group") or ""),
            _num(r.get("qty")), _num(r.get("gross")), _num(r.get("net")),
            json.dumps(extra, separators=(",", ":")) if extra else None)


def _from_record(t) -> dict:
    d = {"restaurant_guid": t[0], "location": t[3], "business_date": t[1],
         "item_guid": t[2], "item": t[4], "menu_group": t[5],
         "qty": t[6], "gross": t[7], "net": t[8]}
    if t[9]:
        try:
            d.update(json.loads(t[9]))
        except ValueError:
            pass
    return d


_SELECT = ("SELECT restaurant_guid, business_date, item_guid, location, item, "
           "menu_group, qty, gross, net, extra FROM sales")
_INSERT = ("INSERT OR REPLACE INTO sales (restaurant_guid, business_date, item_guid, "
           "location, item, menu_group, qty, gross, net, extra) "
           "VALUES (?,?,?,?,?,?,?,?,?,?)")


def _valid(r) -> bool:
    return (isinstance(r, dict) and r.get("restaurant_guid")
            and r.get("business_date") and r.get("item_guid"))


def ensure_ready() -> None:
    """Create the schema and import sales.json once. Cheap after the first
    call in a process."""
    path = str(db_path())
    if path in _ready:
        return
    with _ready_lock:
        if path in _ready:
            return
        with closing(_connect(path)) as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(_SCHEMA)
            con.commit()
        _migrate_json()
        _ready.add(path)


def _migrate_json() -> int:
    """Import sales.json into an EMPTY table, then rename it. Returns rows."""
    src = json_path()
    if not os.path.exists(src):
        return 0
    it = _it()
    with it.data_lock():
        if not os.path.exists(src):            # another worker did it
            return 0
        with closing(_connect()) as con:
            if con.execute("SELECT 1 FROM sales LIMIT 1").fetchone():
                os.replace(src, str(src) + ".migrated")
                return 0
            n = 0
            batch = []
            for r in iter_json_array(src):
                if _valid(r):
                    batch.append(_to_record(r))
                if len(batch) >= 5000:
                    con.executemany(_INSERT, batch)
                    n += len(batch)
                    batch = []
            con.executemany(_INSERT, batch)
            n += len(batch)
            con.commit()
        os.replace(src, str(src) + ".migrated")
        print(f"[sales_store] imported {n} rows from {os.path.basename(src)}",
              flush=True)
        return n


def iter_json_array(path, chunk: int = 1 << 20):
    """Yield the elements of a top-level JSON array one at a time, reading
    the file in 1 MB chunks -- json.load of the 52 MB sales.json costs
    ~200 MB of RAM, too much for a 512 MB instance at deploy time."""
    dec = json.JSONDecoder()
    buf, pos, started, eof = "", 0, False, False
    with open(path, encoding="utf-8") as f:
        while True:
            # skip whitespace, commas and the opening bracket
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if not started and pos < len(buf):
                if buf[pos] != "[":
                    raise ValueError("not a JSON array")
                started, pos = True, pos + 1
                continue
            if pos < len(buf) and buf[pos] == "]":
                return
            try:
                if pos >= len(buf):
                    raise ValueError("need more")
                obj, end = dec.raw_decode(buf, pos)
            except ValueError:
                if eof:
                    if buf[pos:].strip() in ("", "]"):
                        return
                    raise
                more = f.read(chunk)
                if not more:
                    eof = True
                buf, pos = buf[pos:] + more, 0
                continue
            yield obj
            pos = end


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def load(since: str = "", until: str = "", restaurant_guid: str = "") -> list:
    """Rows with since <= business_date <= until (either bound optional),
    optionally for one store, oldest first."""
    ensure_ready()
    where, args = [], []
    if since:
        where.append("business_date >= ?")
        args.append(since[:10])
    if until:
        where.append("business_date <= ?")
        args.append(until[:10])
    if restaurant_guid:
        where.append("restaurant_guid = ?")
        args.append(restaurant_guid)
    sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "") + \
        " ORDER BY business_date, restaurant_guid, item_guid"
    with closing(_connect()) as con:
        return [_from_record(t) for t in con.execute(sql, args)]


def pairs(since: str = "", until: str = "") -> set:
    """{(restaurant_guid, business_date)} that have at least one row."""
    ensure_ready()
    where, args = [], []
    if since:
        where.append("business_date >= ?")
        args.append(since[:10])
    if until:
        where.append("business_date <= ?")
        args.append(until[:10])
    sql = "SELECT DISTINCT restaurant_guid, business_date FROM sales" + \
        (" WHERE " + " AND ".join(where) if where else "")
    with closing(_connect()) as con:
        return {(g, d) for g, d in con.execute(sql, args)}


def dates() -> list:
    """Distinct business dates with any row, newest first."""
    ensure_ready()
    with closing(_connect()) as con:
        return [d for (d,) in con.execute(
            "SELECT DISTINCT business_date FROM sales ORDER BY business_date DESC")]


def latest_date() -> str:
    ensure_ready()
    with closing(_connect()) as con:
        row = con.execute("SELECT MAX(business_date) FROM sales").fetchone()
    return (row[0] or "") if row else ""


def location_summary() -> list:
    """[{restaurant_guid, location, rows, min_date, max_date}] per store."""
    ensure_ready()
    sql = ("SELECT restaurant_guid, MAX(location), COUNT(*), MIN(business_date), "
           "MAX(business_date) FROM sales GROUP BY restaurant_guid")
    with closing(_connect()) as con:
        return [{"restaurant_guid": g, "location": loc or "", "rows": n,
                 "min_date": lo or "", "max_date": hi or ""}
                for g, loc, n, lo, hi in con.execute(sql)]


def count() -> int:
    ensure_ready()
    with closing(_connect()) as con:
        return con.execute("SELECT COUNT(*) FROM sales").fetchone()[0]


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------

def replace_days(rows: list) -> int:
    """Replace every (store, day) present in ``rows`` wholesale (a Toast pull
    of a day is the whole day). Returns rows written."""
    ensure_ready()
    recs = [_to_record(r) for r in rows or [] if _valid(r)]
    days = {(t[0], t[1]) for t in recs}
    with closing(_connect()) as con:
        con.executemany("DELETE FROM sales WHERE restaurant_guid = ? AND business_date = ?",
                        sorted(days))
        con.executemany(_INSERT, recs)
        con.commit()
    return len(recs)


def upsert(rows: list, *, replace_dates: bool = False) -> tuple:
    """POST /api/sales/ingest semantics: same key overwrites. With
    replace_dates, every (store, day) in ``rows`` is cleared first.
    Returns (added, updated, total)."""
    ensure_ready()
    recs = [_to_record(r) for r in rows or [] if _valid(r)]
    added = updated = 0
    with closing(_connect()) as con:
        if replace_dates:
            con.executemany(
                "DELETE FROM sales WHERE restaurant_guid = ? AND business_date = ?",
                sorted({(t[0], t[1]) for t in recs}))
        seen = set()
        for t in recs:
            key = t[:3]
            hit = key in seen or con.execute(
                "SELECT 1 FROM sales WHERE restaurant_guid=? AND business_date=? "
                "AND item_guid=?", key).fetchone()
            if hit:
                updated += 1
            else:
                added += 1
            seen.add(key)
            con.execute(_INSERT, t)
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    return added, updated, total


def replace_all(rows: list) -> None:
    """Make the table hold exactly ``rows`` (tests, restoring an old JSON
    snapshot)."""
    ensure_ready()
    recs = [_to_record(r) for r in rows or [] if _valid(r)]
    with closing(_connect()) as con:
        con.execute("DELETE FROM sales")
        con.executemany(_INSERT, recs)
        con.commit()


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------

def backup_to(dest) -> bool:
    """Consistent copy of the database at ``dest`` (SQLite online backup:
    safe while the app reads and writes). False when there is no data."""
    ensure_ready()
    if not os.path.exists(str(db_path())):
        return False
    with closing(_connect()) as src, closing(sqlite3.connect(str(dest))) as dst:
        src.backup(dst)
    return True


def restore_from(snapshot_db) -> int:
    """Replace the live table with a snapshot database's rows. Returns rows."""
    ensure_ready()
    with closing(sqlite3.connect(str(snapshot_db))) as snap:
        recs = list(snap.execute(_SELECT))
    with closing(_connect()) as con:
        con.execute("DELETE FROM sales")
        con.executemany(_INSERT, [tuple(r) for r in recs])
        con.commit()
    return len(recs)


_FINGERPRINT_SQL = ("SELECT COUNT(*), MIN(business_date), MAX(business_date), "
                    "ROUND(TOTAL(qty), 3), ROUND(TOTAL(gross), 2), ROUND(TOTAL(net), 2), "
                    "COUNT(DISTINCT restaurant_guid || '|' || business_date) FROM sales")


def fingerprint(path=None) -> tuple:
    """Cheap content summary: equal fingerprints = nothing to restore."""
    if path is None:
        ensure_ready()
    with closing(_connect(path)) as con:
        return tuple(con.execute(_FINGERPRINT_SQL).fetchone())


def describe_db(path) -> dict:
    """{rows, days, first_date, last_date} of a database file."""
    with closing(sqlite3.connect(str(path))) as con:
        n, lo, hi, days = con.execute(
            "SELECT COUNT(*), MIN(business_date), MAX(business_date), "
            "COUNT(DISTINCT business_date) FROM sales").fetchone()
    return {"type": "sqlite", "rows": n, "days": days,
            "first_date": lo or "", "last_date": hi or ""}
