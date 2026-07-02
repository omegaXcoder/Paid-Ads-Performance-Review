"""
WhatConverts Leads Export
--------------------------------------------------------------------
Pulls Qualified / Pending / Not Set lead counts from WhatConverts for
every client in the Account_Mapping tab, across three periods (Last 7
Days, Previous Full Week Mon-Sun, Month to Date), and writes a fresh
snapshot to the WhatConverts_Raw_Leads tab in the same Google Sheet
used by the Ads export.

Defaults to a dry run: logs exactly what it would write, touches
nothing, sends no email, unless run with --live.

See README.md for full usage.
"""

import argparse
import datetime
import json
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText

import requests
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()  # no-op if .env doesn't exist (e.g. in GitHub Actions, which uses real env vars)

# ── Config ────────────────────────────────────────────────────────────

WC_BASE_URL = "https://app.whatconverts.com/api/v1/leads"

# Same spreadsheet the Ads export writes to.
SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID", "1v9pqP0IQPsHLF45pTqlHvzkTtg1fHTYNVuNvSzbOU54"
)
MAPPING_SHEET_NAME = "Account_Mapping"
OUTPUT_SHEET_NAME = "WhatConverts_Raw_Leads"

EMAIL_TO = os.environ.get("EMAIL_TO", "omega@kudos.marketing")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "automation@kudos.marketing")

# Small delay between API calls to stay well under WhatConverts' rate
# limit (1 req/ms, 20 concurrent) without needing any fancy throttling.
REQUEST_DELAY_SECONDS = 0.2


# ── Date range helpers (mirrors the logic in the Ads export script) ────

def get_periods():
    """Returns a list of (label, start_date, end_date) as date objects."""
    today = datetime.date.today()

    # Last 7 Days: 7 days ago through yesterday.
    last7_start = today - datetime.timedelta(days=7)
    last7_end = today - datetime.timedelta(days=1)

    # Previous Full Week (Mon-Sun): the most recently completed Mon-Sun.
    days_since_monday = today.weekday()  # Mon=0 ... Sun=6
    this_monday = today - datetime.timedelta(days=days_since_monday)
    last_week_monday = this_monday - datetime.timedelta(days=7)
    last_week_sunday = last_week_monday + datetime.timedelta(days=6)

    # Month to Date: first of this month through today.
    mtd_start = today.replace(day=1)
    mtd_end = today

    return [
        ("Last 7 Days", last7_start, last7_end),
        ("Previous Full Week (Mon-Sun)", last_week_monday, last_week_sunday),
        ("Month to Date", mtd_start, mtd_end),
    ]


# ── WhatConverts API ────────────────────────────────────────────────────

def get_lead_count(token, secret, profile_id, quotable, start_date, end_date):
    """Returns the total_leads count for one profile/status/date range."""
    params = {
        "profile_id": profile_id,
        "quotable": quotable,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "leads_per_page": 1,  # only need the total_leads count, not the leads themselves
        # Google Ads leads only — matches the leads back to the Ads spend
        # they're being reconciled against in the final report.
        "lead_source": "google",
        "lead_medium": "cpc",
    }
    resp = requests.get(WC_BASE_URL, params=params, auth=(token, secret), timeout=30)
    resp.raise_for_status()
    return resp.json().get("total_leads", 0)


def get_qualified_leads_totals(token, secret, profile_id, start_date, end_date):
    """
    Fetches all Qualified (quotable=yes) leads for a profile/date range,
    filtered to Google/CPC leads only, and returns
    (count, total_quote_value, total_sales_value). Paginates using the
    API max of 2500 leads/page in the rare case a client has more
    qualified leads than that in a single period.
    """
    page = 1
    per_page = 2500
    total_count = 0
    quote_sum = 0.0
    sales_sum = 0.0

    while True:
        params = {
            "profile_id": profile_id,
            "quotable": "yes",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "leads_per_page": per_page,
            "page_number": page,
            # Google Ads leads only — matches the leads back to the Ads
            # spend they're being reconciled against in the final report.
            "lead_source": "google",
            "lead_medium": "cpc",
        }
        resp = requests.get(WC_BASE_URL, params=params, auth=(token, secret), timeout=30)
        resp.raise_for_status()
        data = resp.json()

        total_count = data.get("total_leads", 0)
        for lead in data.get("leads", []):
            quote_sum += float(lead.get("quote_value") or 0)
            sales_sum += float(lead.get("sales_value") or 0)

        total_pages = data.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    return total_count, round(quote_sum, 2), round(sales_sum, 2)



# ── Google Sheets ────────────────────────────────────────────────────────

def get_sheets_client():
    """
    Builds an authorized gspread client. Looks for the service account
    JSON in GOOGLE_SERVICE_ACCOUNT_JSON (a raw JSON string, used in
    GitHub Actions) first, then falls back to a local file path in
    GOOGLE_SERVICE_ACCOUNT_FILE (used for local runs).
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        info = json.loads(raw_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        file_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds = Credentials.from_service_account_file(file_path, scopes=scopes)

    return gspread.authorize(creds)


def read_account_mapping(client):
    """Reads Business Name + WhatConverts Profile ID from the mapping tab."""
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(MAPPING_SHEET_NAME)
    rows = sheet.get_all_records()  # uses row 1 as headers

    clients = []
    for row in rows:
        name = str(row.get("Business Name", "")).strip()
        profile_id = str(row.get("What Converts Profile ID", "")).strip()
        if name and profile_id:
            clients.append({"name": name, "profile_id": profile_id})
    return clients


def write_results(client, rows, dry_run, log):
    if dry_run:
        log("DRY RUN — nothing written to the Sheet, no email sent.")
        log(f"Rows that would be written: {len(rows)}")
        if rows:
            log(f"Sample row: {rows[0]}")
        return

    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    try:
        sheet = spreadsheet.worksheet(OUTPUT_SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=OUTPUT_SHEET_NAME, rows=200, cols=10)

    sheet.clear()

    header = [
        "Run Timestamp", "Business Name", "WhatConverts Profile ID", "Period",
        "Qualified Leads", "Pending Leads", "Not Set Leads", "Total Leads",
        "Qualified Quote Value", "Qualified Sales Value",
    ]
    sheet.append_row(header)

    if rows:
        sheet.append_rows(rows, value_input_option="USER_ENTERED")

    log(f"Wrote {len(rows)} rows to '{OUTPUT_SHEET_NAME}'.")


# ── Email log ────────────────────────────────────────────────────────────

def send_log_email(subject, body):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")

    if not (host and username and password and EMAIL_TO):
        print("SMTP not fully configured — skipping email, log was still printed above.")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(username, password)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WhatConverts leads export")
    parser.add_argument(
        "--live", action="store_true",
        help="Actually write to the Sheet and send the email log. Default is dry run.",
    )
    args = parser.parse_args()

    # DRY_RUN env var (used by the GitHub Actions workflow) can also force
    # dry run even if --live is passed, as a safety net.
    dry_run = not args.live or os.environ.get("DRY_RUN", "").lower() == "true"

    log_lines = []

    def log(msg):
        line = f"{datetime.datetime.now().isoformat()} {msg}"
        print(line)
        log_lines.append(line)

    log(f"=== Starting run | mode={'DRY RUN' if dry_run else 'LIVE'} ===")

    token = os.environ.get("WC_TOKEN")
    secret = os.environ.get("WC_SECRET")
    if not token or not secret:
        log("FATAL: WC_TOKEN / WC_SECRET not set.")
        sys.exit(1)

    sheets_client = get_sheets_client()
    clients = read_account_mapping(sheets_client)
    log(f"Loaded {len(clients)} clients from {MAPPING_SHEET_NAME}.")

    periods = get_periods()
    all_rows = []
    errors = []

    for c in clients:
        for period_label, start, end in periods:
            try:
                # Qualified leads need the full records (to sum quote_value /
                # sales_value), not just a count.
                qualified_count, quote_value_sum, sales_value_sum = get_qualified_leads_totals(
                    token, secret, c["profile_id"], start, end
                )
                time.sleep(REQUEST_DELAY_SECONDS)

                # Pending / Not Set only need counts — leads_per_page=1 is enough.
                pending_count = get_lead_count(
                    token, secret, c["profile_id"], "pending", start, end
                )
                time.sleep(REQUEST_DELAY_SECONDS)

                not_set_count = get_lead_count(
                    token, secret, c["profile_id"], "not_set", start, end
                )
                time.sleep(REQUEST_DELAY_SECONDS)

                total = qualified_count + pending_count + not_set_count

                all_rows.append([
                    datetime.datetime.now().isoformat(),
                    c["name"],
                    c["profile_id"],
                    f"{period_label} ({start.isoformat()} to {end.isoformat()})",
                    qualified_count,
                    pending_count,
                    not_set_count,
                    total,
                    quote_value_sum,
                    sales_value_sum,
                ])
            except requests.exceptions.RequestException as e:
                msg = f"{c['name']} ({c['profile_id']}) / {period_label}: {e}"
                errors.append(msg)
                log(f"ERROR {msg}")

    log(f"Processed {len(clients)} clients, {len(all_rows)} rows, {len(errors)} errors.")

    write_results(sheets_client, all_rows, dry_run, log)

    # Always email the log, dry run or live, so a broken dry run doesn't
    # go unnoticed just because it's "only a test."
    mode_label = "DRY RUN" if dry_run else ("COMPLETED WITH ERRORS" if errors else "SUCCESS")
    subject = f"AUTOMATION LOGGING: WhatConverts Leads Export — {mode_label}"
    body = "\n".join(log_lines)
    if errors:
        body += "\n\nErrors:\n" + "\n".join(errors)
    send_log_email(subject, body)

    log("=== Run finished ===")


if __name__ == "__main__":
    main()
