"""Scripts no longer exit 0 on a failure (review 2026-11-12, item 11)."""
import importlib
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "scripts"))


def _mod(name):
    return importlib.reload(importlib.import_module(name))


# --- cowork_graph_scan: Houston screenshot pre-check -------------------------

class _Resp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_screenshot_precheck_errors_fail_the_run(monkeypatch, capsys):
    G = _mod("cowork_graph_scan")
    monkeypatch.delenv("USF_SCREENSHOT_SELFTEST", raising=False)
    answer = {"ok": True, "messages": [], "last_count_at": "2026-11-09",
              "errors": ["jd@ms.hhbagels.com/tom.foley@usfoods.com: list: 503"]}
    monkeypatch.setattr(G.urllib.request, "urlopen",
                        lambda req, timeout=180: _Resp(answer))
    assert G._run_screenshot_ocr("https://app", "tok", dry_run=False) == 1
    assert "could not be read completely" in capsys.readouterr().err
    answer["errors"] = []
    assert G._run_screenshot_ocr("https://app", "tok", dry_run=False) == 0


# --- trigger_email_scan ------------------------------------------------------

def _trigger(monkeypatch, payload):
    T = _mod("trigger_email_scan")
    monkeypatch.setenv("APP_URL", "https://app")
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")

    class R(_Resp):
        status = 200
    monkeypatch.setattr(T.urllib.request, "urlopen",
                        lambda req, timeout=300: R(payload))
    return T.main()


def test_trigger_email_scan_exit_codes(monkeypatch):
    ok = {"status": "ok", "distributor": "Email Inbox", "error": None}
    assert _trigger(monkeypatch, {"reports": [ok]}) == 0
    assert _trigger(monkeypatch, {"reports": [{**ok, "status": "error",
                                               "error": "internal error"}]}) == 1
    assert _trigger(monkeypatch, {"reports": [{**ok, "status": "partial",
                                               "mailbox_failures": ["x"]}]}) == 1
    assert _trigger(monkeypatch, {"reports": [ok], "new_failures": [
        {"key": "unmapped:1", "message": "m"}]}) == 1
    assert _trigger(monkeypatch, {"reports": []}) == 1


# --- ingest_cheney_stock_from_endpoint ---------------------------------------

def _cheney(monkeypatch, data, events_by_wh=None, argv=("--commit",)):
    C = _mod("ingest_cheney_stock_from_endpoint")
    monkeypatch.setattr(C, "_get", lambda base, token, path, timeout=90: data)
    posted = []

    def post(base, token, payload, timeout=90):
        posted.append(payload)
        return {"reports": [{"status": "ok", "by_event_type": {}, "updated": 1,
                             "unchanged": 0}]}
    monkeypatch.setattr(C, "_post", post)

    def events_from_image(img, wh, cd, source=""):
        ev, warn = (events_by_wh or {}).get(wh, ([], ["under-read"]))
        return ev, warn, []
    monkeypatch.setattr(C.ocr, "events_from_image", events_from_image)
    monkeypatch.setattr(C.ocr, "fmt_event_row", lambda e: "row")
    rc = C.main(["--api-token", "tok", "--app-url", "https://app", *argv])
    return rc, posted


def _fac(wh):
    return {"warehouse": wh, "count_date": "2026-11-07", "image_b64": "aGk="}


def _ev(wh):
    return {"event_type": "on_hand", "item": {"variety": "Plain", "warehouse": wh}}


def test_cheney_endpoint_errors_are_not_nothing_to_ingest(monkeypatch):
    rc, _ = _cheney(monkeypatch, {"ok": True, "facilities": [],
                                  "errors": ["jd@x: list: 503"]})
    assert rc == 1
    rc, _ = _cheney(monkeypatch, {"ok": True, "facilities": [], "errors": []})
    assert rc == 0


def test_cheney_partly_blocked_commit_is_not_exit_0(monkeypatch):
    data = {"ok": True, "errors": [],
            "facilities": [_fac("Ocala, FL"), _fac("Punta Gorda, FL")]}
    rc, posted = _cheney(monkeypatch, data,
                         {"Ocala, FL": ([_ev("Ocala, FL")], [])})
    assert rc == 1 and len(posted) == 1          # the clean facility committed
    rc, posted = _cheney(monkeypatch, data,
                         {"Ocala, FL": ([_ev("Ocala, FL")], []),
                          "Punta Gorda, FL": ([_ev("Punta Gorda, FL")], [])})
    assert rc == 0 and len(posted[0]["events"]) == 2


# --- cowork_graph_scan local mode --------------------------------------------

def test_local_mode_mailbox_list_failure_is_not_nothing_new(monkeypatch, tmp_path):
    G = _mod("cowork_graph_scan")
    for k in ("MS365_TENANT_ID", "MS365_CLIENT_ID", "MS365_CLIENT_SECRET"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("LOOKBACK_HOURS_OVERRIDE", raising=False)
    monkeypatch.setattr(G, "_graph_token", lambda *a, **k: "t")

    def boom(token, mb, since, verbose=False):
        raise RuntimeError("403 Forbidden")
    monkeypatch.setattr(G, "_list_recent_messages", boom)
    argv = ["--mode", "local", "--app-url", "https://app", "--api-token", "tok",
            "--mailboxes", "jd@ms.hhbagels.com", "--state", str(tmp_path / "seen")]
    assert G.run(argv) == 1
    monkeypatch.setattr(G, "_list_recent_messages", lambda *a, **k: [])
    assert G.run(argv) == 0
