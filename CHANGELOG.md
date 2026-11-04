# Changelog

All notable changes to the H&H Bagels Inventory Tracker. Versions follow
[semantic versioning](https://semver.org/); the current version is in `VERSION`
and is reported live at `/healthz`.

## [1.3.0] — 2026-11-04

### Added
- **Lineage delivery tracking** per PO (requested → scheduled → delivered),
  learned ship-to-delivery transit per location, and server-sent follow-ups
  that run without the laptop.
- **Counted vs Theoretical**, Count Check and Suggested Orders; orders now feed
  the Production Planner, with a reps-vs-counts backtest.
- **Activity Log** tab; Pending POs and Inventory kept in step.
- Arrived POs can be adjusted in place instead of only reopened.
- Cheney Brothers: daily on-hand CSV feed (HTTPS receiver + FTPS bridge),
  EDI 810 invoice parser, pasted usage-grid OCR, and stacked on-hand grid reader.
- US Foods: Houston (DC B2) and both Chicago-metro DCs (Aurora, Bensenville)
  mapped and seeded; Houston buyer-screen screenshots read into counts.
- Email send: optional inline (cid) images for branded HTML mail.
- `/healthz` now reports `version` and the deployed `commit`.

### Changed
- Mobile layout: every tab slimmed to 1–2 line rows that fit on a phone.
- One live ingest path — the 6h cron delegates to `/api/email/scan`.
- PO revisions ordered by source email date, not revision number.
- CI runs the whole test suite plus a file-integrity gate, and can trigger
  the Render deploy hook after tests pass.

### Fixed
- Counts no longer erase deliveries that arrived after them, and PO re-applies
  no longer claw back count-absorbed deliveries.
- A torn concurrent write can no longer empty `inventory.json`.
- RFC 2047-encoded subjects and attachment names (Manassas report, USF count dates).
- Chicago worksheet reads weekly usage from "USE" columns as well as "WKLY USE".
- Production-record flow: dates, dedup, PO links and the real-time webhook.
- Many PO-lifecycle fixes from the 10/8 audit (ghost Cheney rows, Reverse,
  locking, duplicate on-order rows).

### Repo housekeeping
- Root-level `test_*.py` files moved into `tests/` (with a `conftest.py`).
- Change notes and handoffs moved into `docs/changes/` and `docs/handoffs/`;
  `REFACTOR_PLAN.md` and `DISTRIBUTOR_EMAIL_DRAFTS.md` into `docs/`.
- Added `README.md`, `CHANGELOG.md` and `VERSION`; broader `.gitignore`.

## [1.2.0] — 2026-07-01
Weekly Production Planner, reliability work and PO consolidation.

## [1.1.0] — 2026-06-21
Blueprint refactor and security hardening.

## [1.0.0] — 2026-06-10
First tagged release of the H&H Bagels Inventory Tracker.

[1.3.0]: https://github.com/winnerlose2026/Inventory-Tracker/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/winnerlose2026/Inventory-Tracker/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/winnerlose2026/Inventory-Tracker/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/winnerlose2026/Inventory-Tracker/releases/tag/v1.0.0
