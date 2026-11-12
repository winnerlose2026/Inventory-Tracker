"""One rep registry (feature A, 2026-11-12). These tests fail if the scanner's
sender maps or the status page's rep table ever stop matching
integrations/rep_registry.py -- and pin the registry's seed to exactly what
the four hand-kept copies held before it existed."""
from integrations import bagel_inventory_worksheet as W
from integrations import email_scanner as ES
from integrations import rep_registry as RR
from integrations import report_status as RS
from integrations import usfoods_inventory_report as R

from ingest_fakes import isolate

# --- the maps as they were hard-coded on 2026-11-11 ---------------------------
OLD_REPORT_SENDERS = {
    "maria.hernandez@usfoods.com": ("US Foods", "Zebulon, NC"),
    "kathleen.thompson@usfoods.com": ("US Foods", "Zebulon, NC"),
    "jasmin.gomez@usfoods.com": ("US Foods", "Manassas, VA"),
    "thomas.paxson@usfoods.com": ("US Foods", "Manassas, VA"),
    "5o-dl-streetsalescoordination@usfoods.com": ("US Foods", "Manassas, VA"),
    "usfcustomercontact@usfoods.com": ("US Foods", "Manassas, VA"),
    "sam.travlos@usfoods.com": ("US Foods", "La Mirada, CA"),
    "ozzy.corut@usfoods.com": ("US Foods", "La Mirada, CA"),
    "kimberly.cobb@usfoods.com": ("US Foods", "Alcoa, TN"),
    "christy.dunn@usfoods.com": ("US Foods", "Alcoa, TN"),
    "charles.youngblood@usfoods.com": ("US Foods", "Houston, TX"),
    "tom.foley@usfoods.com": ("US Foods", "Houston, TX"),
}
OLD_WORKSHEET_SENDERS = {"michael.via@usfoods.com": ("US Foods", "Chicago, IL")}
OLD_DOMAINS = {
    "cheneybrothers.com": "Cheney Brothers", "cheney.com": "Cheney Brothers",
    "usfoods.com": "US Foods", "usfood.com": "US Foods",
    "chefswarehouse.com": "Chefs Warehouse",
}
_ROSS = [{"name": "Michael Ross", "email": "mross@cheneybrothers.com"}]
OLD_WAREHOUSE_REPS = {
    "Manassas, VA": [
        {"name": "Thomas Paxson", "email": "thomas.paxson@usfoods.com"},
        {"name": "Jasmin Gomez", "email": "jasmin.gomez@usfoods.com"},
        {"name": "USF Manassas coordination",
         "email": "5o-dl-streetsalescoordination@usfoods.com"},
        {"name": "USF Customer Contact (Sales Coordination)",
         "email": "usfcustomercontact@usfoods.com", "no_chase": True},
    ],
    "Zebulon, NC": [
        {"name": "Maria Hernandez", "email": "maria.hernandez@usfoods.com"},
        {"name": "Kathleen Thompson", "email": "kathleen.thompson@usfoods.com"},
    ],
    "La Mirada, CA": [
        {"name": "Sam Travlos", "email": "sam.travlos@usfoods.com"},
        {"name": "Ozzy Corut", "email": "ozzy.corut@usfoods.com"},
    ],
    "Chicago, IL": [{"name": "Michael Via", "email": "michael.via@usfoods.com"}],
    "Alcoa, TN": [
        {"name": "Kim Cobb", "email": "kimberly.cobb@usfoods.com", "no_chase": True},
        {"name": "Christy Dunn", "email": "christy.dunn@usfoods.com", "no_chase": True},
    ],
    "Houston, TX": [
        {"name": "Tom Foley", "email": "tom.foley@usfoods.com"},
        {"name": "Charles Youngblood", "email": "charles.youngblood@usfoods.com",
         "no_chase": True},
    ],
    "Riviera Beach, FL": _ROSS, "Ocala, FL": _ROSS, "Punta Gorda, FL": _ROSS,
}
OLD_DISTRIBUTOR_OF = {
    "Manassas, VA": "US Foods", "Zebulon, NC": "US Foods",
    "La Mirada, CA": "US Foods", "Chicago, IL": "US Foods",
    "Alcoa, TN": "US Foods", "Houston, TX": "US Foods",
    "Riviera Beach, FL": "Cheney Brothers", "Ocala, FL": "Cheney Brothers",
    "Punta Gorda, FL": "Cheney Brothers",
}


def test_seed_is_identical_to_the_old_hard_coded_maps():
    assert RR.sender_map("usf-report") == OLD_REPORT_SENDERS
    assert RR.sender_map("worksheet") == OLD_WORKSHEET_SENDERS
    assert RR.DOMAIN_TO_DISTRIBUTOR == OLD_DOMAINS
    assert RR.warehouse_reps() == OLD_WAREHOUSE_REPS
    assert list(RR.warehouse_reps()) == list(OLD_WAREHOUSE_REPS)   # page order
    assert RR.distributor_of() == OLD_DISTRIBUTOR_OF


def test_scanner_maps_come_from_the_registry():
    assert R.REPORT_SENDER_TO_WAREHOUSE == RR.sender_map("usf-report")
    assert W.REP_EMAIL_TO_WAREHOUSE == RR.sender_map("worksheet")
    assert ES.DOMAIN_TO_DISTRIBUTOR is RR.DOMAIN_TO_DISTRIBUTOR


def test_status_page_maps_come_from_the_registry():
    assert RS.WAREHOUSE_REPS == RR.warehouse_reps()
    assert RS.DISTRIBUTOR_OF == RR.distributor_of()


def test_scanner_and_status_page_agree_on_every_rep():
    """The drift the registry exists to prevent: a rep the scanner maps to a
    warehouse must cover that warehouse on the status page, and the reverse."""
    scanner = {}
    scanner.update(R.REPORT_SENDER_TO_WAREHOUSE)
    scanner.update(W.REP_EMAIL_TO_WAREHOUSE)
    status = {(rep["email"], wh) for wh, reps in RS.WAREHOUSE_REPS.items()
              for rep in reps}
    for email, (dist, wh) in scanner.items():
        assert (email, wh) in status, f"{email} scans as {wh} but is not on the status page"
        assert RS.DISTRIBUTOR_OF[wh] == dist
    cheney = {r["email"] for rec in RR.WAREHOUSES.values() for r in rec["reps"]
              if r["parser"] == "cheney-workbook"}
    for email, wh in status:
        if email in cheney:
            continue        # Cheney's warehouse comes from the filename
        assert scanner.get(email, (None, None))[1] == wh, \
            f"{email} covers {wh} on the status page but the scanner does not map it"
    # Every status-page sender resolves to a distributor for the scanner.
    for email, _wh in status:
        assert ES._distributor_from_sender(email), email


def test_watch_only_never_email_and_chase_variants():
    for wh, rec in RR.WAREHOUSES.items():
        assert rec["watch_only"] == all(r["no_chase"] for r in rec["reps"]), wh
        assert rec["chase_variant"] in (RR.CHASE_STANDARD, RR.CHASE_HOUSTON_SCREENSHOT)
    assert [w for w, r in RR.WAREHOUSES.items() if r["watch_only"]] == ["Alcoa, TN"]
    assert RR.WAREHOUSES["Houston, TX"]["chase_variant"] == "houston-screenshot"
    assert RR.never_email() == ["charles.youngblood@usfoods.com",
                                "christy.dunn@usfoods.com",
                                "kimberly.cobb@usfoods.com",
                                "usfcustomercontact@usfoods.com"]
    assert RR.chase_recipients("Alcoa, TN") == []
    assert RR.chase_recipients("Houston, TX") == ["tom.foley@usfoods.com"]
    assert RR.chase_recipients("Ocala, FL") == ["mross@cheneybrothers.com"]


def test_is_known_sender():
    assert RR.is_known_sender("Tom.Foley@usfoods.com")
    assert RR.is_known_sender("someone.new@usfoods.com")       # distributor domain
    assert RR.is_known_sender("buyer@mail.chefswarehouse.com")
    assert not RR.is_known_sender("jd@hhbagels.com")
    assert not RR.is_known_sender("usfoods.com@evil.example")
    assert not RR.is_known_sender("")


def test_api_reps_is_token_gated_and_returns_the_registry(tmp_path, monkeypatch):
    isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    from app import app
    c = app.test_client()
    assert c.get("/api/reps").status_code == 401
    d = c.get("/api/reps", headers={"X-Inventory-Token": "tok"}).get_json()
    assert d["ok"] is True
    by = {w["warehouse"]: w for w in d["warehouses"]}
    assert list(by) == list(RR.WAREHOUSES)
    assert by["Alcoa, TN"]["watch_only"] is True and by["Alcoa, TN"]["chase_to"] == []
    assert by["Houston, TX"]["chase_variant"] == "houston-screenshot"
    assert by["Houston, TX"]["chase_to"] == ["tom.foley@usfoods.com"]
    assert by["Zebulon, NC"]["reps"][0] == {
        "name": "Maria Hernandez", "email": "maria.hernandez@usfoods.com",
        "parser": "usf-report", "no_chase": False, "aliases": []}
    assert d["never_email"] == RR.never_email()
    assert d["domains"]["usfoods.com"] == "US Foods" and d["overrides"] == {}
