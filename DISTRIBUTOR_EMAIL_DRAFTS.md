# Distributor data-feed request emails — drafts (updated 2026-05-14)

Two drafts below, refreshed now that the FTPS inbox is live and the
H&H inventory template is finalized. Both lead with the CSV path
(realistic ask + we already have the template), mention EDI 846/867
as a stretch, and skip REST API entirely. Replace bracketed
placeholders before sending. Attach
`distributor_templates/HH_BAGELS_INVENTORY_TEMPLATE.csv` and
`distributor_templates/README.md`.

---

## 1. US Foods — to Lisa Athey

**To:** Lisa Athey <[lisa.athey@usfoods.com — confirm]>
**Cc:** [your USF account exec, if you have one]
**Subject:** H&H Bagels (vendor #150345) — inventory data feed via FTPS
**Attachments:**
- `HH_BAGELS_INVENTORY_TEMPLATE.csv`
- `README.md`

Hi Lisa,

We've finished standing up the inventory tracker on our side and I want
to wire it directly to USF data instead of working off the manual touch
points we have today. Can you connect me with your EDI / Integration
Services team to get this scheduled?

What we're asking for: one CSV per week (daily if it's easy on your
end), one row per (variety × DC) for every USF warehouse currently
shipping us — Manassas 2125/5O, Zebulon, La Mirada, Chicago, Alcoa.
Three required values per row:

- **cs_qty** — cases currently on hand for us at that DC
- **weekly_usage** — your view of our pull-through, cases/week
- **weeks_remaining** — your estimate of weeks of stock left at the
  current pull rate

I've attached a CSV template and a README that walks through each
column and how to drop the file. Short version:

- Drop the file via FTPS (port 21, explicit TLS) at
  `sftp.hhbagels.com`. Username: `usfoods@hhbagels.com`. I'll send the
  password under separate cover.
- Filename: `HH_usfoods_inventory_<YYYY-MM-DD>.csv`, dropped to the
  `incoming/` folder. We pick it up every 15 minutes and parse it
  automatically; processed files move to `processed/` so you can see
  we received them.

If your team already runs EDI 846 (Inventory Inquiry/Advice) and/or
EDI 867 (Product Activity Data) for your other foodservice customers,
we can consume those instead — over SFTP or AS2, same per-DC
granularity. Only worth doing if it's already standing up for our
account; no need to spin up a new EDI integration just for us.

While we get the recurring feed scheduled — is there a report in MOXē
or US Foods Online I can run today against vendor #150345 that gives
me a one-shot version of the inventory snapshot? Even a manual export
emailed to me would close the gap.

Anyone besides you I should be looping in on the USF side?

Thanks Lisa,

JD Gross
H&H Bagels
jd@hhbagels.com

---

## 2. Cheney Brothers — to your DSR

**To:** [DSR name] <[email]>
**Cc:** [your Cheney customer service contact, if you have one]
**Subject:** H&H Bagels — inventory data feed via FTPS
**Attachments:**
- `HH_BAGELS_INVENTORY_TEMPLATE.csv`
- `README.md`

Hi [DSR first name],

Quick ask — we've stood up an internal inventory tracker and want to
pull live on-hand numbers from Cheney instead of working off spot-
checks. Can you connect me with whoever runs customer integration / EDI
on your side?

What we're after, account-level for H&H across Riviera Beach, Ocala,
and Punta Gorda — one CSV per week (daily if it's easy), one row per
(variety × DC). Three required values per row:

- **cs_qty** — cases currently on hand for us at that DC
- **weekly_usage** — cases/week pull-through against our account
- **weeks_remaining** — weeks of stock left at the current pull rate

I've attached our standard template (`HH_BAGELS_INVENTORY_TEMPLATE.csv`)
and a one-page README. The drop:

- FTPS (port 21, explicit TLS) at `sftp.hhbagels.com`. Username:
  `cheney@hhbagels.com`. I'll send the password separately.
- Filename: `HH_cheney_inventory_<YYYY-MM-DD>.csv` into the `incoming/`
  folder. We pick up every 15 minutes; processed files move to
  `processed/`.

If Cheney already has EDI 846/867 standing up for our account, we'll
take that instead — SFTP or AS2 — same per-DC granularity. No need to
spin a new EDI integration just for us, though.

While we get the recurring feed lined up — is there an
inventory-by-location or item-sales-by-DC report inside CB Direct I
can run myself today? A one-shot export would unblock us until the
scheduled feed lands.

Our Cheney account # is [fill in from CB Direct login]. Let me know
who I should be talking to.

Thanks,
JD Gross
H&H Bagels
jd@hhbagels.com

---

## Before you hit send — quick checklist

- [ ] Confirm Lisa Athey's current email + that she's still our buyer
- [ ] Pull the Cheney account # from CB Direct and drop it into draft #2
- [ ] Fill in the Cheney DSR's name + email
- [ ] Decide whether to cc anyone (USF account exec; Cheney customer service)
- [ ] Confirm the FTPS passwords in 1Password and send each distributor
      their own password under separate cover (don't paste it into these
      emails)
- [ ] Attach `distributor_templates/HH_BAGELS_INVENTORY_TEMPLATE.csv`
      and `distributor_templates/README.md` to each email
