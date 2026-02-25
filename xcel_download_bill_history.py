#!/usr/bin/env python3
"""
Xcel Energy Bill History Downloader — Playwright edition
---------------------------------------------------------
Downloads one file per run:
  1. Bill history CSV — up to 2 years of monthly billing summaries,
     one row per billing cycle (electric and gas billed on separate dates).

Navigates to the Bill History page (myenergy/bill-presentment), intercepts
the account-summary AJAX call to capture the custid, then fetches the full
2-year JSON via requests and writes it to CSV.

Auth flow is identical to the other download scripts:
  1. Log in via Gigya ScreenSets on my.xcelenergy.com
  2. IDP-initiated SAML SSO to myenergy.xcelenergy.com
  3. Navigate to bill-presentment, intercept the ajax URL.
  4. Re-fetch with a 2-year date window via requests library.

Setup:
  pip install playwright python-dotenv requests
  python -m playwright install chromium

Create a .env file in the same directory:
  XCEL_USERNAME=youruser
  XCEL_PASSWORD=yourpassword

Schedule this script to run monthly via cron or systemd timer.
Files are saved to ./xcel_data/ with the date in the filename.
"""

from __future__ import annotations

import csv
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests as _req
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from xcel_to_prom import generate_prom, PROM_DIR

# ── Configuration ─────────────────────────────────────────────────────────────

load_dotenv()

EMAIL    = os.getenv("XCEL_USERNAME")
PASSWORD = os.getenv("XCEL_PASSWORD")

# Where to save files
OUTPUT_DIR = Path("./xcel_data")
OUTPUT_DIR.mkdir(exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ── URLs ──────────────────────────────────────────────────────────────────────

LOGIN_URL = (
    "https://my.xcelenergy.com/MyAccount/XE_Login"
    "?template=XE_MA_Template&gig_client_id=JnU2RjC15thihnMDrOyzKzvH"
)
IDP_SSO_URL        = "https://my.xcelenergy.com/MyAccount/idp/login?app=0sp2R0000008OoM"
MYENERGY_BASE      = "https://myenergy.xcelenergy.com"
BILL_HISTORY_URL   = f"{MYENERGY_BASE}/myenergy/bill-presentment"
BILL_AJAX_ENDPOINT = f"{MYENERGY_BASE}/myenergy/bill-presentment-account-summary-ajax"

# ── Helpers ───────────────────────────────────────────────────────────────────

def build_bill_ajax_url(custid: str, start: datetime, end: datetime) -> str:
    """Build the bill summary AJAX URL for the given date range."""
    params = {
        "custid":     custid,
        "page":       "false",
        "widget_id":  "4907",
        "start_date": str(int(start.timestamp())),
        "end_date":   str(int(end.timestamp())),
        "_":          str(int(datetime.now().timestamp() * 1000)),
    }
    return f"{BILL_AJAX_ENDPOINT}?{urlencode(params)}"


def parse_money(s: str) -> float:
    """Parse '$1,234.56' or '1234.56' to float."""
    return float(re.sub(r"[^0-9.\-]", "", str(s)) or "0")


def bill_json_to_csv(data: dict, output_path: Path) -> int:
    """
    Parse the cost_barchart from the bill JSON and write to CSV.
    Returns number of rows written.

    CSV columns: Date, Electric Charges, Gas Charges, Total
    Each row represents one billing cycle. Electric and gas are billed
    on separate dates, so most rows have one $0 column.
    """
    chart   = data.get("cost_barchart", {})
    cats    = chart.get("categories", [])    # ["MM/DD/YYYY", ...]
    series  = chart.get("series_data", [])   # [{name, data}, ...]

    if not cats or not series:
        return 0

    # Build a lookup: series name → values list
    by_name: dict[str, list] = {s["name"]: s["data"] for s in series}
    elec_vals  = by_name.get("Electric Charges", [0.0] * len(cats))
    gas_vals   = by_name.get("Gas Charges",      [0.0] * len(cats))

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Electric Charges", "Gas Charges", "Total"])
        rows_written = 0
        for i, raw_date in enumerate(cats):
            # Convert MM/DD/YYYY → YYYY-MM-DD for consistent sorting
            try:
                date = datetime.strptime(raw_date, "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                date = raw_date
            elec  = elec_vals[i] if i < len(elec_vals) else 0.0
            gas   = gas_vals[i]  if i < len(gas_vals)  else 0.0
            total = round((elec or 0) + (gas or 0), 2)
            # Only write rows that have a non-zero charge
            if elec or gas:
                writer.writerow([date, elec or 0.0, gas or 0.0, total])
                rows_written += 1
    return rows_written


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not EMAIL or not PASSWORD:
        sys.exit("ERROR: Set XCEL_USERNAME and XCEL_PASSWORD in your .env file.")

    today          = datetime.today()
    history_start  = today - timedelta(days=730)  # 2-year window

    # Will hold the first bill-presentment-account-summary-ajax URL
    captured: dict[str, str | None] = {"ajax_url": None}

    def on_request(req: object) -> None:
        url = req.url  # type: ignore[attr-defined]
        if (
            "bill-presentment-account-summary-ajax" in url
            and captured["ajax_url"] is None
        ):
            captured["ajax_url"] = url

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, user_agent=UA)
        page    = context.new_page()
        page.on("request", on_request)

        # ── Step 1: Load the Gigya login page ─────────────────────────────────
        print("Step 1: Loading Xcel Energy login page...")
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_selector(
            "input[data-screenset-roles='instance'][data-gigya-name='loginID']",
            state="attached", timeout=30_000,
        )
        print("  Login form ready.")

        # ── Step 2: Fill credentials and submit ───────────────────────────────
        print("Step 2: Signing in...")
        page.locator(
            "input[data-screenset-roles='instance'][data-gigya-name='loginID']"
        ).fill(EMAIL)
        page.locator(
            "input[data-screenset-roles='instance'][data-gigya-name='password']"
        ).fill(PASSWORD)
        page.locator(
            "input[data-screenset-roles='instance'][type='submit']"
        ).click()

        try:
            page.wait_for_url(lambda url: "XE_Login" not in url, timeout=30_000)
        except PWTimeout:
            page.screenshot(path=str(OUTPUT_DIR / "login_error.png"))
            raise RuntimeError(
                "Timed out waiting for post-login redirect. "
                "Check xcel_data/login_error.png."
            )
        page.wait_for_load_state("networkidle", timeout=30_000)
        print(f"  Logged in — now at: {page.url}")

        # ── Step 3: IDP-initiated SAML SSO to myenergy.xcelenergy.com ────────
        print("Step 3: IDP-initiated SAML SSO to myenergy.xcelenergy.com...")
        page.goto(IDP_SSO_URL, wait_until="networkidle", timeout=60_000)

        try:
            page.wait_for_url("**/myenergy.xcelenergy.com/**", timeout=60_000)
        except PWTimeout:
            page.screenshot(path=str(OUTPUT_DIR / "sso_error.png"))
            raise RuntimeError(
                "Failed to reach myenergy.xcelenergy.com after IDP SSO. "
                "Check xcel_data/sso_error.png."
            )
        page.wait_for_load_state("networkidle", timeout=30_000)
        print(f"  SSO complete — now at: {page.url}")

        # ── Step 4: Load bill-presentment page, intercept AJAX URL ───────────
        print("Step 4: Loading bill history page...")
        page.goto(BILL_HISTORY_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_load_state("networkidle", timeout=30_000)

        ajax_url = captured["ajax_url"]
        if not ajax_url:
            page.screenshot(path=str(OUTPUT_DIR / "bill_history_error.png"))
            raise RuntimeError(
                "Did not capture bill-presentment-account-summary-ajax URL. "
                "The page layout may have changed. "
                "Check xcel_data/bill_history_error.png."
            )

        # Extract custid from the intercepted URL
        parsed = urlparse(ajax_url)
        params = parse_qs(parsed.query)
        custid = params.get("custid", [None])[0]
        if not custid:
            raise RuntimeError(f"Could not extract custid from AJAX URL: {ajax_url}")
        print(f"  Captured AJAX URL (custid={custid}).")

        # Extract session cookies for use with requests library
        raw_cookies = context.cookies([MYENERGY_BASE])
        cookies     = {c["name"]: c["value"] for c in raw_cookies}
        req_headers = {"User-Agent": UA, "Referer": BILL_HISTORY_URL}

        browser.close()

    # ── Step 5: Fetch 2-year bill history JSON via requests ───────────────────
    print(
        f"Step 5: Fetching bill history "
        f"({history_start.strftime('%m/%d/%Y')} → {today.strftime('%m/%d/%Y')})..."
    )
    fetch_url = build_bill_ajax_url(custid, history_start, today)
    r = _req.get(fetch_url, cookies=cookies, headers=req_headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Bill history AJAX request failed: HTTP {r.status_code}")

    bill_file = OUTPUT_DIR / f"bill_summary_{today.strftime('%Y-%m-%d')}.csv"
    rows = bill_json_to_csv(r.json(), bill_file)
    print(f"  Saved {bill_file.name} ({rows} billing cycles)")

    # ── Step 6: Regenerate Prometheus textfile ─────────────────────────────────
    print("Step 6: Regenerating Prometheus textfile...")
    prom_out = generate_prom(data_dir=OUTPUT_DIR, prom_dir=PROM_DIR)
    text     = prom_out.read_text(encoding="utf-8")
    samples  = sum(1 for ln in text.splitlines() if ln and not ln.startswith("#"))
    print(f"  Saved {prom_out}  ({samples} samples)")

    print("\nDone!")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFailed: {e}")
        sys.exit(1)
