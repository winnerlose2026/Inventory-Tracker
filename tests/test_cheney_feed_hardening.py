"""Cheney feed bridge + FTPS inbox: half-uploaded files wait, failures exit
non-zero, a failed rename is reported, the run is recorded for the watchdog,
and a shipment row with no PO number is refused."""
from __future__ import annotations

import ftplib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from integrations import sftp_inbox as S
from integrations import po_health as PH
from test_cheney_feed_bridge import BRIDGE, FIX, ON_HAND_EXAMPLE, FakeTransport, env

NOW = datetime.utcnow()
OLD = NOW - timedelta(minutes=30)


class StatTransport(FakeTransport):
    """FakeTransport with stat(): sizes/mtimes per listing call."""

    def __init__(self, files, listings, fail_move=()):
        super().__init__(files)
        self._listings = list(listings)
        self._fail_move = set(fail_move)

    def stat(self, path):
        return self._listings.pop(0) if len(self._listings) > 1 else self._listings[0]

    def file_away(self, remote_dir, name, processed_dir):
        if name in self._fail_move:
            return False
        return super().file_away(remote_dir, name, processed_dir)


def _run(conn, argv, post=None, **envkw):
    posts = []

    def fake_post(url, data_bytes, token, content_type="text/csv"):
        posts.append((url, data_bytes))
        return post(url) if post else (200, '{"ok": true}')

    real_open, real_post, real_argv = BRIDGE._open_transport, BRIDGE._post, sys.argv
    BRIDGE._open_transport = lambda: conn
    BRIDGE._post = fake_post
    sys.argv = ["sync_cheney_feeds.py", *argv]
    import os
    os.environ["SFTP_SETTLE_SECONDS"] = "0"
    try:
        with env(CHENEY_SFTP_HOST="h", CHENEY_SFTP_USER="u", CHENEY_SFTP_PASSWORD="x",
                 **envkw):
            rc = BRIDGE.main()
    finally:
        os.environ.pop("SFTP_SETTLE_SECONDS", None)
        BRIDGE._open_transport, BRIDGE._post, sys.argv = real_open, real_post, real_argv
    return rc, posts


# ----------------------------------------------------------- stability rule

def test_unstable_reason():
    first = {"a.csv": (100, OLD), "b.csv": (100, OLD), "c.csv": (100, NOW - timedelta(seconds=30)),
             "d.csv": (100, None), "e.csv": (100, OLD)}
    second = {"a.csv": (100, OLD), "b.csv": (250, OLD), "c.csv": (100, NOW - timedelta(seconds=30)),
              "d.csv": (100, None)}
    assert S.unstable_reason("a.csv", first, second, NOW) == ""
    assert "size changed" in S.unstable_reason("b.csv", first, second, NOW)
    assert "may still be uploading" in S.unstable_reason("c.csv", first, second, NOW)
    assert S.unstable_reason("d.csv", first, second, NOW) == ""      # no mtime: size only
    assert "gone" in S.unstable_reason("e.csv", first, second, NOW)
    assert S.parse_mtime("20260803113324.123") == datetime(2026, 8, 3, 11, 33, 24)
    assert S.parse_mtime("") is None


def test_half_uploaded_files_wait_for_the_next_run():
    edi = sorted(FIX.glob("*.EDI"))[:2]
    files = {p.name: p for p in edi}
    a, b = [p.name for p in edi]
    listing1 = {a: (10, OLD), b: (10, OLD)}
    listing2 = {a: (10, OLD), b: (99, OLD)}                 # b is still growing
    conn = StatTransport(files, [listing1, listing2])
    import os
    real = BRIDGE._settle_listing
    try:
        os.environ["SFTP_SETTLE_SECONDS"] = "0.01"
        ready, deferred = BRIDGE._settle_listing(conn, "incoming")
    finally:
        os.environ.pop("SFTP_SETTLE_SECONDS", None)
    assert ready == [a] and deferred[0][0] == b
    # too-young file: skipped, not a failure, not filed
    conn = StatTransport(files, [{a: (10, OLD), b: (10, NOW - timedelta(seconds=20))}])
    rc, posts = _run(conn, ["--commit"])
    assert rc == 0
    assert {n for _, n, _ in conn.filed} == {a}
    assert not any(b in url for url, _ in posts)


# --------------------------------------------------------------- exit codes

def test_non_200_or_ok_false_exits_1_and_the_file_stays():
    files = {ON_HAND_EXAMPLE.name: ON_HAND_EXAMPLE}
    for reply in ((500, "boom"), (200, '{"ok": false, "error": "refused"}')):
        conn = FakeTransport(files)
        rc, posts = _run(conn, ["--commit"],
                         post=lambda url, r=reply: r if "cheney-inventory-csv" in url
                         else (200, '{"ok": true}'))
        assert rc == 1, reply
        assert conn.filed == []
        beat = json.loads([d for u, d in posts if u.endswith("/api/feeds/cheney/heartbeat")][0])
        assert beat["ok"] is False and "last_file_at" not in beat


def test_failed_810_post_exits_1():
    edi = sorted(FIX.glob("*.EDI"))[:1]
    conn = FakeTransport({p.name: p for p in edi})
    rc, posts = _run(conn, ["--commit"],
                     post=lambda url: (503, "") if "cheney-810" in url else (200, "{}"))
    assert rc == 1 and conn.filed == []


def test_failed_rename_is_not_swallowed():
    edi = sorted(FIX.glob("*.EDI"))[:2]
    files = {p.name: p for p in edi}
    conn = StatTransport(files, [{n: (10, OLD) for n in files}], fail_move=[edi[0].name])
    rc, posts = _run(conn, ["--commit"])
    assert rc == 1
    assert {n for _, n, _ in conn.filed} == {edi[1].name}


def test_commit_run_posts_810s_and_a_heartbeat_with_last_file_at():
    edi = sorted(FIX.glob("*.EDI"))
    conn = FakeTransport({p.name: p for p in edi})
    rc, posts = _run(conn, ["--commit"])
    assert rc == 0
    assert sum(1 for u, _ in posts if "/api/ingest/cheney-810?dry_run=0" in u) == 7
    beat = json.loads([d for u, d in posts if u.endswith("/api/feeds/cheney/heartbeat")][0])
    assert beat["ok"] is True and beat["files"] == 7 and beat["last_file_at"]
    # a dry run records nothing
    rc, posts = _run(FakeTransport({p.name: p for p in edi}), [])
    assert rc == 0 and not any("heartbeat" in u for u, _ in posts)
    assert all("dry_run=1" in u for u, _ in posts)


def test_move_to_processed_reports_failure():
    class Ftp:
        def mkd(self, p):
            pass

        def rename(self, a, b):
            raise ftplib.error_perm("550 rename failed")
    assert S._move_to_processed(Ftp(), "incoming/x.csv", "processed", "x.csv") is False

    class Ok(Ftp):
        def rename(self, a, b):
            self.dest = b
    ftp = Ok()
    assert S._move_to_processed(ftp, "incoming/x.csv", "processed", "x.csv") is True
    assert ftp.dest.endswith("_x.csv")


# ------------------------------------------------ heartbeat + stale check

@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    import inventory_tracker as it
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    for attr, fn in (("INVENTORY_FILE", "inventory.json"), ("USAGE_FILE", "usage.json")):
        monkeypatch.setattr(it, attr, tmp_path / fn)
    import app as app_module
    app_module.app.config["TESTING"] = True
    return it, app_module.app.test_client()


def test_stale_feed_is_flagged_only_once_it_has_ever_delivered(app_env):
    it, c = app_env
    from core import feed_health
    h = {"X-Inventory-Token": "tok"}
    now = datetime.now(timezone.utc)
    # runs, but has never handled a file: not live, never stale
    assert c.post("/api/feeds/cheney/heartbeat", json={"ok": True, "files": 0,
                                                       "detail": "nothing"}, headers=h).status_code == 200
    assert PH._check_cheney_feed(now + timedelta(days=30)) == {"live": False}
    assert "cheney-feed-stale" not in feed_health.snapshot()["issues"]
    # a file arrives
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    c.post("/api/feeds/cheney/heartbeat", headers=h,
           json={"ok": True, "files": 3, "last_file_at": stamp, "detail": "3 handled"})
    feed = feed_health.snapshot()["feeds"]["cheney-feed"]
    assert feed["ok"] is True and feed["last_file_at"] == stamp and feed["files"] == 3
    assert PH._check_cheney_feed(now + timedelta(hours=30))["live"] is True
    assert "cheney-feed-stale" not in feed_health.snapshot()["issues"]
    # later empty runs do not move last_file_at; 36 h on, it is stale
    c.post("/api/feeds/cheney/heartbeat", json={"ok": True, "files": 0}, headers=h)
    assert feed_health.snapshot()["feeds"]["cheney-feed"]["last_file_at"] == stamp
    PH._check_cheney_feed(now + timedelta(hours=37))
    assert "cheney-feed-stale" in feed_health.snapshot()["issues"]
    c.post("/api/feeds/cheney/heartbeat", headers=h, json={
        "ok": True, "files": 1,
        "last_file_at": (now + timedelta(hours=37)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    PH._check_cheney_feed(now + timedelta(hours=38))
    assert "cheney-feed-stale" not in feed_health.snapshot()["issues"]


# ------------------------------------------------------ shipments without PO

def test_shipment_row_without_a_po_number_is_refused():
    from integrations.parsers import parse_shipments_csv
    csv = (b"variety,warehouse,quantity,po_number\n"
           b"Plain,Riviera Beach,56,014511763291\n"
           b"Everything,Riviera Beach,24,\n")
    events, errors = parse_shipments_csv("Cheney Brothers", "ship.csv", csv)
    assert [e["po_number"] for e in events] == ["014511763291"]
    assert not any(str(e["po_number"]).startswith("SFTP-") for e in events)
    assert any("row 2: no PO number" in e for e in errors)
