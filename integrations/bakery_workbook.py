"""Parse the "Bakery Model - Sales v. Labor" workbook.

The bakery is not on Toast, so this weekly workbook is the only source for the
Report tab's Sales vs Labor chart. It used to be read ONLY by a script on the
owner's laptop (scripts/ingest_bakery_xlsx.py, off a Dropbox copy that is
sometimes truncated); the same parser now also backs
``POST /api/admin/bakery-workbook/upload`` so the server can ingest it with no
laptop involved. Depends on openpyxl only -- no app imports -- so the laptop
scripts can keep importing it.

Each sheet is one Mon-Sun week, named like ``5.11.26 - 5.17.26`` (older
sheets: ``7.19 - 7.25``, no year).

What is read from each sheet
----------------------------
    B3..H3    daily sales (Mon..Sun)
    I3        weekly total sales
    B10..H10  daily labor $ (Mon..Sun)
    B11..H11  daily labor hours (Mon..Sun)
    N3..N8    selling channel names        O3..O8  channel sale $
    O10       SPLH (sales per labor hour)

Week dating
-----------
The sheet NAME is the authority when it carries a date. The old rule walked
7 days per sheet from 2021-07-19 and, when a name disagreed, "trusted the
walk" -- so one inserted, deleted or re-ordered sheet silently re-dated every
later week (and every run replaces ALL stored rows). Now:

  * a name with a year is used as written;
  * a name without a year takes the year that puts it nearest the week after
    the previous sheet;
  * a name that cannot be read continues 7 days after the previous sheet.

Anything that makes a week's date doubtful is DRIFT and aborts the whole
parse (``WorkbookError``) unless ``force=True``: a week that does not start
on a Monday, a duplicate week, a sheet dated before the one to its left, or
an undated name whose month/day cannot be placed near its neighbours.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path

WORKBOOK_START = date(2021, 7, 19)
DOW_COLS = ["B", "C", "D", "E", "F", "G", "H"]
CHANNEL_ROWS = list(range(3, 9))  # N3..N8
SOURCE_TAG = "bakery-xlsx"

_NAME_RE = re.compile(
    r"^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s*-\s*"
    r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s*$"
)


class WorkbookError(ValueError):
    """The workbook cannot be ingested as-is. ``problems`` lists why."""

    def __init__(self, message: str, problems: "list | None" = None):
        super().__init__(message)
        self.problems = list(problems or [])


# ---------------------------------------------------------------------------
# File validation
# ---------------------------------------------------------------------------

def validate_xlsx_bytes(data: bytes) -> None:
    """Raise WorkbookError unless ``data`` is a complete .xlsx.

    The Dropbox copy is regularly cut short mid-sync (the zip's end record is
    missing); openpyxl then fails with an unhelpful BadZipFile, or worse reads
    a stale subset. Check the container before parsing anything.
    """
    if not data:
        raise WorkbookError("The file is empty.")
    if data[:2] != b"PK":
        raise WorkbookError("This is not an .xlsx workbook (it does not start "
                            "like one). Upload the Excel file itself.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            bad = zf.testzip()
    except zipfile.BadZipFile:
        raise WorkbookError(
            "The workbook file is cut short (truncated) -- its end is missing. "
            "This happens when Dropbox has not finished syncing it. Open it in "
            "Excel, save it again, wait for the sync, and upload again.") from None
    if bad:
        raise WorkbookError(f"The workbook file is damaged (part '{bad}' fails "
                            "its checksum). Re-save it in Excel and upload again.")
    if "xl/workbook.xml" not in names:
        raise WorkbookError("This zip file is not an Excel workbook "
                            "(xl/workbook.xml is missing).")


# ---------------------------------------------------------------------------
# Week dating
# ---------------------------------------------------------------------------

def _yr(v):
    if v is None:
        return None
    y = int(v)
    return y + 2000 if y < 100 else y


def _parse_sheet_name(name: str):
    """(start, end) as dates when the name carries explicit years; each is
    None when its year is missing or the date is impossible."""
    m = _NAME_RE.match(name or "")
    if not m:
        return None, None
    sm, sd, sy, em, ed, ey = m.groups()
    start = end = None
    ys, ye = _yr(sy), _yr(ey)
    # "5.11 - 5.17.26": one year written, meant for both ends.
    if ys is None and ye is not None:
        ys = ye - 1 if (int(sm), int(sd)) > (int(em), int(ed)) else ye
    if ye is None and ys is not None:
        ye = ys + 1 if (int(sm), int(sd)) > (int(em), int(ed)) else ys
    if ys:
        try:
            start = date(ys, int(sm), int(sd))
        except ValueError:
            pass
    if ye:
        try:
            end = date(ye, int(em), int(ed))
        except ValueError:
            pass
    return start, end


def _name_month_day(name: str):
    m = _NAME_RE.match(name or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _walk_week(idx: int):
    ws = WORKBOOK_START + timedelta(days=7 * idx)
    return ws, ws + timedelta(days=6)


def resolve_weeks(sheet_names: list) -> "tuple[list, list, list]":
    """Date every sheet. Returns (weeks, problems, notes):

      weeks     [(week_start, week_end, how)] in sheet order; ``how`` is
                "name" | "name-no-year" | "walk"
      problems  drift that must abort the ingest (see module docstring)
      notes     things worth saying that are not errors (a skipped week)
    """
    weeks, problems, notes = [], [], []
    prev = None
    seen: dict = {}
    for idx, name in enumerate(sheet_names):
        expected = (prev + timedelta(days=7)) if prev else _walk_week(idx)[0]
        start, end = _parse_sheet_name(name)
        how = "name"
        if start is None:
            md = _name_month_day(name)
            if md is None:
                start, how = expected, "walk"
                notes.append(f"sheet {idx + 1} {name!r}: name has no date -- "
                             f"taken as the week of {start.isoformat()}")
            else:
                # No year in the name: pick the year that lands nearest the
                # week after the previous sheet.
                cands = []
                for y in (expected.year - 1, expected.year, expected.year + 1):
                    try:
                        cands.append(date(y, md[0], md[1]))
                    except ValueError:
                        pass
                best = min(cands, key=lambda c: abs((c - expected).days)) if cands else None
                if best is None or abs((best - expected).days) > 60:
                    problems.append(
                        f"sheet {idx + 1} {name!r}: cannot place {md[0]}.{md[1]} near "
                        f"{expected.isoformat()} (the week after the sheet before it)")
                    start, how = expected, "walk"
                else:
                    start, how = best, "name-no-year"
        if start.weekday() != 0:
            problems.append(f"sheet {idx + 1} {name!r}: {start.isoformat()} is a "
                            f"{start.strftime('%A')}, not a Monday")
        if end is not None and how == "name" and end != start + timedelta(days=6):
            problems.append(f"sheet {idx + 1} {name!r}: the name's end date "
                            f"{end.isoformat()} is not 6 days after its start")
        if start in seen:
            problems.append(f"sheet {idx + 1} {name!r}: the week of {start.isoformat()} "
                            f"is also sheet {seen[start] + 1} "
                            f"{sheet_names[seen[start]]!r}")
        elif prev is not None and start < prev:
            problems.append(f"sheet {idx + 1} {name!r}: dated {start.isoformat()}, "
                            f"before the sheet to its left ({prev.isoformat()})")
        elif prev is not None and start != expected:
            gap = (start - prev).days // 7 - 1
            notes.append(f"sheet {idx + 1} {name!r}: {gap} week(s) missing before "
                         f"{start.isoformat()}")
        seen.setdefault(start, idx)
        weeks.append((start, start + timedelta(days=6), how))
        prev = start
    return weeks, problems, notes


# ---------------------------------------------------------------------------
# Sheet parsing
# ---------------------------------------------------------------------------

def _num(v) -> float:
    """Best-effort numeric coercion. 0.0 for empty / #DIV/0! sentinels."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("#") or s.upper() in {"NA", "N/A"}:
            return 0.0
        try:
            return float(s.replace(",", "").replace("$", ""))
        except ValueError:
            return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _all_equal(xs, tol: float = 0.05) -> bool:
    """True when every value is within tol of the first -- a formula-spread
    placeholder (weekly_total / 7 in every daily cell), not real actuals."""
    if not xs:
        return False
    base = xs[0]
    return all(abs(x - base) <= tol for x in xs)


def parse_sheet(ws, week_start):
    raw_dol = [_num(ws[col + "10"].value) for col in DOW_COLS]
    raw_hrs = [_num(ws[col + "11"].value) for col in DOW_COLS]
    is_forecast_dollars = _all_equal(raw_dol) and any(v > 0 for v in raw_dol)
    is_forecast_hours = _all_equal(raw_hrs) and any(v > 0 for v in raw_hrs)

    labor_daily = []
    if not is_forecast_dollars and not is_forecast_hours:
        for i, _col in enumerate(DOW_COLS):
            d = week_start + timedelta(days=i)
            hrs, dol = raw_hrs[i], raw_dol[i]
            if hrs <= 0 and dol <= 0:
                continue
            labor_daily.append({"date": d.isoformat(), "hours": round(hrs, 4),
                                "dollars": round(dol, 2)})

    channels = {}
    for r in CHANNEL_ROWS:
        name = ws["N" + str(r)].value
        if not name or not isinstance(name, str):
            continue
        amt = _num(ws["O" + str(r)].value)
        if amt <= 0:
            continue
        channels[name.strip()] = round(amt, 2)

    total = _num(ws["I3"].value)
    splh = _num(ws["O10"].value) or None
    return {
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=6)).isoformat(),
        "labor_daily": labor_daily,
        "channels": channels,
        "total": round(total, 2),
        "splh": round(splh, 4) if splh else None,
    }


def parse_workbook_detailed(source, *, force: bool = False) -> dict:
    """Parse a workbook (a path, bytes or a binary file object).

    Returns {"labor": [...], "weeks": [...], "problems": [...], "notes":
    [...], "sheets": N, "data_through": "<last week_start>" | ""}.
    Raises WorkbookError on a bad file, or on week-date drift unless
    ``force`` (then the best-effort dates are used and the problems are
    returned for the caller to show).
    """
    import openpyxl
    if isinstance(source, (bytes, bytearray)):
        validate_xlsx_bytes(bytes(source))
        source = io.BytesIO(bytes(source))
    elif isinstance(source, (str, Path)):
        data = Path(source).read_bytes()
        validate_xlsx_bytes(data)
        source = io.BytesIO(data)
    # read_only=False is deliberate: parse_sheet reads cells by address, and
    # read-only worksheets re-scan on every random access -- across 260+
    # sheets that took minutes. An eager load is ~5 s; data_only=True returns
    # Excel's cached values.
    try:
        wb = openpyxl.load_workbook(source, data_only=True, read_only=False)
    except WorkbookError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WorkbookError("Excel could not be read from this file "
                            f"({type(exc).__name__}). Re-save it and upload again.") from None

    names = list(wb.sheetnames)
    weeks_dated, problems, notes = resolve_weeks(names)
    if problems and not force:
        raise WorkbookError(
            "The sheet names do not line up into clean Monday weeks, so "
            "nothing was changed. Fix the sheet name(s) below, or upload "
            "again with force if the dates shown are right.", problems)

    labor_entries, bakery_weeks = [], []
    for name, (week_start, _week_end, _how) in zip(names, weeks_dated):
        rec = parse_sheet(wb[name], week_start)
        if not rec["labor_daily"] and rec["total"] <= 0:
            continue
        for L in rec["labor_daily"]:
            labor_entries.append({"date": L["date"], "hours": L["hours"],
                                  "dollars": L["dollars"], "source": SOURCE_TAG})
        bakery_weeks.append({
            "week_start": rec["week_start"], "week_end": rec["week_end"],
            "location": "Bakery", "channels": rec["channels"],
            "total": rec["total"], "splh": rec["splh"], "source": SOURCE_TAG,
        })
    bakery_weeks.sort(key=lambda w: w["week_start"])
    return {
        "labor": labor_entries, "weeks": bakery_weeks,
        "problems": problems, "notes": notes, "sheets": len(names),
        "data_through": bakery_weeks[-1]["week_start"] if bakery_weeks else "",
        "data_through_end": bakery_weeks[-1]["week_end"] if bakery_weeks else "",
    }


def parse_workbook(path, *, force: bool = False):
    """(labor_entries, bakery_weeks) -- the shape the laptop script posts."""
    res = parse_workbook_detailed(path, force=force)
    return res["labor"], res["weeks"]


__all__ = ["WorkbookError", "validate_xlsx_bytes", "resolve_weeks", "parse_sheet",
           "parse_workbook", "parse_workbook_detailed", "SOURCE_TAG",
           "WORKBOOK_START"]
