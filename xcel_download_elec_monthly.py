#!/usr/bin/env python3
"""
Xcel Energy Electric Monthly Usage Downloader — Playwright edition
-------------------------------------------------------------------
Downloads two files per run:
  1. Chart "By Month" kWh CSV  — past year, monthly On Peak / Off Peak kWh
  2. Chart "By Month" cost CSV — past year, monthly On Peak / Off Peak $

Auth flow is identical to xcel_download_elec_daily.py:
  1. Log in via Gigya ScreenSets on my.xcelenergy.com
  2. IDP-initiated SAML SSO to myenergy.xcelenergy.com
  3. Navigate to usage-history, switch to MONTHLY, intercept the ajax URL.
  4. Fetch monthly kWh (usageType=Q) and cost (usageType=C) JSON via
     requests library with browser cookies, write to CSV.

Setup:
  pip install playwright python-dotenv requests
  python -m playwright install chromium

Create a .env file in the same directory:
  XCEL_USERNAME=youruser
  XCEL_PASSWORD=yourpassword

Schedule this script to run monthly via cron or systemd timer.
Files are saved to ./xcel_data/ with the date in the filename.
See xcel_download_elec_daily.py for the daily electric download.
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
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
IDP_SSO_URL       = "https://my.xcelenergy.com/MyAccount/idp/login?app=0sp2R0000008OoM"
MYENERGY_BASE     = "https://myenergy.xcelenergy.com"
USAGE_HISTORY_URL = f"{MYENERGY_BASE}/myenergy/usage-history"

# ── Helpers ───────────────────────────────────────────────────────────────────

def swap_usage_type(url: str, usage_type: str) -> str:
    """Return url with usageType query param replaced by usage_type."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["usageType"] = [usage_type]
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def json_to_csv(data: dict, output_path: Path) -> int:
    """Convert chart JSON response to CSV. Returns number of rows written."""
    dates  = [d.split(" ")[0] for d in data.get("column_fulldates", [])]
    series = data.get("series_data", [])
    if not dates or not series:
        return 0
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date"] + [s["name"] for s in series] + ["Total"])
        for i, date in enumerate(dates):
            vals  = [s["data"][i] if i < len(s["data"]) else 0.0 for s in series]
            total = round(sum(v or 0.0 for v in vals), 3)
            writer.writerow([date] + vals + [total])
    return len(dates)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not EMAIL or not PASSWORD:
        sys.exit("ERROR: Set XCEL_USERNAME and XCEL_PASSWORD in your .env file.")

    today = datetime.today()

    # Will hold the first MONTHLY kWh ajax URL fired by the usage-history page
    captured: dict[str, str | None] = {"ajax_url": None}

    def on_request(req: object) -> None:
        url = req.url  # type: ignore[attr-defined]
        if (
            "usage-history-ajax/format/json" in url
            and "timePeriod=MONTHLY" in url
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

        # ── Step 4: Load usage-history, switch to By Month view ───────────────
        print("Step 4: Loading usage-history, switching to By Month view...")
        page.goto(USAGE_HISTORY_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_load_state("networkidle", timeout=30_000)

        # Switch to MONTHLY — this fires the kWh ajax request we intercept.
        # The select#timePeriod element is inside Salesforce LWC shadow DOM.
        page.evaluate("""() => {
            function scan(root) {
                for (const sel of root.querySelectorAll('select#timePeriod')) {
                    sel.value = 'MONTHLY';
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    return;
                }
                for (const h of root.querySelectorAll('*')) {
                    if (h.shadowRoot) scan(h.shadowRoot);
                }
            }
            scan(document);
        }""")
        page.wait_for_timeout(3000)
        page.wait_for_load_state("networkidle", timeout=15_000)

        ajax_url = captured["ajax_url"]
        if not ajax_url:
            page.screenshot(path=str(OUTPUT_DIR / "elec_monthly_debug.png"))
            raise RuntimeError(
                "Did not capture usage-history-ajax URL after switching to MONTHLY. "
                "The page layout may have changed. "
                "Check xcel_data/elec_monthly_debug.png."
            )
        print("  Captured ajax URL (timePeriod=MONTHLY).")

        kwh_url  = ajax_url                       # usageType=Q already in URL
        cost_url = swap_usage_type(ajax_url, "C") # swap Q → C for cost

        # Extract session cookies for use with requests library
        raw_cookies = context.cookies([MYENERGY_BASE])
        cookies     = {c["name"]: c["value"] for c in raw_cookies}
        req_headers = {"User-Agent": UA, "Referer": USAGE_HISTORY_URL}

        # ── Step 5: Download By Month kWh chart CSV ───────────────────────────
        print("Step 5: Downloading By Month kWh chart data...")
        r = _req.get(kwh_url, cookies=cookies, headers=req_headers, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"kWh ajax request failed: HTTP {r.status_code}")
        kwh_file = OUTPUT_DIR / f"bymonth_elec_kwh_{today.strftime('%Y-%m-%d')}.csv"
        rows = json_to_csv(r.json(), kwh_file)
        print(f"  Saved {kwh_file.name} ({rows} months)")

        # ── Step 6: Download By Month cost chart CSV ──────────────────────────
        print("Step 6: Downloading By Month cost chart data...")
        r = _req.get(cost_url, cookies=cookies, headers=req_headers, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Cost ajax request failed: HTTP {r.status_code}")
        cost_file = OUTPUT_DIR / f"bymonth_elec_cost_{today.strftime('%Y-%m-%d')}.csv"
        rows = json_to_csv(r.json(), cost_file)
        print(f"  Saved {cost_file.name} ({rows} months)")

        browser.close()

    # ── Step 7: Regenerate Prometheus textfile ─────────────────────────────────
    print("Step 7: Regenerating Prometheus textfile...")
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
