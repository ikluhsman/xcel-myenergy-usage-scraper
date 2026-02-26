#!/usr/bin/env python3
"""
Xcel Energy On-Demand Interval Read Downloader — Playwright edition
---------------------------------------------------------------------
Downloads one file per run:
  1. On-demand 15-minute interval CSV — current day's kWh readings

Navigates to usage-history, which auto-fires an odr-ajax request containing
all 15-minute interval readings for the current day.  Captures the JSON
response directly (no re-fetch required), converts to CSV, and regenerates
the Prometheus textfile.

Auth flow is identical to the other download scripts:
  1. Log in via Gigya ScreenSets on my.xcelenergy.com
  2. IDP-initiated SAML SSO to myenergy.xcelenergy.com
  3. Navigate to usage-history — the page auto-fires odr-ajax.
  4. Parse intervals from the JSON response body.

Schedule this script to run hourly via systemd timer.
Files are saved to ./xcel_data/ with the date in the filename.
Each run overwrites the current day's CSV so the prom file always reflects
all intervals recorded so far today.

CSV columns: DateTime, kWh, rate_level, unix_ms
  DateTime  — ISO-format string, e.g. 2026-02-26 08:30
  kWh       — odr_amt value (15-minute electricity usage)
  rate_level — off-peak or on-peak
  unix_ms   — Unix timestamp in milliseconds (used in prom textfile)
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from xcel_to_prom import generate_prom, PROM_DIR

# ── Configuration ─────────────────────────────────────────────────────────────

load_dotenv()

EMAIL    = os.getenv("XCEL_USERNAME")
PASSWORD = os.getenv("XCEL_PASSWORD")

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

def parse_interval_datetime(ts: str) -> str:
    """Convert '2/26/2026 8:30 AM' to '2026-02-26 08:30'."""
    try:
        return datetime.strptime(ts, "%m/%d/%Y %I:%M %p").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def intervals_to_csv(intervals: list[dict], output_path: Path) -> int:
    """
    Write interval list from odr-ajax response to CSV.
    Intervals are sorted oldest-first.
    Returns number of rows written.
    """
    rows = []
    for iv in intervals:
        ts_str = iv.get("last_request_timestamp", "")
        unix_s = iv.get("last_request_unix_timestamp")
        kwh    = iv.get("odr_amt")
        rate   = iv.get("rate_level", "")
        if ts_str and unix_s is not None and kwh is not None:
            rows.append({
                "DateTime":  parse_interval_datetime(ts_str),
                "kWh":       kwh,
                "rate_level": rate,
                "unix_ms":   int(unix_s) * 1000,
            })

    # Sort oldest → newest so Prometheus sees chronological order
    rows.sort(key=lambda r: r["unix_ms"])

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["DateTime", "kWh", "rate_level", "unix_ms"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not EMAIL or not PASSWORD:
        sys.exit("ERROR: Set XCEL_USERNAME and XCEL_PASSWORD in your .env file.")

    today = datetime.today()

    captured: dict[str, object] = {"odr_data": None}

    def on_response(resp) -> None:
        if "odr-ajax" in resp.url and captured["odr_data"] is None:
            try:
                captured["odr_data"] = resp.json()
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, user_agent=UA)
        page    = context.new_page()
        page.on("response", on_response)

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

        # ── Step 4: Load usage-history — odr-ajax fires automatically ─────────
        print("Step 4: Loading usage-history (triggers odr-ajax automatically)...")
        page.goto(USAGE_HISTORY_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_load_state("networkidle", timeout=30_000)

        odr_data = captured["odr_data"]
        if not odr_data:
            page.screenshot(path=str(OUTPUT_DIR / "ondemand_error.png"))
            raise RuntimeError(
                "Did not capture odr-ajax response. "
                "The page layout may have changed. "
                "Check xcel_data/ondemand_error.png."
            )

        error = odr_data.get("error")
        if error:
            raise RuntimeError(f"odr-ajax returned error: {error}")

        intervals = odr_data.get("intervals", [])
        print(f"  Captured odr-ajax response ({len(intervals)} intervals).")

        browser.close()

    # ── Step 5: Write CSV ──────────────────────────────────────────────────────
    print("Step 5: Writing on-demand interval CSV...")
    csv_file = OUTPUT_DIR / f"ondemand_{today.strftime('%Y-%m-%d')}.csv"
    rows = intervals_to_csv(intervals, csv_file)
    print(f"  Saved {csv_file.name} ({rows} intervals)")

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
