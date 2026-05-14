# H&H Bagels — Distributor Inventory Template

H&H Bagels uses this CSV to keep our on-hand visibility in sync with what
you're holding for us. Please fill in one row per (variety × warehouse)
pair you carry for our account, then drop the file into the FTPS inbox
described below. Once a week is fine; daily is better.

## Required columns

| Column            | Required | Example                  | Notes                                                              |
| ----------------- | -------- | ------------------------ | ------------------------------------------------------------------ |
| `variety`         | yes      | `Plain`, `Everything`    | Match the variety names listed in the template rows.               |
| `warehouse`       | yes      | `Manassas, VA`           | Your DC. Use the city + state form we already use on POs.          |
| `distributor_sku` | optional | `USF-100234`, `CB-BGL-PLN-4` | Your internal item code. Helps us match unambiguously if a variety name ever drifts. |
| `cs_qty`          | yes      | `48`                     | Cases currently on hand at that warehouse. Integer.                |
| `weekly_usage`    | yes      | `12.5`                   | Cases per week consumed (your view of H&H's pull-through).         |
| `weeks_remaining` | yes      | `3.8`                    | Your estimate of weeks of stock remaining at current pull rate.    |
| `as_of`           | optional | `2026-05-14`             | Date of the snapshot. Defaults to upload time if blank.            |

## Where to drop the file

FTPS over TLS (port 21 explicit) at `sftp.hhbagels.com`.
Your username + password were provided separately by your H&H contact.

| Distributor      | FTPS Username             | Folder      |
| ---------------- | ------------------------- | ----------- |
| US Foods         | `usfoods@hhbagels.com`    | `incoming/` |
| Cheney Brothers  | `cheney@hhbagels.com`     | `incoming/` |

Filename convention: `HH_<distributor>_inventory_<YYYY-MM-DD>.csv`,
e.g. `HH_usfoods_inventory_2026-05-14.csv`. The H&H side picks the file
up automatically every 15 minutes, parses it, and moves it to
`processed/` once ingested.

## Optional: EDI 846 / 867 instead of CSV

If your team already has EDI 846 (Inventory Inquiry/Advice) or 867
(Product Activity Data) standing up against your other foodservice
customers, we can consume those instead — over SFTP or AS2, with the
same per-DC granularity. Reach out to JD at jd@hhbagels.com to scope.
