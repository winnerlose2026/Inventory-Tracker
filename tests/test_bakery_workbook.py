"""Bakery "Sales v. Labor" workbook on the server (review 2026-11-12).

It used to be ingested only from the owner's laptop off a Dropbox copy; the
week of every sheet was a 7-day walk from 2021-07-19, so one inserted sheet
silently re-dated everything after it, and a truncated file was either a
BadZipFile or a partial overwrite."""
import io
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, ".")

import openpyxl
import pytest

import inventory_tracker as it
from core import feed_health
from integrations import bakery_workbook as bw

H = {"X-Inventory-Token": "tok"}


def _book(names, total=1000.0):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for i, n in enumerate(names):
        ws = wb.create_sheet(n)
        ws["I3"] = total + i
        for j, col in enumerate("BCDEFGH"):
            ws[col + "10"] = 100.0 + j          # labor $
            ws[col + "11"] = 8.0 + j            # labor hours
        ws["N3"] = "Wholesale"; ws["O3"] = total + i
        ws["O10"] = 55.5
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


GOOD = ["10.19.26 - 10.25.26", "10.26.26 - 11.1.26", "11.2.26 - 11.8.26"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    old = it.DATA_DIR
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    for name in dir(it):
        v = getattr(it, name)
        if isinstance(v, Path) and (v.parent == old or name.endswith("_FILE")):
            monkeypatch.setattr(it, name, tmp_path / v.name)
    it._FILE_CACHE.clear()
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    from app import app
    from blueprints import reporting as RP
    monkeypatch.setattr(RP, "business_today", lambda: date(2026, 11, 12))
    yield app.test_client()
    it._FILE_CACHE.clear()


def _up(c, data, **kw):
    r = c.post("/api/admin/bakery-workbook/upload" + ("?force=1" if kw.get("force") else ""),
               data=data, headers=dict(H, **{"Content-Type": "application/octet-stream"}))
    return r.status_code, r.get_json()


# --- week dating -------------------------------------------------------------

def test_dated_sheet_names_are_trusted_over_the_walk():
    weeks, problems, notes = bw.resolve_weeks(GOOD)
    assert [w[0].isoformat() for w in weeks] == ["2026-10-19", "2026-10-26", "2026-11-02"]
    assert problems == [] and notes == []          # the walk would have said 2021


def test_undated_names_follow_their_neighbours_and_roll_the_year():
    weeks, problems, _ = bw.resolve_weeks(["7.19 - 7.25", "7.26 - 8.1"])
    assert problems == [] and [w[0] for w in weeks] == [date(2021, 7, 19), date(2021, 7, 26)]
    weeks, problems, _ = bw.resolve_weeks(["12.20.21 - 12.26.21", "12.27 - 1.2", "1.3 - 1.9"])
    assert problems == []
    assert weeks[1][0] == date(2021, 12, 27) and weeks[2][0] == date(2022, 1, 3)
    # an undated name that cannot sit near its neighbours is drift
    _, problems, _ = bw.resolve_weeks(["7.19 - 7.25", "12.27 - 1.2"])
    assert any("cannot place" in p for p in problems)


def test_a_missing_week_is_a_note_not_an_error():
    _, problems, notes = bw.resolve_weeks(["10.19.26 - 10.25.26", "11.2.26 - 11.8.26"])
    assert problems == [] and "1 week(s) missing" in notes[0]


@pytest.mark.parametrize("names,needle", [
    (["10.19.26 - 10.25.26", "10.19.26 - 10.25.26"], "is also sheet"),
    (["10.20.26 - 10.26.26"], "not a Monday"),
    (["10.26.26 - 11.1.26", "10.19.26 - 10.25.26"], "before the sheet to its left"),
    (["10.19.26 - 10.26.26"], "not 6 days after"),
])
def test_date_drift_is_reported(names, needle):
    _, problems, _ = bw.resolve_weeks(names)
    assert any(needle in p for p in problems), problems


def test_drift_aborts_the_parse_unless_forced():
    data = _book(["10.19.26 - 10.25.26", "10.19.26 - 10.25.26"])
    with pytest.raises(bw.WorkbookError) as e:
        bw.parse_workbook_detailed(data)
    assert e.value.problems
    res = bw.parse_workbook_detailed(data, force=True)
    assert res["problems"] and res["weeks"]


def test_truncated_and_non_xlsx_files_are_rejected_clearly():
    data = _book(GOOD)
    with pytest.raises(bw.WorkbookError, match="cut short"):
        bw.validate_xlsx_bytes(data[: len(data) - 200])
    with pytest.raises(bw.WorkbookError, match="not an .xlsx"):
        bw.validate_xlsx_bytes(b"<html>502 Bad Gateway</html>")
    with pytest.raises(bw.WorkbookError, match="empty"):
        bw.validate_xlsx_bytes(b"")
    bw.validate_xlsx_bytes(data)


def test_laptop_script_still_parses_through_the_shared_module(tmp_path):
    import scripts.ingest_bakery_xlsx as script
    p = tmp_path / "wb.xlsx"
    p.write_bytes(_book(GOOD))
    labor, weeks = script.parse_workbook(p)
    assert len(weeks) == 3 and len(labor) == 21 and weeks[-1]["week_start"] == "2026-11-02"
    assert script.SOURCE_TAG == "bakery-xlsx"


# --- upload endpoint ---------------------------------------------------------

def test_upload_ingests_reports_and_records_health(client):
    code, out = _up(client, _book(GOOD))
    assert code == 200 and out["ok"] and out["unchanged"] is False
    assert (out["weeks"], out["labor_rows"], out["sales_rows"]) == (3, 21, 3)
    assert out["data_through"] == "2026-11-02"
    assert len(it.load_bakery_sales()) == 3 and len(it.load_labor()) == 21
    h = feed_health.snapshot()["feeds"]["bakery-workbook"]
    assert h["ok"] and h["data_through"] == "2026-11-02"
    st = client.get("/api/admin/bakery-workbook/status", headers=H).get_json()
    assert st["data_through"] == "2026-11-02" and st["uploaded_at"]
    assert st["days_since_data_through"] == 4 and st["stale"] is False


def test_multipart_upload_and_same_file_is_skipped(client):
    data = _book(GOOD)
    r = client.post("/api/admin/bakery-workbook/upload", headers=H,
                    data={"file": (io.BytesIO(data), "Bakery Model.xlsx")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["weeks"] == 3
    it.save_labor([])                               # prove the second call writes nothing
    code, out = _up(client, data)
    assert out["ok"] and out["unchanged"] is True and it.load_labor() == []
    code, out = _up(client, data, force=True)
    assert out["unchanged"] is False and len(it.load_labor()) == 21


def test_truncated_upload_changes_nothing(client):
    _up(client, _book(GOOD))
    data = _book(GOOD + ["11.9.26 - 11.15.26"])
    code, out = _up(client, data[: len(data) // 2])
    assert code == 400 and out["ok"] is False and "cut short" in out["error"]
    assert len(it.load_bakery_sales()) == 3
    assert feed_health.snapshot()["feeds"]["bakery-workbook"]["ok"] is False


def test_fewer_weeks_than_stored_is_refused_unless_forced(client):
    _up(client, _book(GOOD))
    code, out = _up(client, _book(GOOD[:2]))
    assert code == 409 and "delete history" in out["error"]
    assert len(it.load_bakery_sales()) == 3
    code, out = _up(client, _book(GOOD[:2]), force=True)
    assert code == 200 and len(it.load_bakery_sales()) == 2


def test_date_drift_upload_is_refused_with_the_reasons(client):
    code, out = _up(client, _book(["10.19.26 - 10.25.26", "10.20.26 - 10.26.26"]))
    assert code == 400 and out["problems"] and it.load_bakery_sales() == []


def test_chart_reports_data_through_and_goes_stale_after_ten_days(client, monkeypatch):
    from blueprints import reporting as RP
    _up(client, _book(GOOD))
    j = client.get("/api/report/plh?grain=week", headers=H).get_json()
    assert j["bakery_data"]["data_through"] == "2026-11-02" and not j["bakery_data"]["stale"]
    monkeypatch.setattr(RP, "business_today", lambda: date(2026, 11, 19))
    j = client.get("/api/report/plh?grain=week", headers=H).get_json()
    assert j["bakery_data"]["stale"] and j["bakery_data"]["days_since_data_through"] == 11


def test_ingest_endpoints_take_the_data_lock(client, monkeypatch):
    seen = []
    real = it.data_lock

    def spy():
        seen.append(1)
        return real()
    monkeypatch.setattr(it, "data_lock", spy)
    for path, body in (("/api/admin/labor/ingest", {"entries": [{"date": "2026-11-10", "hours": 8}]}),
                       ("/api/admin/bakery-sales/ingest", {"entries": [{"week_start": "2026-11-02", "total": 5}]}),
                       ("/api/sales/ingest", {"rows": []})):
        n = len(seen)
        assert client.post(path, json=body, headers=H).status_code == 200
        assert len(seen) > n, path


def test_forecast_decrement_backfills_a_missed_day_once(client):
    it.save_inventory({"plain": {"name": "Plain Bagel", "quantity": 70.0, "weekly_usage": 70.0,
                                 "unit": "cs", "warehouse": "Alcoa, TN"}})
    r = client.post("/api/forecast/decrement-daily", json={"date": "2026-11-10"}, headers=H).get_json()
    assert r["date"] == "2026-11-10" and len(r["applied"]) == 1
    r2 = client.post("/api/forecast/decrement-daily", json={"date": "2026-11-10"}, headers=H).get_json()
    assert r2["applied"] == [] and r2["skipped_existing"] == ["Plain Bagel"]
    assert it.load_inventory()["plain"]["quantity"] == 60.0
    # the back-fill does not use up today's slot
    r3 = client.post("/api/forecast/decrement-daily", json={}, headers=H).get_json()
    assert len(r3["applied"]) == 1
    bad = client.post("/api/forecast/decrement-daily", json={"date": "11/10/2026"}, headers=H)
    assert bad.status_code == 400
