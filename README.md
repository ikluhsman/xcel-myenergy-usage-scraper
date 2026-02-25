# Xcel Energy Usage Scraper

Automated downloader for Xcel Energy electric and gas usage data. Uses Playwright (headless Chromium) to authenticate via Gigya + SAML SSO, then fetches chart data via the internal JSON API. Outputs dated CSVs and a Prometheus textfile for Grafana dashboards.

## What it downloads

### Electric (daily) — `xcel_download_elec_daily.py`
| File | Content |
|------|---------|
| `byday_kwh_YYYY-MM-DD.csv` | 30-day billing period — On Peak / Off Peak / Total kWh |
| `byday_cost_YYYY-MM-DD.csv` | 30-day billing period — On Peak / Off Peak / Total $ |
| `xcel_usage_YYYY-MM-DD.csv` | 2-year daily interval kWh (cassandra endpoint) |
| `xcel_energy.prom` | Prometheus textfile (regenerated after each run) |

### Gas (monthly) — `xcel_download_gas_monthly.py`
| File | Content |
|------|---------|
| `bymonth_gas_usage_YYYY-MM-DD.csv` | 13 months of gas usage in therms |
| `bymonth_gas_cost_YYYY-MM-DD.csv` | 13 months of gas cost in $ |
| `xcel_energy.prom` | Prometheus textfile (regenerated, gas metrics added) |

All files are saved to `./xcel_data/`.

## Prometheus metrics

Written to `xcel_energy.prom` for [node_exporter textfile collector](https://github.com/prometheus/node_exporter#textfile-collector):

| Metric | Labels | Description |
|--------|--------|-------------|
| `xcel_energy_daily_kwh` | `period`, `date` | Daily kWh by time-of-use period |
| `xcel_energy_daily_cost_dollars` | `period`, `date` | Daily cost in USD |
| `xcel_energy_interval_kwh` | `date`, `generated` | 2-year daily interval kWh |
| `xcel_gas_monthly_therms` | `date` | Monthly gas therms (optional) |
| `xcel_gas_monthly_cost_dollars` | `date` | Monthly gas cost in USD (optional) |
| `xcel_energy_last_updated_seconds` | — | Unix timestamp of last run |

Gas metrics are only written when gas CSVs are present in `xcel_data/`.

## Setup

### 1. Install dependencies

```bash
pip install playwright python-dotenv requests
python -m playwright install chromium
```

### 2. Create `.env`

```ini
XCEL_USERNAME=your_xcel_login_email_or_id
XCEL_PASSWORD=your_password
METER_ID=your_ami_meter_id
PROM_DIR=/var/lib/node_exporter/textfile_collector/
```

`PROM_DIR` defaults to `./xcel_data/` if not set. On Linux with node_exporter, set it to the textfile collector path shown above.

Optionally set `XCEL_DATA_DIR` to override the CSV output directory (default: `./xcel_data/`).

`METER_ID` is your AMI meter ID used by the cassandra interval download in the electric script. You can find it in the Xcel Energy portal URL or network requests when viewing usage history.

## Running

```bash
# Electric daily
python xcel_download_elec_daily.py

# Gas monthly
python xcel_download_gas_monthly.py

# Regenerate .prom from existing CSVs only (no login needed)
python xcel_to_prom.py
```

If login fails, a screenshot is saved to `xcel_data/login_error.png` or `xcel_data/sso_error.png` for debugging.

## Scheduling (Linux/cron)

```cron
# Electric — run daily at 6 AM
0 6 * * * cd /path/to/xcel_energy_usage_scraper && python xcel_download_elec_daily.py >> /var/log/xcel_elec.log 2>&1

# Gas — run on the 1st of each month at 6 AM
0 6 1 * * cd /path/to/xcel_energy_usage_scraper && python xcel_download_gas_monthly.py >> /var/log/xcel_gas.log 2>&1
```

## Grafana dashboard

Import `grafana_dashboard.json` via **Dashboards → Import** in the Grafana UI. The dashboard uses Prometheus instant queries with `format=table` and date-sorted bar charts for:

- Daily kWh (On Peak / Off Peak / Total)
- Daily cost
- 2-year interval kWh history
- Monthly gas therms and cost
- Last updated timestamp

**Requirements:** Prometheus scraping `node_exporter` with the textfile collector enabled.

## Auth flow (technical notes)

1. **Gigya login** — `my.xcelenergy.com` with ScreenSets; targets `data-screenset-roles='instance'` elements to avoid duplicate template fields in the DOM.
2. **SAML SSO** — navigates to the Salesforce IDP-initiated SSO URL; Salesforce generates a SAMLResponse server-side and POSTs it to `myenergy.xcelenergy.com`, setting `SimpleSAMLSessionID` + `PHPSESSID`.
3. **Shadow DOM interaction** — `select#timePeriod` and the Meter dropdown are inside Salesforce LWC shadow roots; accessed via recursive `page.evaluate()` scans.
4. **JSON interception** — a request listener captures the `usage-history-ajax/format/json` URL (which includes dynamic `custId` and `fuelType` params); kWh and cost data are then fetched via the `requests` library using the browser's session cookies.
5. **Cassandra CSV** — the 2-year interval CSV is downloaded via `page.expect_download()` since the endpoint returns `Content-Disposition: attachment`.

## Files

```
xcel_download_elec_daily.py   Main electric script (8 steps)
xcel_download_gas_monthly.py  Gas script (9 steps)
xcel_to_prom.py               CSV → Prometheus converter (also used as a library)
grafana_dashboard.json        Grafana dashboard definition
xcel_data/                    Output directory (CSVs + .prom file)
.env                          Credentials (do not commit)
```

## `.gitignore` recommendation

```gitignore
.env
xcel_data/
.venv/
__pycache__/
```
