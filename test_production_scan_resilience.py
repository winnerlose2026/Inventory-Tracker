#!/usr/bin/env python3
"""The production scan must not go quiet when Graph hiccups.

The old code did a single bare urlopen per page and, on ANY exception,
appended "page N: parse error" and broke out of that mailbox. A momentary 429
on JD@ -- the only mailbox Daily Production sheets arrive in -- therefore
skipped the entire inbox for that run, blamed the wrong layer (nothing had
been parsed yet), and logged nothing. Combined with nothing scheduling the
scan at all, the tab sat ten weeks behind with no visible signal.
"""
import sys
import urllib.error

sys.path.insert(0, ".")

import blueprints.production as prod


class _Resp:
    def __init__(self, payload):
        self._p = payload.encode()
    def read(self):
        return self._p
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _http(code, retry_after=None):
    hdrs = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError("u", code, "err", hdrs, None)


def _patch(monkeypatch, seq):
    """seq: list of Exception-or-payload; each call pops the next."""
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        i = calls["n"]; calls["n"] += 1
        item = seq[min(i, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return _Resp(item)
    import urllib.request as _ureq
    monkeypatch.setattr(_ureq, "urlopen", fake_urlopen)
    import time
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    return calls


def test_transient_429_is_retried_then_succeeds(monkeypatch):
    calls = _patch(monkeypatch, [_http(429, retry_after=0), '{"value": [1,2]}'])
    page = prod._graph_page("https://x", "tok")
    assert page == {"value": [1, 2]}
    assert calls["n"] == 2


def test_transient_503_is_retried(monkeypatch):
    calls = _patch(monkeypatch, [_http(503), _http(503), '{"value": []}'])
    assert prod._graph_page("https://x", "tok") == {"value": []}
    assert calls["n"] == 3


def test_timeout_is_retried(monkeypatch):
    calls = _patch(monkeypatch, [TimeoutError("timed out"), '{"value": []}'])
    assert prod._graph_page("https://x", "tok") == {"value": []}
    assert calls["n"] == 2


def test_gives_up_after_the_attempt_budget(monkeypatch):
    calls = _patch(monkeypatch, [_http(429, retry_after=0)])
    try:
        prod._graph_page("https://x", "tok", attempts=3)
        assert False, "should have raised"
    except RuntimeError as exc:
        assert "429" in str(exc)
    assert calls["n"] == 3


def test_a_permanent_400_is_not_retried(monkeypatch):
    """An InefficientFilter or a bad query will never succeed on retry;
    burning the budget on it just delays the report."""
    calls = _patch(monkeypatch, [_http(400)])
    try:
        prod._graph_page("https://x", "tok")
        assert False, "should have raised"
    except RuntimeError as exc:
        assert "400" in str(exc)
    assert calls["n"] == 1


def test_401_is_not_retried(monkeypatch):
    calls = _patch(monkeypatch, [_http(401)])
    try:
        prod._graph_page("https://x", "tok")
        assert False
    except RuntimeError:
        pass
    assert calls["n"] == 1


def test_error_message_carries_no_exception_text(monkeypatch):
    """CodeQL py/stack-trace-exposure: the scan report is returned as JSON, so
    the classified message must not embed str(exc)."""
    _patch(monkeypatch, [_http(500)])
    try:
        prod._graph_page("https://x", "tok", attempts=1)
    except RuntimeError as exc:
        assert str(exc) == "HTTP 500"
