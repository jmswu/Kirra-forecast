# Kirra freedive forecast

A background monitor and rule-based 48-hour freedive outlook for Kirra,
Gold Coast. It combines BOM wind observations with surf-forecast.com wind,
swell and tide data, then applies the local freediver heuristics:

- Best conditions follow a few days of gentle southerly winds.
- Swell under 1m is workable.
- Swell under 0.5m is worth swimming out to the reef for a look.
- There is never a guarantee.

No third-party Python packages are required.

## Quick start

```bash
# Generate the latest report now
python3 kirra_monitor.py

# Open the HTML dashboard
python3 kirra_monitor.py --open

# Install the macOS background job
python3 kirra_monitor.py --install
```

The background job starts at login and runs every **6 hours** at
**00:05, 06:05, 12:05, 18:05** local time. It needs no terminal window open.

```bash
python3 kirra_monitor.py --status
python3 kirra_monitor.py --uninstall
python3 kirra_monitor.py --test-notification
python3 kirra_monitor.py --test-ntfy
python3 kirra_monitor.py --setup-email
python3 kirra_monitor.py --test-email
```

## What it produces

- `reports/latest.html` — colour-coded dashboard with one combined
  swell/wind/tide chart; exact wind and tide tables are collapsed behind
  expandable sections
- `reports/latest.md` — readable Markdown version
- `reports/latest.json` — machine-readable report, including tide events
- `data/state.json` — last run, verdict and alert de-duplication
- `data/history.jsonl` — one compact record per monitor run
- `data/bom_cache.json` — last successful BOM observations (fallback)
- `logs/launchd.out.log` and `logs/launchd.err.log` — background logs

The monitor alerts **only** when a new **REEF** or **GOOD** window appears:

- a macOS notification,
- a phone push through [ntfy](https://ntfy.sh), and
- an SMTP email through Python's `smtplib` (no Mail.app involved).

There is no daily summary, so quiet days stay quiet. Night blocks are shown in
the report but never trigger alerts.

### Phone alerts

Install the free **ntfy** app on your phone and subscribe to the topic in
`alerts.ntfy.topic` in `config.json`. To print it:

```bash
python3 -c "import json; print(json.load(open('config.json'))['alerts']['ntfy']['topic'])"
```

The topic is effectively a private channel: anyone who knows it can read it,
so do not publish it. `config.json` is ignored by git; commit only
`config.json.example`.

### Email over SMTP

The email path uses `smtplib`, not your email client. Gmail requires an
**App Password**:

1. Turn on 2-Step Verification in your Google Account.
2. Google Account → Security → App passwords.
3. Create an App Password for “Mail” / “Kirra forecast”.
4. Run:

```bash
python3 kirra_monitor.py --setup-email
```

Paste the 16-character App Password when prompted. The non-secret settings are
written to `config.json`; the password is stored separately at
`~/.config/kirra-forecast/smtp.json` with mode `600`. It is not in this repo.

The default SMTP settings are `smtp.gmail.com:587` with STARTTLS. Set the
sending and receiving addresses in `alerts.email`. Until an App Password is
configured, email alerts are skipped with a clear log message; phone alerts
through ntfy still work.
Test email with:

```bash
python3 kirra_monitor.py --test-email
```

If you use a different mail provider, change the host, port and security mode
in `config.json` during or after setup.

## Configuration

The repository ships a safe `config.json.example`. Copy it to `config.json` and
fill in your own alert settings:

```bash
cp config.json.example config.json
```

`config.json` is ignored by git, so your email address, ntfy topic and other
local settings stay out of version control.

## Forecast rules

The defaults live in `config.json`.

### Past 72 hours — BOM Coolangatta

- Southerly sector: **135°–225°** (SE through SW)
- Gentle wind: **≤ 20 km/h**
- Setup **PASS**: at least **50%** of observations are gentle southerly
  **and** the longest continuous run is at least **24 hours**
- Otherwise `PARTIAL` or `FAIL`

### Next 48 hours — surf-forecast.com

| Swell | Meaning |
| --- | --- |
| < 0.5m | **REEF** — worth a look |
| 0.5–0.99m | **GOOD** if the southerly setup passed |
| 1.0–1.49m | **WATCH** |
| ≥ 1.5m | **NO** |

- **GOOD** also requires a gentle southerly wind at that time.
- **REEF** is downgraded to **WATCH** if the wind is strong (> 25 km/h).
- `WATCH` is the catch-all for “close but not the pattern the freediver described.”
- Daylight is approximated as 05:00–18:00 AEST for alerting.

The report also lists the next 48 hours of high/low tides. Tide is shown for
dive planning; it does not change the REEF/GOOD rating yet.

All thresholds are configurable. For example, to require a literal 48-hour
southerly run, set `antecedent_min_run_hours` to `48` in `config.json`.

## Data sources

- **Wind history:** Bureau of Meteorology, Coolangatta station `94592`
  ([JSON observations](https://www.bom.gov.au/fwo/IDQ60801/IDQ60801.94592.json)).
  This is the same BOM wind data used by Willyweather.
- **Wind and swell forecast:** [surf-forecast.com Kirra](https://www.surf-forecast.com/breaks/Kirra/forecasts/latest).
  Its swell row is NOAA WaveWatch III, the same underlying swell source used by
  Willyweather.
- **Tide times/heights:** surf-forecast.com’s high/low tide rows in the same
  table (next 48 hours).
- The 7-day surf-forecast table supplies a “next potential” note beyond the
  48-hour window.

Willyweather’s forecast charts are JavaScript-rendered, so this app uses the
underlying BOM/NOAA data directly instead of scraping those charts.

## Scraper CLI

The original scraper still works independently:

```bash
python3 kirra_forecast.py                 # 7-day CSV to stdout
python3 kirra_forecast.py --hourly        # 48-hour / 3-hourly CSV
python3 kirra_forecast.py --tides         # high/low tide events CSV
python3 kirra_forecast.py --tides --hourly
python3 kirra_forecast.py -o kirra.csv
python3 kirra_forecast.py --json
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Notes

- This is a deterministic rule-based outlook, not an AI forecast. The report
  shows the evidence and lets you judge it.
- Forecasts change. The background job refreshes every 6 hours; the HTML
  report is overwritten each time and history is appended for audit.
- If you move the project folder, re-run `python3 kirra_monitor.py --install`
  so the LaunchAgent points at the new path.
- If BOM is unavailable, a cache is used for up to 6 hours. Older than that,
  the southerly setup is treated as unknown rather than guessed.
- If surf-forecast is unavailable, the last successful report is kept and the
  error is logged.
- This is guidance, not a guarantee. Check the water and your own limits.
