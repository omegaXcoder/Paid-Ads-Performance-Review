# WhatConverts Leads Export

Pulls Qualified / Pending / Not Set lead counts from WhatConverts for every
client in your `Account_Mapping` Google Sheet tab, across three periods
(Last 7 Days, Previous Full Week Mon-Sun, Month to Date), and writes a
fresh snapshot to the `WhatConverts_Raw_Leads` tab in the same spreadsheet
your Ads export writes to.

Runs on GitHub Actions **daily, automatically** via a scheduled workflow.

**Always defaults to a dry run.** Locally, it will not touch the Sheet or
send email unless you pass `--live`. On GitHub Actions, scheduled runs are
always live (that's the point of the automation); manual runs from the
Actions tab default to dry run unless you tick the "live" box.

## 1. Repo setup

Create a new GitHub repo and push these files:

```
main.py
requirements.txt
.env.example
.github/workflows/wc_leads_export.yml
README.md
```

## 2. Local setup (for testing before you trust it on a schedule)

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- `WC_TOKEN` / `WC_SECRET` — your WhatConverts Master Account Key (from
  the agency dashboard: Integrations → API Keys)
- `GOOGLE_SERVICE_ACCOUNT_FILE` — path to the same service account JSON
  key used by your Ads/SA automation, if it already has edit access to
  this spreadsheet. If not, create one in Google Cloud Console with the
  Sheets API enabled and share the spreadsheet with its email address.
- `SPREADSHEET_ID` — already pre-filled with your sheet
- `SMTP_*` / `EMAIL_FROM` / `EMAIL_TO` — used to email the run log

**Never commit `.env` or `service_account.json`.** Add both to
`.gitignore` before your first commit.

## 3. GitHub Actions setup (for the real Mon/Wed/Fri schedule)

In your repo, go to **Settings → Secrets and variables → Actions** and
add these repository secrets:

| Secret | Value |
|---|---|
| `WC_TOKEN` | Your WhatConverts token |
| `WC_SECRET` | Your WhatConverts secret |
| `SPREADSHEET_ID` | `1v9pqP0IQPsHLF45pTqlHvzkTtg1fHTYNVuNvSzbOU54` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The **entire contents** of your service account JSON key file, pasted as one secret (not a file path — GitHub Secrets don't support files directly) |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | e.g. `587` |
| `SMTP_USERNAME` | Your SMTP account |
| `SMTP_PASSWORD` | Your SMTP app password |
| `EMAIL_FROM` | `automation@kudos.marketing` |
| `EMAIL_TO` | `omega@kudos.marketing` |

Once secrets are set, the workflow is already scheduled — no further
setup needed. First run will happen at the next 11:00 UTC.

## Commands / modes

| What you want | How |
|---|---|
| **Dry run locally** | `python main.py` (no `--live` flag = dry run by default) |
| **Live run locally** | `python main.py --live` |
| **Manual dry run on GitHub** | Actions tab → "WhatConverts Leads Export" → Run workflow → leave "live" unchecked |
| **Manual live run on GitHub** | Actions tab → "WhatConverts Leads Export" → Run workflow → check "live" |
| **Change the schedule** | Edit the `cron` line in `.github/workflows/wc_leads_export.yml`. Current: `0 11 * * *` = 11:00 UTC daily. [crontab.guru](https://crontab.guru) is useful for building the expression. |

## What gets written

Tab `WhatConverts_Raw_Leads` is **fully overwritten every run** — same
snapshot pattern as `Ads_Raw_Metrics`. Columns:

`Run Timestamp | Business Name | WhatConverts Profile ID | Period | Qualified Leads | Pending Leads | Not Set Leads | Total Leads | Qualified Quote Value | Qualified Sales Value`

Three rows per client (one per period).

**Quote Value / Sales Value are Qualified-leads-only** — summed from each
individual qualified lead's `quote_value` and `sales_value` fields, since
that's the number that ties directly to your target cost-per-qualified-lead
calculation. Pending and Not Set leads don't get value totals (they're
counts only), since they haven't been qualified yet.

## Known limitations

- **Relies on `Account_Mapping` being current.** If a client is added to
  WhatConverts but not yet added as a row in that tab, they're silently
  skipped from this export. Worth a periodic manual check.
- **`quotable` field mapping**: WhatConverts' `quotable` field has 4
  possible values — this export uses `yes` (Qualified), `pending`
  (Pending), and `not_set` (Not Set). It ignores `no` (explicitly
  disqualified leads) since that wasn't part of the original ask — let
  me know if you want that tracked too.
- **Lead counts, not spend.** This tab has no cost data — that
  reconciliation against Google Ads spend happens in the next phase
  (the final Mon/Wed/Fri report job), using `Account_Mapping` to join
  this tab to `Ads_Raw_Metrics` by Profile ID ↔ Customer ID.
- **Best-effort execution.** If one client/period errors out (bad
  profile ID, API hiccup), it's logged and the run continues — check the
  email log after each run.
