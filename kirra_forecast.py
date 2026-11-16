#!/usr/bin/env python3
"""Scrape wind and swell forecasts from surf-forecast.com.

By default this pulls the 7-day Kirra table (3 forecasts per day: 9am, 3pm,
9pm local time). Use --hourly for the 48-hour detailed table (3-hourly).

No third-party packages are required.

Examples:
    python3 kirra_forecast.py                          # CSV to stdout
    python3 kirra_forecast.py -o kirra_forecast.csv    # CSV to a file
    python3 kirra_forecast.py --json                   # JSON to stdout
    python3 kirra_forecast.py --hourly                 # 48h / 3-hourly table
    python3 kirra_forecast.py --url <url>              # another forecast page

The site shows forecast times in local time. Kirra is AEST (UTC+10); use
--tz to supply the timezone when scraping a forecast page for another region.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

DEFAULT_URL = "https://www.surf-forecast.com/breaks/Kirra/forecasts/latest"
SIX_DAY_SUFFIX = "/six_day"
DEFAULT_TZ = "Australia/Brisbane"  # AEST, UTC+10; no daylight saving
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
]
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

FIELDS = [
    "datetime_local",
    "date",
    "time",
    "wind_speed_kmh",
    "wind_direction",
    "wind_direction_deg",
    "swell_height_m",
    "swell_direction",
    "swell_direction_deg",
    "swell_period_s",
]


class ScrapeError(RuntimeError):
    """Raised when the page cannot be fetched or its table layout changes."""


# ---------------------------------------------------------------------------
# Minimal HTML tree (stdlib only)
# ---------------------------------------------------------------------------

class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        parts: list[str] = []

        def walk(node: Node) -> None:
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    walk(child)

        walk(self)
        return " ".join("".join(parts).split())

    def iter_nodes(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter_nodes()

    def find(self, tag: str | None = None, cls: str | None = None) -> Node | None:
        for node in self.iter_nodes():
            if tag is not None and node.tag != tag:
                continue
            if cls is not None and cls not in node.classes:
                continue
            return node
        return None

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list[Node]:
        found = []
        for node in self.iter_nodes():
            if tag is not None and node.tag != tag:
                continue
            if cls is not None and cls not in node.classes:
                continue
            found.append(node)
        return found


class TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def parse_html(markup: str) -> Node:
    parser = TreeParser()
    parser.feed(markup)
    parser.close()
    return parser.root


# ---------------------------------------------------------------------------
# Fetching and table parsing
# ---------------------------------------------------------------------------

def fetch(url: str, attempts: int = 3) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
    raise ScrapeError(f"Could not fetch {url}: {last_error}")


def direct_cells(row: Node, tag: str = "td") -> list[Node]:
    return [
        child for child in row.children
        if isinstance(child, Node) and child.tag == tag
    ]


def number_from_rotation(icon: Node, arrow_cls: str) -> float | None:
    """The arrows point where wind/swell is going; return where it comes from."""
    arrow = icon.find(cls=arrow_cls)
    if arrow is None:
        return None
    match = re.search(r"rotate\(\s*(-?[\d.]+)", arrow.attrs.get("transform", ""))
    if not match:
        return None
    return (float(match.group(1)) + 180.0) % 360.0


def compass_to_degrees(direction: str) -> float | None:
    try:
        return COMPASS_POINTS.index(direction.upper()) * 22.5
    except ValueError:
        return None


def unit_text(table: Node, cls: str) -> str:
    node = table.find(cls=cls)
    return node.text().strip().lower() if node else ""


def parse_wind(cell: Node, wind_factor: float) -> tuple[float | None, str, float | None]:
    icon = cell.find(cls="wind-icon")
    if icon is None or "data-speed" not in icon.attrs:
        return None, "", None
    speed = float(icon.attrs["data-speed"]) * wind_factor
    letters = icon.find(cls="wind-icon__letters")
    direction = letters.text().upper() if letters else ""
    degrees = number_from_rotation(icon, "wind-icon__arrow")
    if degrees is None:
        degrees = compass_to_degrees(direction)
    return round(speed, 1), direction, degrees


def parse_swell(cell: Node, height_factor: float) -> tuple[float | None, str, float | None, int | None]:
    icon = cell.find(cls="swell-icon")
    if icon is None or "data-height" not in icon.attrs:
        return None, "", None, None

    height = round(float(icon.attrs["data-height"]) * height_factor, 1)
    container = cell.find(cls="forecast-table__container")
    if container is None:
        return height, "", None, None

    divs = [
        child for child in container.children
        if isinstance(child, Node) and child.tag == "div"
    ]
    # Layout: <div class="swell-icon">, <div>DIRECTION</div>, <div>PERIOD</div>
    direction = divs[1].text().upper() if len(divs) >= 3 else ""
    period_text = divs[2].text() if len(divs) >= 3 else ""
    period_match = re.search(r"\d+", period_text)
    period = int(period_match.group()) if period_match else None

    degrees = number_from_rotation(icon, "swell-icon__arrow")
    if degrees is None:
        degrees = compass_to_degrees(direction)
    return height, direction, degrees, period


def scrape(url: str, timezone: ZoneInfo) -> list[dict]:
    markup = fetch(url)
    root = parse_html(markup)

    table = next(
        (node for node in root.iter_nodes()
         if node.tag == "table" and "forecast-table__table" in node.classes),
        None,
    )
    if table is None:
        raise ScrapeError("Forecast table not found; the site markup may have changed.")

    rows: dict[str, Node] = {}
    for row in table.find_all("tr"):
        row_name = row.attrs.get("data-row")
        if row_name:
            rows.setdefault(row_name, row)

    for required in ("days", "time", "wind", "swell"):
        if required not in rows:
            raise ScrapeError(f"Missing '{required}' row; the site markup may have changed.")

    # Build one date per column from the day cells' colspan.
    column_dates: list[str] = []
    for cell in direct_cells(rows["days"]):
        date_value = cell.attrs.get("data-date")
        if date_value:
            column_dates.extend([date_value] * int(cell.attrs.get("colspan", "1")))

    time_cells = [c for c in direct_cells(rows["time"]) if "data-hour" in c.attrs]
    hours = [int(c.attrs["data-hour"]) for c in time_cells]
    wind_cells = direct_cells(rows["wind"])
    swell_cells = direct_cells(rows["swell"])

    counts = {
        "day columns": len(column_dates),
        "time columns": len(hours),
        "wind columns": len(wind_cells),
        "swell columns": len(swell_cells),
    }
    if len(set(counts.values())) != 1:
        raise ScrapeError(f"Column counts do not line up: {counts}")

    # The site may serve mph/ft based on visitor location; normalise to metric.
    wind_unit = unit_text(table, "windu")
    height_unit = unit_text(table, "heightu")
    wind_factor = 1.609344 if wind_unit == "mph" else 1.852 if wind_unit == "kts" else 1.0
    height_factor = 0.3048 if height_unit in ("ft", "feet") else 1.0

    records = []
    for date_value, hour, wind_cell, swell_cell in zip(
        column_dates, hours, wind_cells, swell_cells
    ):
        local_dt = datetime.strptime(date_value, "%Y-%m-%d").replace(
            hour=hour, tzinfo=timezone
        )
        wind_speed, wind_direction, wind_degrees = parse_wind(wind_cell, wind_factor)
        swell_height, swell_direction, swell_degrees, swell_period = parse_swell(
            swell_cell, height_factor
        )
        records.append({
            "datetime_local": local_dt.isoformat(timespec="minutes"),
            "date": local_dt.date().isoformat(),
            "time": local_dt.strftime("%H:%M"),
            "wind_speed_kmh": wind_speed,
            "wind_direction": wind_direction,
            "wind_direction_deg": wind_degrees,
            "swell_height_m": swell_height,
            "swell_direction": swell_direction,
            "swell_direction_deg": swell_degrees,
            "swell_period_s": swell_period,
        })

    if not records:
        raise ScrapeError("No forecast rows were parsed; the site layout may have changed.")
    return records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def write_csv(records: list[dict], stream) -> None:
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape date, wind and swell data from surf-forecast.com."
    )
    parser.add_argument(
        "--hourly",
        action="store_true",
        help="use the 48-hour detailed table (3-hourly) instead of the 7-day table",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="forecast page to scrape (default: Kirra 7-day forecast)",
    )
    parser.add_argument(
        "--tz",
        default=DEFAULT_TZ,
        help=f"timezone of the forecast page (default: {DEFAULT_TZ})",
    )
    parser.add_argument(
        "-o", "--out",
        default=None,
        help="write output to this file instead of stdout",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output JSON instead of CSV",
    )
    args = parser.parse_args(argv)

    url = args.url or (DEFAULT_URL if args.hourly else DEFAULT_URL + SIX_DAY_SUFFIX)

    try:
        timezone = ZoneInfo(args.tz)
    except Exception:
        print(f"error: unknown timezone '{args.tz}'", file=sys.stderr)
        return 1

    try:
        records = scrape(url, timezone)
    except ScrapeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        output = json.dumps(records, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(output + "\n")
        else:
            print(output)
    else:
        if args.out:
            # BOM so Excel opens the CSV as UTF-8.
            with open(args.out, "w", encoding="utf-8-sig", newline="") as handle:
                write_csv(records, handle)
        else:
            buffer = io.StringIO()
            write_csv(records, buffer)
            sys.stdout.write(buffer.getvalue())

    if args.out:
        print(
            f"Wrote {len(records)} rows to {args.out} (source: {url})",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
