#!/usr/bin/env python3
"""Background monitor and 48-hour freedive outlook for Kirra.

Run one cycle:
    python3 kirra_monitor.py

Install the macOS background job (runs every 6 hours):
    python3 kirra_monitor.py --install

Remove it:
    python3 kirra_monitor.py --uninstall

Check it:
    python3 kirra_monitor.py --status
    python3 kirra_monitor.py --open

Alerts are sent only for a new REEF/GOOD window, via macOS notification,
ntfy phone push, and SMTP email (smtplib).

The monitor combines:
  * BOM Coolangatta observations for the past 72h wind setup.
  * surf-forecast.com for the next 48h wind and swell.
  * surf-forecast.com's 7-day table for "what happens after 48h".
"""

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import plistlib
import smtplib
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from kirra_forecast import (
    DEFAULT_URL,
    SIX_DAY_SUFFIX,
    ScrapeError,
    fetch,
    scrape,
)
from kirra_outlook import (
    build_report,
    compass_to_degrees,
    render_html,
    render_markdown,
)

PROJECT_DIR = Path(__file__).resolve().parent
LABEL = "com.kirra.forecast"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"
REPORTS_DIR = PROJECT_DIR / "reports"
DATA_DIR = PROJECT_DIR / "data"
LOGS_DIR = PROJECT_DIR / "logs"

DEFAULT_CONFIG = {
    "location": {
        "name": "Kirra",
        "timezone": "Australia/Brisbane",
        "bom_station_url": "https://www.bom.gov.au/fwo/IDQ60801/IDQ60801.94592.json",
        "bom_attribution": "Bureau of Meteorology, Coolangatta (station 94592)",
    },
    "sources": {
        "forecast_url": DEFAULT_URL,
        "seven_day_url": DEFAULT_URL + SIX_DAY_SUFFIX,
    },
    "rules": {
        "southerly_min_deg": 135,
        "southerly_max_deg": 225,
        "gentle_max_kmh": 20,
        "strong_wind_kmh": 25,
        "antecedent_window_hours": 72,
        "antecedent_min_fraction": 0.50,
        "antecedent_min_run_hours": 24,
        "antecedent_partial_fraction": 0.30,
        "antecedent_partial_run_hours": 12,
        "antecedent_min_observations": 12,
        "observation_gap_minutes": 90,
        "sample_minutes": 30,
        "swell_reef_max_m": 0.5,
        "swell_good_max_m": 1.0,
        "swell_no_min_m": 1.5,
        "forecast_hours": 48,
        "block_hours": 3,
        "daylight_start_hour": 5,
        "daylight_end_hour": 18,
    },
    "monitor": {
        "interval_hours": 6,
        "notify": True,
        "max_observation_age_hours": 6,
    },
    "alerts": {
        "macos": True,
        "ntfy": {
            "enabled": False,
            "server": "https://ntfy.sh",
            "topic": "",
        },
        "email": {
            "enabled": False,
            "host": "smtp.gmail.com",
            "port": 587,
            "security": "starttls",
            "username": "",
            "from": "",
            "to": "",
            "password_env": "KIRRA_SMTP_PASSWORD",
            "password_file": "~/.config/kirra-forecast/smtp.json",
        },
    },
}


# ---------------------------------------------------------------------------
# Config and file helpers
# ---------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict:
    override = {}
    if path.exists():
        with open(path, encoding="utf-8") as handle:
            override = json.load(handle)
    return deep_merge(DEFAULT_CONFIG, override)


def ensure_dirs() -> None:
    for directory in (REPORTS_DIR, DATA_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    temporary.replace(path)


def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def log(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def parse_bom_json(text: str, timezone: ZoneInfo) -> list[dict]:
    payload = json.loads(text)
    rows = payload.get("observations", {}).get("data", [])
    observations = []
    for row in rows:
        stamp = row.get("local_date_time_full")
        speed = row.get("wind_spd_kmh")
        if not stamp or speed is None:
            continue
        try:
            moment = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone)
            speed_value = float(speed)
        except (TypeError, ValueError):
            continue
        direction = (row.get("wind_dir") or "").strip().upper()
        gust = row.get("gust_kmh")
        observations.append({
            "time": moment,
            "speed_kmh": speed_value,
            "gust_kmh": float(gust) if gust not in (None, "") else None,
            "direction": direction,
            "direction_deg": compass_to_degrees(direction),
        })
    observations.sort(key=lambda item: item["time"])
    # BOM occasionally repeats a timestamp; keep the latest copy.
    deduped = {}
    for item in observations:
        deduped[item["time"]] = item
    return [deduped[key] for key in sorted(deduped)]


def fetch_bom_observations(config: dict, timezone: ZoneInfo) -> list[dict]:
    url = config["location"]["bom_station_url"]
    observations = parse_bom_json(fetch(url), timezone)
    if not observations:
        raise RuntimeError("BOM returned no usable wind observations.")
    return observations


def save_bom_cache(observations: list[dict], fetched_at: datetime) -> None:
    payload = {
        "fetched_at": fetched_at.isoformat(timespec="minutes"),
        "observations": [
            {
                "time": obs["time"].isoformat(timespec="minutes"),
                "speed_kmh": obs["speed_kmh"],
                "gust_kmh": obs["gust_kmh"],
                "direction": obs["direction"],
                "direction_deg": obs["direction_deg"],
            }
            for obs in observations
        ],
    }
    atomic_write(
        DATA_DIR / "bom_cache.json",
        json.dumps(payload, indent=2) + "\n",
    )


def load_bom_cache(timezone: ZoneInfo) -> tuple[list[dict], datetime] | tuple[None, None]:
    payload = load_json(DATA_DIR / "bom_cache.json", None)
    if not payload:
        return None, None
    try:
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        observations = []
        for item in payload.get("observations", []):
            moment = datetime.fromisoformat(item["time"])
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone)
            observations.append({
                "time": moment,
                "speed_kmh": item.get("speed_kmh"),
                "gust_kmh": item.get("gust_kmh"),
                "direction": item.get("direction"),
                "direction_deg": item.get("direction_deg"),
            })
        if observations:
            observations.sort(key=lambda item: item["time"])
            return observations, fetched_at
    except (KeyError, TypeError, ValueError):
        pass
    return None, None


def records_to_blocks(records: list[dict]) -> list[dict]:
    blocks = []
    for record in records:
        moment = datetime.fromisoformat(record["datetime_local"])
        blocks.append({
            "time": moment,
            "wind_speed_kmh": record.get("wind_speed_kmh"),
            "wind_direction": record.get("wind_direction"),
            "wind_direction_deg": record.get("wind_direction_deg"),
            "swell_height_m": record.get("swell_height_m"),
            "swell_direction": record.get("swell_direction"),
            "swell_direction_deg": record.get("swell_direction_deg"),
            "swell_period_s": record.get("swell_period_s"),
        })
    blocks.sort(key=lambda item: item["time"])
    return blocks


def source_metadata(config: dict) -> dict:
    return {
        "location": config["location"]["name"],
        "forecast_url": config["sources"]["forecast_url"],
        "seven_day_url": config["sources"]["seven_day_url"],
        "bom_url": config["location"]["bom_station_url"],
        "bom_attribution": config["location"]["bom_attribution"],
    }


# ---------------------------------------------------------------------------
# Reports and notifications
# ---------------------------------------------------------------------------

def write_reports(report: dict) -> dict[str, Path]:
    ensure_dirs()
    paths = {
        "markdown": REPORTS_DIR / "latest.md",
        "html": REPORTS_DIR / "latest.html",
        "json": REPORTS_DIR / "latest.json",
    }
    atomic_write(paths["markdown"], render_markdown(report))
    atomic_write(paths["html"], render_html(report))
    atomic_write(
        paths["json"],
        json.dumps(report, indent=2, default=json_default) + "\n",
    )
    return paths


def append_history(report: dict) -> None:
    ensure_dirs()
    window = report.get("best_window")
    record = {
        "generated_at": report["generated_at"].isoformat(timespec="minutes"),
        "verdict": report["verdict"],
        "confidence": report["confidence"],
        "antecedent_status": report["antecedent"]["status"],
        "antecedent_fraction": report["antecedent"]["fraction"],
        "antecedent_longest_run_hours": report["antecedent"]["longest_run_hours"],
        "min_swell_m": report["range"]["min_swell_m"],
        "max_swell_m": report["range"]["max_swell_m"],
        "peak_swell_time": (
            report["range"]["peak_time"].isoformat(timespec="minutes")
            if report["range"].get("peak_time") else None
        ),
        "best_window": (
            {
                "start": window["start"].isoformat(timespec="minutes"),
                "end": window["end"].isoformat(timespec="minutes"),
                "rating": window["rating"],
                "alertable": window["alertable"],
            }
            if window else None
        ),
    }
    with open(DATA_DIR / "history.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def applescript_quote(value: str) -> str:
    value = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return f'"{value}"'


def send_notification(title: str, subtitle: str, message: str) -> bool:
    script = (
        f"display notification {applescript_quote(message)} "
        f"with title {applescript_quote(title)} "
        f"subtitle {applescript_quote(subtitle)} sound name \"Glass\""
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"Notification failed: {exc}")
        return False
    if result.returncode != 0:
        log(f"Notification failed: {result.stderr.strip()}")
        return False
    return True


def smtp_secrets_path(email_config: dict) -> Path:
    raw = email_config.get("password_file") or "~/.config/kirra-forecast/smtp.json"
    return Path(os.path.expanduser(raw))


def load_smtp_password(email_config: dict) -> str | None:
    env_name = email_config.get("password_env") or "KIRRA_SMTP_PASSWORD"
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    path = smtp_secrets_path(email_config)
    payload = load_json(path, {})
    password = payload.get("password") if isinstance(payload, dict) else None
    if password and path.exists():
        try:
            if path.stat().st_mode & 0o077:
                log(f"WARNING: {path} is readable by other users; chmod 600 it.")
        except OSError:
            pass
    return password


def save_smtp_password(email_config: dict, password: str) -> Path:
    path = smtp_secrets_path(email_config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"password": password}, handle)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return path


def send_email(subject: str, body: str, email_config: dict) -> bool:
    """Send email directly over SMTP (no Mail.app / email client involved)."""
    host = (email_config.get("host") or "").strip()
    port = int(email_config.get("port") or 587)
    security = (email_config.get("security") or "starttls").lower()
    username = (email_config.get("username") or "").strip()
    from_addr = (email_config.get("from") or username).strip()
    to_addr = (email_config.get("to") or "").strip()
    if not all((host, username, from_addr, to_addr)):
        log("Email alert skipped: incomplete SMTP settings in config.json")
        return False
    password = load_smtp_password(email_config)
    if not password:
        log(
            "Email alert skipped: no SMTP password. "
            "Run: python3 kirra_monitor.py --setup-email"
        )
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to_addr
    message.set_content(body)

    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(
                host, port, timeout=30, context=ssl.create_default_context()
            ) as smtp:
                smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                if security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                smtp.login(username, password)
                smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        log(
            "Email failed: SMTP login was rejected. Gmail requires an App "
            f"Password, not the normal account password ({exc.smtp_code})."
        )
        return False
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        log(f"Email failed: {exc}")
        return False
    return True


def setup_email(config: dict, config_path: Path) -> int:
    """Interactive one-time SMTP setup; the password is stored outside git."""
    email_config = config.setdefault("alerts", {}).setdefault("email", {})
    print("SMTP email setup")
    print("Use a Gmail App Password, not your normal Google password.")
    print("Google Account > Security > 2-Step Verification > App passwords")

    def ask(label: str, current: str) -> str:
        value = input(f"{label} [{current}]: ").strip()
        return value or current

    host = ask("SMTP host", email_config.get("host", "smtp.gmail.com"))
    port_text = ask("SMTP port", str(email_config.get("port", 587)))
    username = ask(
        "SMTP username", email_config.get("username", "wu.zhuhua@gmail.com")
    )
    from_addr = ask("From address", email_config.get("from", username))
    to_addr = ask("To address", email_config.get("to", "wu.zhuhua@gmail.com"))
    security = ask(
        "Security (starttls/ssl/none)",
        email_config.get("security", "starttls"),
    ).lower()
    try:
        port = int(port_text)
    except ValueError:
        print("error: SMTP port must be a number", file=sys.stderr)
        return 1
    try:
        password = getpass.getpass(
            "Gmail App Password (not echoed; spaces are ignored): "
        ).replace(" ", "").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 1
    if not password:
        print("error: no password entered", file=sys.stderr)
        return 1

    email_config.update({
        "enabled": True,
        "host": host,
        "port": port,
        "security": security,
        "username": username,
        "from": from_addr,
        "to": to_addr,
        "password_env": "KIRRA_SMTP_PASSWORD",
        "password_file": "~/.config/kirra-forecast/smtp.json",
    })
    secret_path = save_smtp_password(email_config, password)
    atomic_write(config_path, json.dumps(config, indent=2) + "\n")
    print(f"Saved non-secret settings to {config_path}")
    print(f"Saved App Password to {secret_path} (mode 600)")

    test_subject = "Kirra freedive monitor test"
    test_body = "SMTP email alerts are working."
    if send_email(test_subject, test_body, email_config):
        print("Test email sent.")
        return 0
    print("Settings saved, but the test email failed. Check the log above.")
    return 1


def send_ntfy(
    title: str,
    message: str,
    server: str,
    topic: str,
    priority: str = "high",
) -> bool:
    """Send a phone push through ntfy.sh (no account or API key)."""
    server = (server or "https://ntfy.sh").rstrip("/")
    topic = (topic or "").strip()
    if not topic:
        log("ntfy alert skipped: alerts.ntfy.topic is empty")
        return False
    headers = {
        "Title": title.encode("ascii", "replace").decode("ascii"),
        "Priority": priority,
        "Tags": "ocean,warning",
        "Content-Type": "text/plain; charset=utf-8",
    }
    request = urllib.request.Request(
        f"{server}/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if 200 <= response.status < 300:
                    return True
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt == 0:
            time.sleep(2)
    log(f"ntfy alert failed: {last_error}")
    return False


def load_state() -> dict:
    return load_json(DATA_DIR / "state.json", {})


def save_state(state: dict) -> None:
    ensure_dirs()
    atomic_write(DATA_DIR / "state.json", json.dumps(state, indent=2) + "\n")


def build_alert_email(report: dict) -> tuple[str, str]:
    antecedent = report["antecedent"]
    window = report.get("best_window")
    lines = [
        "Kirra freedive outlook",
        report["generated_at"].strftime("%A %d %B %Y, %I:%M %p %Z"),
        "",
        report["headline"],
        "",
        f"Verdict: {report['verdict']}",
        f"Confidence: {report['confidence'].title()}",
        (
            f"Past 72h: {antecedent['fraction'] * 100:.0f}% gentle southerly, "
            f"longest run {antecedent['longest_run_hours']:.1f}h "
            f"({antecedent['status_label']})"
        ),
    ]
    if window:
        lines += [
            (
                f"Window: {window['start'].strftime('%a %d %b %H:%M')} to "
                f"{window['end'].strftime('%H:%M')}"
            ),
            f"Swell: {window['min_swell_m']:g}-{window['max_swell_m']:g}m",
        ]
    lines += [
        "",
        "Report: reports/latest.html",
        "",
        "Rule-based outlook from local freediver experience. Guidance, not a guarantee.",
    ]
    return f"Kirra freedive alert: {report['verdict']}", "\n".join(lines)


def maybe_notify(report: dict, config: dict, enabled: bool) -> bool:
    """Send macOS + email alerts, only for a new REEF/GOOD window."""
    state = load_state()
    state["last_run"] = report["generated_at"].isoformat(timespec="minutes")
    state["last_verdict"] = report["verdict"]
    state["last_confidence"] = report["confidence"]
    alert = report.get("alert")
    sent = False

    if enabled and alert:
        alerts = config.get("alerts", {})
        macos_enabled = bool(alerts.get("macos", True))
        ntfy_config = alerts.get("ntfy", {})
        ntfy_enabled = bool(ntfy_config.get("enabled", False))
        email_config = alerts.get("email", {})
        email_enabled = bool(email_config.get("enabled", False))
        recipient = (email_config.get("to") or "").strip()

        if (
            macos_enabled
            and alert["key"] != state.get("last_macos_alert_key")
            and send_notification(alert["title"], alert["subtitle"], alert["message"])
        ):
            state["last_macos_alert_key"] = alert["key"]
            state["last_macos_alert_at"] = report["generated_at"].isoformat(
                timespec="minutes"
            )
            sent = True

        if (
            ntfy_enabled
            and alert["key"] != state.get("last_ntfy_alert_key")
            and send_ntfy(
                alert["subtitle"],
                alert["message"],
                ntfy_config.get("server", "https://ntfy.sh"),
                ntfy_config.get("topic", ""),
                priority="high" if report["verdict"] == "REEF WINDOW" else "default",
            )
        ):
            state["last_ntfy_alert_key"] = alert["key"]
            state["last_ntfy_alert_at"] = report["generated_at"].isoformat(
                timespec="minutes"
            )
            sent = True

        if email_enabled and alert["key"] != state.get("last_email_alert_key"):
            if not recipient:
                log("Email alert skipped: alerts.email.to is empty")
            else:
                subject, body = build_alert_email(report)
                if send_email(subject, body, email_config):
                    state["last_email_alert_key"] = alert["key"]
                    state["last_email_alert_at"] = report["generated_at"].isoformat(
                        timespec="minutes"
                    )
                    sent = True

    save_state(state)
    return sent


# ---------------------------------------------------------------------------
# Monitor cycle
# ---------------------------------------------------------------------------

def run_cycle(args, config: dict) -> int:
    timezone = ZoneInfo(config["location"]["timezone"])
    now = datetime.now(timezone)
    ensure_dirs()

    log(f"Fetching surf forecast for {config['location']['name']}...")
    try:
        records = scrape(config["sources"]["forecast_url"], timezone)
    except ScrapeError as exc:
        log(f"ERROR: {exc}")
        return 1

    seven_day_records = None
    try:
        seven_day_records = scrape(config["sources"]["seven_day_url"], timezone)
    except ScrapeError as exc:
        log(f"WARNING: 7-day context unavailable: {exc}")

    stale_hours = None
    max_age = float(config["monitor"].get("max_observation_age_hours", 6))
    try:
        observations = fetch_bom_observations(config, timezone)
        save_bom_cache(observations, now)
        latest_age = round(
            (now - observations[-1]["time"]).total_seconds() / 3600.0, 1
        )
        log(
            f"BOM observations: {len(observations)} records "
            f"(latest {latest_age:.1f}h ago)"
        )
        if latest_age > max_age:
            log(
                f"WARNING: latest BOM observation is {latest_age:.1f}h old "
                f"(max {max_age:g}h); setup will be treated as unknown."
            )
            stale_hours = latest_age
            observations = []
    except Exception as exc:  # noqa: BLE001 - any BOM failure should fall back
        log(f"WARNING: BOM observations unavailable: {exc}")
        observations, fetched_at = load_bom_cache(timezone)
        if observations and fetched_at:
            cache_age = (
                now - fetched_at.astimezone(timezone)
            ).total_seconds() / 3600.0
            observation_age = (
                now - observations[-1]["time"]
            ).total_seconds() / 3600.0
            stale_hours = round(max(cache_age, observation_age), 1)
            if stale_hours > max_age:
                log(
                    f"WARNING: BOM cache is {stale_hours:.1f}h old "
                    f"(max {max_age:g}h); setup will be treated as unknown."
                )
                observations = []
            else:
                log(f"Using BOM cache from {stale_hours:.1f}h ago")
        else:
            observations = []
            log("WARNING: no BOM cache available; setup will be unknown")

    blocks = records_to_blocks(records)
    horizon = now + timedelta(hours=float(config["rules"]["forecast_hours"]))
    blocks = [block for block in blocks if now <= block["time"] <= horizon]
    if not blocks:
        # If the table is old, use the last available block rather than
        # producing a completely empty report.
        blocks = records_to_blocks(records)[-1:]

    seven_day = records_to_blocks(seven_day_records) if seven_day_records else None
    report = build_report(
        blocks,
        observations,
        now,
        config["rules"],
        source_metadata(config),
        seven_day=seven_day,
        stale_hours=stale_hours,
    )

    paths = write_reports(report)
    append_history(report)
    notification_sent = maybe_notify(
        report,
        config,
        enabled=bool(config["monitor"].get("notify", True)) and not args.no_notify,
    )

    log(f"Verdict: {report['verdict']} - {report['headline']}")
    log(f"Wrote {paths['markdown'].relative_to(PROJECT_DIR)}")
    log(f"Wrote {paths['html'].relative_to(PROJECT_DIR)}")
    if notification_sent:
        log("Notification sent")
    return 0


# ---------------------------------------------------------------------------
# LaunchAgent management
# ---------------------------------------------------------------------------

def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install_agent(config: dict, config_path: Path) -> int:
    ensure_dirs()
    interval = int(config["monitor"].get("interval_hours", 6))
    if interval < 1 or interval > 24:
        print("error: monitor.interval_hours must be between 1 and 24", file=sys.stderr)
        return 1

    hours = list(range(0, 24, interval))
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            str(PROJECT_DIR / "kirra_monitor.py"),
            "--config",
            str(config_path),
        ],
        "WorkingDirectory": str(PROJECT_DIR),
        "StartCalendarInterval": [
            {"Hour": hour, "Minute": 5} for hour in hours
        ],
        "RunAtLoad": True,
        "ProcessType": "Background",
        "ThrottleInterval": 60,
        "StandardOutPath": str(LOGS_DIR / "launchd.out.log"),
        "StandardErrorPath": str(LOGS_DIR / "launchd.err.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }

    path = launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        plistlib.dump(plist, handle)

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log(f"launchctl bootstrap failed: {result.stderr.strip()}")
        fallback = subprocess.run(
            ["launchctl", "load", "-w", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if fallback.returncode != 0:
            print(
                "error: could not load the LaunchAgent: "
                f"{fallback.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
    subprocess.run(
        ["launchctl", "enable", f"gui/{uid}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )

    print(f"Installed {path}")
    print(f"Runs at {', '.join(f'{hour:02d}:05' for hour in hours)} local time.")
    print("The first run starts now (RunAtLoad).")
    print("Report: reports/latest.html")
    return 0


def uninstall_agent() -> int:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    path = launch_agent_path()
    if path.exists():
        path.unlink()
        print(f"Removed {path}")
    else:
        print("LaunchAgent was not installed.")
    return 0


def show_status() -> int:
    path = launch_agent_path()
    print(f"LaunchAgent: {path}")
    print(f"Installed: {'yes' if path.exists() else 'no'}")
    if path.exists():
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            print("launchctl: loaded")
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith(("state =", "last exit code =", "pid =")):
                    print(f"  {stripped}")
        else:
            print("launchctl: not loaded")
    for name in ("latest.md", "latest.html", "latest.json"):
        report = REPORTS_DIR / name
        if report.exists():
            stamp = datetime.fromtimestamp(
                report.stat().st_mtime, tz=timezone.utc
            ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{name}: {stamp}")
    state = load_state()
    if state:
        print(f"Last verdict: {state.get('last_verdict', '-')}")
        print(f"Last run: {state.get('last_run', '-')}")
    return 0


def open_report() -> int:
    report = REPORTS_DIR / "latest.html"
    if not report.exists():
        print("No report yet. Run: python3 kirra_monitor.py", file=sys.stderr)
        return 1
    subprocess.run(["open", str(report)], check=False)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monitor Kirra wind/swell and produce a 48-hour freedive outlook."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="install and load the macOS LaunchAgent",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="unload and remove the macOS LaunchAgent",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="show LaunchAgent and report status",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="open the latest HTML report",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep running in the foreground at the configured interval",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="foreground watch interval in minutes (default: configured interval)",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="generate the report without sending alerts",
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="send a test macOS notification and exit",
    )
    parser.add_argument(
        "--setup-email",
        action="store_true",
        help="interactively configure SMTP email (App Password stored outside git)",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="send a test email through SMTP and exit",
    )
    parser.add_argument(
        "--test-ntfy",
        action="store_true",
        help="send a test phone push through ntfy and exit",
    )
    args = parser.parse_args(argv)

    if args.uninstall:
        return uninstall_agent()
    if args.status:
        return show_status()
    if args.open:
        return open_report()
    if args.test_notification:
        return 0 if send_notification(
            "Kirra freedive monitor",
            "Test",
            "Background notifications are working.",
        ) else 1

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"error: could not load config: {exc}", file=sys.stderr)
        return 1

    if args.setup_email:
        return setup_email(config, args.config)

    if args.test_email:
        email_config = config.get("alerts", {}).get("email", {})
        return 0 if send_email(
            "Kirra freedive monitor test",
            "SMTP email alerts are working.",
            email_config,
        ) else 1

    if args.test_ntfy:
        ntfy_config = config.get("alerts", {}).get("ntfy", {})
        return 0 if send_ntfy(
            "Kirra freedive monitor test",
            "Phone alerts are working.",
            ntfy_config.get("server", "https://ntfy.sh"),
            ntfy_config.get("topic", ""),
            priority="default",
        ) else 1

    if args.install:
        return install_agent(config, args.config)

    if args.watch:
        interval = args.interval_minutes or int(
            config["monitor"].get("interval_hours", 6)
        ) * 60
        log(f"Watching every {interval} minutes. Ctrl-C to stop.")
        try:
            while True:
                run_cycle(args, config)
                time.sleep(max(60, interval * 60))
        except KeyboardInterrupt:
            log("Stopped.")
            return 0

    try:
        return run_cycle(args, config)
    except Exception:  # noqa: BLE001 - launchd should log the traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
