"""scripts/lineage_followups.py -- the lineage-po-followups cron, now in the
repo instead of a $FOLLOWUP_SCRIPT env var. Driven with a fake opener."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import lineage_followups as LF

ENV = {"APP_URL": "https://app.example/", "DELIVERY_API_TOKEN": "dtok"}
SYNC_OK = {"ok": True, "threads": [{}, {}], "registered": ["A1"], "errors": [],
           "confirmed_by_server": [{"applied": True}], "not_in_tracker": []}
SEND_OK = {"ok": True, "emails": [{"sent": True, "po_numbers": ["A1"]}], "sent": 1,
           "unconfirmed_sends": [], "due_without_thread": [], "closed": []}


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(script):
    """script: list of responses (dict), exceptions, or callables, in order."""
    calls = []

    def opener(req, timeout=None):
        calls.append({"url": req.full_url, "headers": dict(req.header_items()),
                      "body": json.loads(req.data.decode())})
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _Resp(step)
    return opener, calls


def _run(script, env=ENV, argv=()):
    opener, calls = _opener(list(script))
    lines = []
    code = LF.run(env=env, argv=list(argv), opener=opener, sleep=lambda s: None,
                  out=lines.append)
    return code, lines, calls


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b'{"ok": false}'))


def test_happy_path_posts_sync_then_send_with_the_token():
    code, lines, calls = _run([SYNC_OK, SEND_OK])
    assert code == 0 and len(lines) == 1 and lines[0].endswith("-- ok")
    assert "sent=1" in lines[0] and "threads=2" in lines[0]
    assert [c["url"] for c in calls] == [
        "https://app.example/api/pos/delivery/sync-threads",
        "https://app.example/api/pos/delivery/send-followups"]
    assert all(c["headers"]["X-inventory-token"] == "dtok" for c in calls)
    assert calls[1]["body"] == {"dry_run": False}


def test_falls_back_to_the_main_token_and_needs_configuration():
    code, _, calls = _run([SYNC_OK, SEND_OK],
                          env={"APP_URL": "https://app.example", "INVENTORY_API_TOKEN": "main"})
    assert code == 0 and calls[0]["headers"]["X-inventory-token"] == "main"
    assert _run([], env={"APP_URL": "https://app.example"})[0] == 2
    assert _run([], env={"DELIVERY_API_TOKEN": "x"})[0] == 2


def test_dry_run_flag_and_env():
    code, lines, calls = _run([SYNC_OK, dict(SEND_OK, sent=0, emails=[{"would_send": True}])],
                              argv=["--dry-run"])
    assert code == 0 and calls[1]["body"] == {"dry_run": True} and "DRY RUN" in lines[0]
    _, _, calls = _run([SYNC_OK, SEND_OK], env=dict(ENV, FOLLOWUP_DRY_RUN="1"))
    assert calls[1]["body"] == {"dry_run": True}


def test_retries_gateway_errors_and_timeouts_twice_then_fails():
    code, _, calls = _run([_http(502), TimeoutError("t"), SYNC_OK, SEND_OK])
    assert code == 0 and len(calls) == 4
    code, lines, calls = _run([_http(503), _http(504), _http(502)])
    assert code == 1 and len(calls) == 3 and "gave up after 3 attempts" in lines[0]
    code, lines, calls = _run([SYNC_OK, urllib.error.URLError("refused"),
                               urllib.error.URLError("refused"),
                               urllib.error.URLError("refused")])
    assert code == 1 and "send-followups" in lines[0]


def test_other_http_errors_fail_at_once():
    code, lines, calls = _run([_http(500)])
    assert code == 1 and len(calls) == 1 and "HTTP 500" in lines[0]
    code, _, calls = _run([_http(401)])
    assert code == 1 and len(calls) == 1


def test_ok_false_and_things_that_need_a_person_exit_non_zero():
    assert _run([{"ok": False, "error": "boom"}])[0] == 1
    code, lines, _ = _run([SYNC_OK, {"ok": False, "error": "threads not synced", "emails": []}])
    assert code == 1 and "ok:false" in lines[0]
    code, lines, _ = _run([SYNC_OK, dict(SEND_OK, unconfirmed_sends=["conv1"])])
    assert code == 1 and "conv1" in lines[0]
    code, lines, _ = _run([SYNC_OK, dict(SEND_OK, due_without_thread=["393072B2"])])
    assert code == 1 and "393072B2" in lines[0]
    code, lines, _ = _run([SYNC_OK, dict(SEND_OK, sent=0, emails=[
        {"po_numbers": ["A1"], "error": "graph createReply: 403", "conversation_id": "c9"}])])
    assert code == 1 and "createReply" in lines[0]
    code, lines, _ = _run([dict(SYNC_OK, errors=["info@: list: boom"]), SEND_OK])
    assert code == 1 and "partial" in lines[0]


def test_field_names_match_the_routes():
    """The script reads these keys; the routes must keep returning them."""
    src = Path(__file__).resolve().parent.parent.joinpath(
        "blueprints", "delivery.py").read_text()
    for key in ('"unconfirmed_sends"', '"due_without_thread"', '"emails"',
                '"registered"', '"confirmed_by_server"', '"not_in_tracker"'):
        assert key in src, key
