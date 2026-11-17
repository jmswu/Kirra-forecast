#!/usr/bin/env python3
"""Rule-based freedive outlook for Kirra.

The rules come from local freediver knowledge:

  * Best conditions follow a few days of gentle southerly winds.
  * Swell under 1 m is workable.
  * Swell under 0.5 m is worth swimming out to the reef for a look.
  * There is never a guarantee.

This module is deliberately deterministic: it turns that advice into visible
checks and a 48-hour outlook. It does not try to be an AI surf model.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, timedelta
from typing import Any

COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
]

RATING_PRIORITY = {"UNKNOWN": 0, "NO": 1, "WATCH": 2, "GOOD": 3, "REEF": 4}
RATING_LABELS = {
    "REEF": "REEF",
    "GOOD": "GOOD",
    "WATCH": "WATCH",
    "NO": "NO",
    "UNKNOWN": "NO DATA",
}
STATUS_LABELS = {
    "pass": "PASS",
    "partial": "PARTIAL",
    "fail": "FAIL",
    "unknown": "UNKNOWN",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compass_to_degrees(direction: str | None) -> float | None:
    if not direction:
        return None
    direction = direction.strip().upper()
    if direction == "CALM" or direction not in COMPASS_POINTS:
        return None
    return COMPASS_POINTS.index(direction) * 22.5


def in_southerly_sector(degrees: float | None, rules: dict) -> bool:
    if degrees is None:
        return False
    return (
        float(rules.get("southerly_min_deg", 135))
        <= degrees
        <= float(rules.get("southerly_max_deg", 225))
    )


def is_daylight(moment: datetime, rules: dict) -> bool:
    start = int(rules.get("daylight_start_hour", 5))
    end = int(rules.get("daylight_end_hour", 18))
    return start <= moment.hour < end


def _clean_observations(observations: list[dict]) -> list[dict]:
    # Calm/missing directions are still real observations: they count in the
    # denominator but can never be "gentle southerly".
    valid = [
        obs for obs in observations
        if obs.get("time") is not None
        and obs.get("speed_kmh") is not None
    ]
    return sorted(valid, key=lambda obs: obs["time"])


def _is_gentle_southerly(obs: dict, rules: dict) -> bool:
    return (
        obs.get("speed_kmh") is not None
        and float(obs["speed_kmh"]) <= float(rules.get("gentle_max_kmh", 20))
        and in_southerly_sector(obs.get("direction_deg"), rules)
    )


def _longest_gentle_southerly_run(window: list[dict], rules: dict) -> float:
    """Longest approximate run, tolerant of small gaps in observations."""
    max_gap = float(rules.get("observation_gap_minutes", 90))
    sample_hours = float(rules.get("sample_minutes", 30)) / 60.0
    best = 0.0
    run_start: datetime | None = None
    previous: datetime | None = None

    def finish() -> float:
        if run_start is None or previous is None:
            return 0.0
        return (previous - run_start).total_seconds() / 3600.0 + sample_hours

    for obs in window:
        if _is_gentle_southerly(obs, rules):
            if run_start is None:
                run_start = obs["time"]
                previous = obs["time"]
            elif (obs["time"] - previous).total_seconds() / 60.0 <= max_gap:
                previous = obs["time"]
            else:
                best = max(best, finish())
                run_start = obs["time"]
                previous = obs["time"]
        elif run_start is not None:
            best = max(best, finish())
            run_start = None
            previous = None

    if run_start is not None:
        best = max(best, finish())
    return round(best, 1)


def summarize_antecedent(
    observations: list[dict],
    rules: dict,
    end: datetime | None = None,
) -> dict:
    valid = _clean_observations(observations)
    if not valid:
        return {
            "status": "unknown",
            "status_label": STATUS_LABELS["unknown"],
            "fraction": 0.0,
            "gentle_southerly_count": 0,
            "total_count": 0,
            "longest_run_hours": 0.0,
            "mean_speed_kmh": None,
            "window_start": None,
            "window_end": None,
            "coverage_hours": 0.0,
            "latest": None,
            "stale_hours": None,
            "note": "No wind observations available.",
        }

    window_hours = float(rules.get("antecedent_window_hours", 72))
    end = end or valid[-1]["time"]
    start = end - timedelta(hours=window_hours)
    window = [obs for obs in valid if start <= obs["time"] <= end]
    if not window:
        window = valid[-1:]

    total = len(window)
    gentle = [obs for obs in window if _is_gentle_southerly(obs, rules)]
    fraction = len(gentle) / total if total else 0.0
    longest_run = _longest_gentle_southerly_run(window, rules)
    mean_speed = (
        sum(float(obs["speed_kmh"]) for obs in window) / total
        if total else None
    )
    coverage = (
        (window[-1]["time"] - window[0]["time"]).total_seconds() / 3600.0
        + float(rules.get("sample_minutes", 30)) / 60.0
        if total else 0.0
    )

    min_fraction = float(rules.get("antecedent_min_fraction", 0.50))
    min_run = float(rules.get("antecedent_min_run_hours", 24))
    partial_fraction = float(rules.get("antecedent_partial_fraction", 0.30))
    partial_run = float(rules.get("antecedent_partial_run_hours", 12))

    if total < int(rules.get("antecedent_min_observations", 12)):
        status = "unknown"
        note = "Not enough observations in the window."
    elif fraction >= min_fraction and longest_run >= min_run:
        status = "pass"
        note = "A sustained gentle southerly pattern is present."
    elif fraction >= partial_fraction or longest_run >= partial_run:
        status = "partial"
        note = "Some gentle southerly wind, but not yet a sustained setup."
    else:
        status = "fail"
        note = "No sustained gentle southerly pattern in the last 72 hours."

    latest = valid[-1]
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "fraction": round(fraction, 3),
        "gentle_southerly_count": len(gentle),
        "total_count": total,
        "longest_run_hours": longest_run,
        "mean_speed_kmh": round(mean_speed, 1) if mean_speed is not None else None,
        "window_start": window[0]["time"],
        "window_end": window[-1]["time"],
        "coverage_hours": round(coverage, 1),
        "latest": {
            "time": latest["time"],
            "speed_kmh": latest.get("speed_kmh"),
            "gust_kmh": latest.get("gust_kmh"),
            "direction": latest.get("direction"),
            "direction_deg": latest.get("direction_deg"),
        },
        "stale_hours": None,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

def classify_swell(height: float | None, rules: dict) -> str:
    if height is None:
        return "unknown"
    if height < float(rules.get("swell_reef_max_m", 0.5)):
        return "reef"
    if height < float(rules.get("swell_good_max_m", 1.0)):
        return "good"
    if height < float(rules.get("swell_no_min_m", 1.5)):
        return "marginal"
    return "no"


def classify_wind(speed: float | None, degrees: float | None, rules: dict) -> str:
    if speed is None:
        return "unknown"
    if speed > float(rules.get("strong_wind_kmh", 25)):
        return "strong"
    if speed <= float(rules.get("gentle_max_kmh", 20)):
        return "ideal" if in_southerly_sector(degrees, rules) else "ok"
    return "moderate"


def evaluate_block(block: dict, antecedent: dict, rules: dict) -> dict:
    swell_level = classify_swell(block.get("swell_height_m"), rules)
    wind_level = classify_wind(
        block.get("wind_speed_kmh"),
        block.get("wind_direction_deg"),
        rules,
    )
    daylight = is_daylight(block["time"], rules)
    rating = "NO"
    reason = ""

    if block.get("swell_height_m") is None:
        rating = "UNKNOWN"
        reason = "No swell data for this time."
    elif swell_level == "reef":
        if wind_level == "strong":
            rating = "WATCH"
            reason = "Reef-sized swell, but strong wind."
        else:
            rating = "REEF"
            reason = "Swell below 0.5m."
    elif swell_level == "good":
        if antecedent["status"] == "pass" and wind_level == "ideal":
            rating = "GOOD"
            reason = "Sub-1m swell after a sustained southerly setup."
        elif antecedent["status"] != "pass" and wind_level == "ideal":
            rating = "WATCH"
            reason = "Sub-1m swell and a good wind, but no sustained southerly setup."
        elif antecedent["status"] == "pass":
            rating = "WATCH"
            reason = "Sub-1m swell, but the wind is not a gentle southerly."
        else:
            rating = "WATCH"
            reason = "Sub-1m swell, but neither the setup nor the wind is right."
    elif swell_level == "marginal":
        rating = "WATCH"
        reason = "Swell is 1.0-1.5m; close, but above the freediver's 1m guide."
    else:
        rating = "NO"
        reason = "Swell is 1.5m or more."

    if rating in ("REEF", "GOOD") and not daylight:
        reason += " Outside daylight hours."

    return {
        **block,
        "swell_level": swell_level,
        "wind_level": wind_level,
        "daylight": daylight,
        "rating": rating,
        "rating_label": RATING_LABELS[rating],
        "reason": reason,
        "alertable": rating in ("REEF", "GOOD") and daylight and wind_level != "strong",
    }


# ---------------------------------------------------------------------------
# Outlook
# ---------------------------------------------------------------------------

def _block_sort_key(block: dict) -> datetime:
    return block["time"]


def _segments(blocks: list[dict], predicate) -> list[list[dict]]:
    segments: list[list[dict]] = []
    current: list[dict] = []
    for block in blocks:
        if predicate(block):
            current.append(block)
        elif current:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


def _best_segment(segments: list[list[dict]]) -> list[dict] | None:
    if not segments:
        return None

    def key(segment: list[dict]) -> tuple:
        alertable = any(block["alertable"] for block in segment)
        rating = max(RATING_PRIORITY[block["rating"]] for block in segment)
        return (alertable, rating, len(segment), -segment[0]["time"].timestamp())

    return max(segments, key=key)


def _window_payload(segment: list[dict], rules: dict) -> dict:
    alertable_blocks = [block for block in segment if block["alertable"]]
    rating_blocks = alertable_blocks or [
        block for block in segment if block["rating"] in ("REEF", "GOOD")
    ]
    if not rating_blocks:
        rating_blocks = segment
    rating = max((block["rating"] for block in rating_blocks), key=RATING_PRIORITY.get)
    heights = [
        block["swell_height_m"] for block in segment
        if block.get("swell_height_m") is not None
    ]
    block_hours = float(rules.get("block_hours", 3))
    start = segment[0]["time"]
    end = segment[-1]["time"] + timedelta(hours=block_hours)
    peak = max(rating_blocks, key=lambda block: RATING_PRIORITY[block["rating"]])
    return {
        "start": start,
        "end": end,
        "rating": rating,
        "alertable": any(block["alertable"] for block in segment),
        "min_swell_m": round(min(heights), 2) if heights else None,
        "max_swell_m": round(max(heights), 2) if heights else None,
        "peak_wind_kmh": peak.get("wind_speed_kmh"),
        "peak_wind_direction": peak.get("wind_direction"),
        "blocks": segment,
    }


def _format_time(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    return moment.strftime("%a %d %b %H:%M")


def _wind_text(block: dict) -> str:
    speed = block.get("wind_speed_kmh")
    direction = block.get("wind_direction") or ""
    if speed is None:
        return "wind n/a"
    return f"{direction} {speed:g} km/h".strip()


def build_outlook(
    blocks: list[dict],
    antecedent: dict,
    rules: dict,
    now: datetime,
) -> dict:
    horizon = now + timedelta(hours=float(rules.get("forecast_hours", 48)))
    visible = [
        block for block in sorted(blocks, key=_block_sort_key)
        if now <= block["time"] <= horizon
    ]
    evaluated = [evaluate_block(block, antecedent, rules) for block in visible]

    good_segments = _segments(
        evaluated,
        lambda block: block["rating"] in ("REEF", "GOOD"),
    )
    best_segment = _best_segment(good_segments)
    best_window = _window_payload(best_segment, rules) if best_segment else None

    has_alertable = bool(best_window and best_window["alertable"])
    daylight_watch = [
        block for block in evaluated
        if block["rating"] == "WATCH" and block["daylight"]
    ]
    if has_alertable and best_window["rating"] == "REEF":
        verdict = "REEF WINDOW"
        level = "reef"
    elif has_alertable and best_window["rating"] == "GOOD":
        verdict = "GOOD WINDOW"
        level = "good"
    elif best_window or daylight_watch:
        verdict = "WATCH"
        level = "watch"
    else:
        verdict = "NO WINDOW"
        level = "no"

    swell_values = [
        block["swell_height_m"] for block in evaluated
        if block.get("swell_height_m") is not None
    ]
    min_swell = min(swell_values) if swell_values else None
    max_swell = max(swell_values) if swell_values else None
    if evaluated:
        peak_block = max(
            (block for block in evaluated if block.get("swell_height_m") is not None),
            key=lambda block: block["swell_height_m"],
            default=None,
        )
    else:
        peak_block = None

    if has_alertable:
        headline = (
            f"{verdict.title()}: {_format_time(best_window['start'])} to "
            f"{best_window['end'].strftime('%H:%M')} - "
            f"swell {best_window['min_swell_m']:g}-{best_window['max_swell_m']:g}m, "
            f"{_wind_text(best_window['blocks'][0])}."
        )
    elif best_window:
        headline = (
            "No daylight window; a swell/rating window exists at night: "
            f"{_format_time(best_window['start'])} to "
            f"{best_window['end'].strftime('%H:%M')}, "
            f"swell {best_window['min_swell_m']:g}-{best_window['max_swell_m']:g}m."
        )
    elif daylight_watch:
        closest = min(
            daylight_watch,
            key=lambda block: (block.get("swell_height_m") or 99, block["time"]),
        )
        above_guide = (
            closest.get("swell_height_m") is not None
            and closest["swell_height_m"] >= float(rules.get("swell_good_max_m", 1.0))
        )
        headline = (
            "No clear window. Closest daylight option is "
            f"{_format_time(closest['time'])}: swell "
            f"{closest['swell_height_m']:g}m, {_wind_text(closest)}"
            + (" - still above the 1m guide." if above_guide else ".")
        )
    elif any(block["rating"] == "WATCH" for block in evaluated):
        closest = min(
            (block for block in evaluated if block["rating"] == "WATCH"),
            key=lambda block: (block.get("swell_height_m") or 99, block["time"]),
        )
        headline = (
            "No daylight window. Lowest swell is "
            f"{closest['swell_height_m']:g}m at {_format_time(closest['time'])} "
            "(night), with " + _wind_text(closest) + "."
        )
    elif evaluated:
        closest = min(
            (block for block in evaluated if block.get("swell_height_m") is not None),
            key=lambda block: block["swell_height_m"],
            default=None,
        )
        if closest is not None:
            headline = (
                "No window in the next 48 hours. "
                f"Lowest swell is {closest['swell_height_m']:g}m "
                f"at {_format_time(closest['time'])}, "
                f"with {_wind_text(closest)}."
            )
        else:
            headline = "No usable forecast data in the next 48 hours."
    else:
        headline = "No forecast blocks available for the next 48 hours."

    if antecedent["status"] == "unknown":
        confidence = "low"
    elif has_alertable:
        hours_out = (best_window["start"] - now).total_seconds() / 3600.0
        if hours_out <= 24 and antecedent["status"] == "pass":
            confidence = "high"
        elif hours_out <= 36:
            confidence = "medium"
        else:
            confidence = "low"
    elif best_window:
        confidence = "low"
    else:
        confidence = "medium" if evaluated else "low"

    alert = None
    if has_alertable:
        alert = {
            "key": f"{best_window['rating']}:{best_window['start'].isoformat(timespec='minutes')}",
            "title": "Kirra freedive forecast",
            "subtitle": verdict,
            "message": headline,
        }

    return {
        "verdict": verdict,
        "verdict_level": level,
        "headline": headline,
        "confidence": confidence,
        "blocks": evaluated,
        "best_window": best_window,
        "alert": alert,
        "range": {
            "start": visible[0]["time"] if visible else None,
            "end": visible[-1]["time"] if visible else None,
            "min_swell_m": round(min_swell, 2) if min_swell is not None else None,
            "max_swell_m": round(max_swell, 2) if max_swell is not None else None,
            "peak_time": peak_block["time"] if peak_block else None,
        },
    }


# ---------------------------------------------------------------------------
# Report assembly and rendering
# ---------------------------------------------------------------------------

def build_report(
    blocks: list[dict],
    observations: list[dict],
    now: datetime,
    rules: dict,
    sources: dict,
    seven_day: list[dict] | None = None,
    tides: list[dict] | None = None,
    stale_hours: float | None = None,
) -> dict:
    antecedent = summarize_antecedent(observations, rules, end=now)
    antecedent["stale_hours"] = stale_hours
    if stale_hours is not None and stale_hours > 0:
        antecedent["note"] += f" BOM cache is {stale_hours:.1f}h old."

    outlook = build_outlook(blocks, antecedent, rules, now)
    tide_horizon = now + timedelta(hours=float(rules.get("forecast_hours", 48)))
    tide_events = [
        event for event in (tides or [])
        if now <= event["time"] <= tide_horizon
    ]

    next_potential = None
    next_potential_note = None
    if seven_day is None:
        next_potential_note = "7-day context unavailable."
    else:
        after = now + timedelta(hours=float(rules.get("forecast_hours", 48)))
        candidates = [
            block for block in seven_day
            if block["time"] > after
            and block.get("swell_height_m") is not None
            and block["swell_height_m"] < float(rules.get("swell_good_max_m", 1.0))
        ]
        if candidates:
            next_potential = min(candidates, key=lambda block: block["time"])
        else:
            next_potential_note = "No sub-1m swell block in the current 7-day table."

    return {
        "generated_at": now,
        "location": sources.get("location", "Kirra"),
        "verdict": outlook["verdict"],
        "verdict_level": outlook["verdict_level"],
        "headline": outlook["headline"],
        "confidence": outlook["confidence"],
        "antecedent": antecedent,
        "blocks": outlook["blocks"],
        "best_window": outlook["best_window"],
        "alert": outlook["alert"],
        "range": outlook["range"],
        "tides": tide_events,
        "all_tides": tides or [],
        "next_tide": tide_events[0] if tide_events else None,
        "next_potential": next_potential,
        "next_potential_note": next_potential_note,
        "rules": rules,
        "sources": sources,
    }


def _fmt_num(value: Any, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}{suffix}"


def _fmt_time(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    return moment.strftime("%a %d %b %H:%M")


def render_markdown(report: dict) -> str:
    rule = report["rules"]
    antecedent = report["antecedent"]
    stale = antecedent.get("stale_hours")
    lines = [
        "# Kirra freedive outlook",
        "",
        f"**Generated:** {report['generated_at'].strftime('%A %d %B %Y, %I:%M %p %Z')}",
        f"**Verdict:** **{report['verdict']}**",
        f"**Confidence:** {report['confidence'].title()}",
        "",
        f"> {report['headline']}",
        "",
    ]

    if stale is not None and stale > 0:
        lines += [
            f"> **Warning:** wind observations are {stale:.1f}h old; the setup check is less reliable.",
            "",
        ]

    latest = antecedent.get("latest")
    if latest:
        latest_text = (
            f"Latest observation: **{latest.get('direction') or 'CALM'} "
            f"{_fmt_num(latest.get('speed_kmh'), 0, ' km/h')}** at "
            f"{_fmt_time(latest['time'])}."
        )
    else:
        latest_text = "Latest observation unavailable."

    lines += [
        "## Past 72 hours at Coolangatta",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Observations | {antecedent['total_count']} ({_fmt_time(antecedent['window_start'])} to {_fmt_time(antecedent['window_end'])}) |",
        f"| Gentle southerly | {antecedent['fraction'] * 100:.0f}% ({antecedent['gentle_southerly_count']}/{antecedent['total_count']}) |",
        f"| Longest run | {antecedent['longest_run_hours']:.1f} hours |",
        f"| Mean wind | {_fmt_num(antecedent['mean_speed_kmh'], 1, ' km/h')} |",
        f"| Setup | **{antecedent['status_label']}** |",
        "",
        latest_text,
        "",
        f"_{antecedent['note']}_",
        "",
        "## Next 48 hours",
        "",
        "| Time (AEST) | Swell | Period | Wind | Rating | Why |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for block in report["blocks"]:
        time_label = block["time"].strftime("%a %d %b %H:%M")
        if not block["daylight"]:
            time_label += " (night)"
        swell = (
            f"{block['swell_height_m']:g}m {block['swell_direction']}"
            if block.get("swell_height_m") is not None else "-"
        )
        period = f"{block['swell_period_s']}s" if block.get("swell_period_s") else "-"
        wind = (
            f"{block['wind_direction']} {block['wind_speed_kmh']:g} km/h"
            if block.get("wind_speed_kmh") is not None else "-"
        )
        lines.append(
            f"| {time_label} | {swell} | {period} | {wind} | "
            f"**{block['rating_label']}** | {block['reason']} |"
        )

    if not report["blocks"]:
        lines.append("| - | - | - | - | - | No forecast blocks. |")

    lines += [
        "",
        "## Tides (next 48 hours)",
        "",
        "| Tide | Time | Height |",
        "| --- | --- | --- |",
    ]
    if report.get("tides"):
        for event in report["tides"]:
            lines.append(
                f"| {event['type'].title()} | {_fmt_time(event['time'])} | "
                f"{_fmt_num(event['height_m'], 2, 'm')} |"
            )
    else:
        lines.append("| - | - | No tide data available. |")

    lines += ["", "## Call", ""]
    if report["best_window"]:
        window = report["best_window"]
        lines += [
            (
                f"**Best window:** {_fmt_time(window['start'])} to "
                f"{window['end'].strftime('%H:%M')} "
                f"({window['rating']}, alertable: "
                f"{'yes' if window['alertable'] else 'no'})"
            ),
            (
                f"Swell range: {_fmt_num(window['min_swell_m'], 1, 'm')} to "
                f"{_fmt_num(window['max_swell_m'], 1, 'm')}."
            ),
            "",
        ]
    else:
        lines += ["**Best window:** no actionable window in the next 48 hours.", ""]

    if report["next_potential"]:
        nxt = report["next_potential"]
        lines += [
            "## Next potential (beyond 48h, context only)",
            "",
            (
                f"{_fmt_time(nxt['time'])}: swell {nxt['swell_height_m']:g}m "
                f"{nxt.get('swell_direction') or ''}, wind "
                f"{nxt.get('wind_direction') or ''} "
                f"{_fmt_num(nxt.get('wind_speed_kmh'), 0, ' km/h')}."
            ),
            "",
        ]
    else:
        lines += [
            "## Next potential (beyond 48h, context only)",
            "",
            report.get("next_potential_note")
            or "No sub-1m swell block in the current 7-day table.",
            "",
        ]

    lines += [
        "## How the call is made",
        "",
        f"- Southerly sector: {rule['southerly_min_deg']:g}° to {rule['southerly_max_deg']:g}°",
        f"- Gentle wind: ≤ {rule['gentle_max_kmh']:g} km/h",
        (
            f"- Setup pass: ≥ {rule['antecedent_min_fraction'] * 100:.0f}% "
            f"of the last {rule['antecedent_window_hours']:g}h and a run of "
            f"≥ {rule['antecedent_min_run_hours']:g}h"
        ),
        f"- Reef trigger: swell < {rule['swell_reef_max_m']:g}m",
        f"- Good guide: swell < {rule['swell_good_max_m']:g}m",
        f"- No-go guide: swell ≥ {rule['swell_no_min_m']:g}m",
        "- Night blocks are shown but do not trigger alerts.",
        "",
        "## Sources",
        "",
        f"- Wind history: {report['sources'].get('bom_attribution', 'Bureau of Meteorology')}",
        f"- Forecast: {report['sources'].get('forecast_url', 'surf-forecast.com')}",
        "- Swell data: NOAA WaveWatch III via surf-forecast.com",
        "- Tide times/heights: surf-forecast.com",
        "",
        (
            "_This is a rule-based outlook built from local freediver experience. "
            "It is guidance, not a guarantee. Check the water and your own limits._"
        ),
        "",
    ]
    return "\n".join(lines)


def _html_rating_class(rating: str) -> str:
    return {
        "REEF": "reef",
        "GOOD": "good",
        "WATCH": "watch",
        "NO": "no",
        "UNKNOWN": "unknown",
    }.get(rating, "unknown")


CHART_WIDTH = 860
CHART_HEIGHT = 320
CHART_LEFT = 48
CHART_RIGHT = 62
CHART_TOP = 26
CHART_BOTTOM = 52
CHART_PLOT_WIDTH = CHART_WIDTH - CHART_LEFT - CHART_RIGHT
CHART_PLOT_HEIGHT = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM


def _chart_x(moment: datetime, start: datetime, end: datetime) -> float:
    span = (end - start).total_seconds() or 1
    ratio = (moment - start).total_seconds() / span
    return CHART_LEFT + ratio * CHART_PLOT_WIDTH


def _chart_y(value: float, low: float, high: float) -> float:
    span = (high - low) or 1
    return CHART_TOP + CHART_PLOT_HEIGHT - (value - low) / span * CHART_PLOT_HEIGHT


def _chart_ticks(start: datetime, end: datetime, step_hours: int):
    tick = start.replace(minute=0, second=0, microsecond=0)
    tick += timedelta(hours=(step_hours - tick.hour % step_hours) % step_hours)
    if tick <= start:
        tick += timedelta(hours=step_hours)
    while tick <= end:
        yield tick
        tick += timedelta(hours=step_hours)


def _chart_label(moment: datetime) -> str:
    if moment.hour == 0:
        return moment.strftime("%a %H:%M")
    return moment.strftime("%H:%M")


def _tide_height_at(moment: datetime, events: list[dict]) -> float | None:
    if not events:
        return None
    if moment <= events[0]["time"]:
        return events[0]["height_m"]
    if moment >= events[-1]["time"]:
        return events[-1]["height_m"]
    for left, right in zip(events, events[1:]):  # noqa: RUF007 - py3.9 support
        if left["time"] <= moment <= right["time"]:
            span = (right["time"] - left["time"]).total_seconds()
            if not span:
                return left["height_m"]
            fraction = (moment - left["time"]).total_seconds() / span
            smooth = (1 - math.cos(math.pi * fraction)) / 2
            return left["height_m"] + (right["height_m"] - left["height_m"]) * smooth
    return None


def _height_axis_label(value: float) -> str:
    label = f"{value:.2f}".rstrip("0").rstrip(".")
    if "." not in label:
        label += ".0"
    return f"{label}m"


def render_combined_chart(report: dict) -> str:
    rules = report["rules"]
    start = report["generated_at"]
    end = start + timedelta(hours=float(rules.get("forecast_hours", 48)))
    blocks = [
        block for block in report.get("blocks", [])
        if start <= block["time"] <= end
    ]
    wind_blocks = [
        block for block in blocks if block.get("wind_speed_kmh") is not None
    ]
    swell_blocks = [
        block for block in blocks if block.get("swell_height_m") is not None
    ]
    tide_events = sorted(
        [
            event for event in (report.get("all_tides") or report.get("tides") or [])
            if event.get("height_m") is not None
        ],
        key=lambda event: event["time"],
    )
    if not wind_blocks and not swell_blocks and len(tide_events) < 2:
        return "<p class=\"muted\">No wind, swell or tide data in the next 48 hours.</p>"

    max_wind = max(
        (float(block["wind_speed_kmh"]) for block in wind_blocks),
        default=0.0,
    )
    wind_max = max(20, math.ceil(max_wind / 10) * 10)
    height_values = [
        float(block["swell_height_m"]) for block in swell_blocks
    ] + [float(event["height_m"]) for event in tide_events]
    height_max = max(1.5, math.ceil(max(height_values, default=1.5) * 2) / 2)
    if height_values and max(height_values) > height_max - 0.1:
        height_max += 0.5
    height_min = 0.0
    if height_values and min(height_values) < 0:
        height_min = math.floor(min(height_values) * 10) / 10

    def x(moment: datetime) -> float:
        return _chart_x(moment, start, end)

    def y_wind(value: float) -> float:
        return _chart_y(value, 0, wind_max)

    def y_height(value: float) -> float:
        return _chart_y(value, height_min, height_max)

    plot_right = CHART_LEFT + CHART_PLOT_WIDTH
    plot_bottom = CHART_TOP + CHART_PLOT_HEIGHT

    parts = [
        (
            f"<svg class=\"chart\" viewBox=\"0 0 {CHART_WIDTH} {CHART_HEIGHT}\" "
            "role=\"img\" aria-label=\"Swell, wind and tide over the next 48 hours\">"
        ),
        "<title>Swell, wind and tide, next 48 hours</title>",
        (
            "<desc>Blue line is wind speed and arrows show downwind direction. "
            "Green line is swell height. Teal curve is tide height.</desc>"
        ),
        (
            f"<rect x=\"{CHART_LEFT}\" y=\"{CHART_TOP}\" width=\"{CHART_PLOT_WIDTH}\" "
            f"height=\"{CHART_PLOT_HEIGHT}\" fill=\"#f8fbfd\"/>"
        ),
    ]

    for value in range(0, int(wind_max) + 1, 10):
        y = y_wind(value)
        parts.append(
            f"<line class=\"grid\" x1=\"{CHART_LEFT}\" y1=\"{y:.1f}\" "
            f"x2=\"{plot_right}\" y2=\"{y:.1f}\"/>"
        )
        parts.append(
            f"<text class=\"axis axis-left\" x=\"{CHART_LEFT - 8}\" "
            f"y=\"{y + 4:.1f}\" text-anchor=\"end\">{value}</text>"
        )

    height_step = 0.5 if height_max - height_min >= 1.5 else 0.25
    height_tick = height_min
    while height_tick <= height_max + 1e-9:
        y = y_height(height_tick)
        parts.append(
            f"<line class=\"tick\" x1=\"{plot_right}\" y1=\"{y:.1f}\" "
            f"x2=\"{plot_right + 5}\" y2=\"{y:.1f}\"/>"
        )
        parts.append(
            f"<text class=\"axis axis-right\" x=\"{plot_right + 8}\" "
            f"y=\"{y + 4:.1f}\" text-anchor=\"start\">"
            f"{_height_axis_label(height_tick)}</text>"
        )
        height_tick += height_step

    for tick in _chart_ticks(start, end, 6):
        tick_x = x(tick)
        parts.append(
            f"<line class=\"grid\" x1=\"{tick_x:.1f}\" y1=\"{CHART_TOP}\" "
            f"x2=\"{tick_x:.1f}\" y2=\"{plot_bottom}\"/>"
        )
        parts.append(
            f"<text class=\"axis\" x=\"{tick_x:.1f}\" y=\"{plot_bottom + 18}\" "
            f"text-anchor=\"middle\">{_chart_label(tick)}</text>"
        )

    if len(tide_events) >= 2:
        tide_samples = []
        for minute in range(0, 48 * 60 + 1, 15):
            moment = start + timedelta(minutes=minute)
            height = _tide_height_at(moment, tide_events)
            if height is not None:
                tide_samples.append((moment, height))
        if len(tide_samples) >= 2:
            tide_points = [
                (x(moment), y_height(height)) for moment, height in tide_samples
            ]
            tide_path = "M " + " L ".join(
                f"{px:.1f},{py:.1f}" for px, py in tide_points
            )
            tide_area = (
                tide_path
                + f" L {tide_points[-1][0]:.1f},{plot_bottom:.1f}"
                + f" L {tide_points[0][0]:.1f},{plot_bottom:.1f} Z"
            )
            parts.extend([
                f"<path class=\"tide-area\" d=\"{tide_area}\"/>",
                f"<path class=\"tide-line\" d=\"{tide_path}\"/>",
            ])
            for event in tide_events:
                if not start <= event["time"] <= end:
                    continue
                marker_x = x(event["time"])
                marker_y = y_height(float(event["height_m"]))
                is_high = event["type"] == "high"
                title = (
                    f"{event['type'].title()} tide at "
                    f"{event['time'].strftime('%a %d %b %H:%M')}, "
                    f"{event['height_m']:.2f}m"
                )
                parts.append(
                    f"<circle class=\"tide-marker\" cx=\"{marker_x:.1f}\" "
                    f"cy=\"{marker_y:.1f}\" r=\"3.5\" "
                    f"fill=\"{'#0e7490' if is_high else '#ffffff'}\" "
                    f"stroke=\"#0e7490\" stroke-width=\"1.5\">"
                    f"<title>{html.escape(title)}</title></circle>"
                )

    if swell_blocks:
        swell_points = [
            (x(block["time"]), y_height(float(block["swell_height_m"])))
            for block in swell_blocks
        ]
        swell_path = "M " + " L ".join(
            f"{px:.1f},{py:.1f}" for px, py in swell_points
        )
        parts.append(f"<path class=\"swell-line\" d=\"{swell_path}\"/>")
        for block, (px, py) in zip(swell_blocks, swell_points):
            title = (
                f"{block['time'].strftime('%a %d %b %H:%M')} - swell "
                f"{block['swell_height_m']:g}m {block.get('swell_direction') or ''}"
                f" ({block.get('swell_period_s') or '-'}s)"
            )
            parts.append(
                f"<circle class=\"swell-dot\" cx=\"{px:.1f}\" cy=\"{py:.1f}\" "
                f"r=\"3\"><title>{html.escape(title)}</title></circle>"
            )

    if wind_blocks:
        wind_points = [
            (x(block["time"]), y_wind(float(block["wind_speed_kmh"])))
            for block in wind_blocks
        ]
        wind_path = "M " + " L ".join(
            f"{px:.1f},{py:.1f}" for px, py in wind_points
        )
        parts.append(f"<path class=\"wind-line\" d=\"{wind_path}\"/>")
        for block, (px, py) in zip(wind_blocks, wind_points):
            speed = float(block["wind_speed_kmh"])
            direction = block.get("wind_direction") or ""
            title = (
                f"{block['time'].strftime('%a %d %b %H:%M')} - "
                f"wind from {direction or '?'} at {speed:g} km/h"
            )
            degrees = block.get("wind_direction_deg")
            if degrees is not None:
                rotation = (float(degrees) + 180.0) % 360.0
                parts.append(
                    f"<g class=\"wind-arrow\" transform=\"translate({px:.1f} {py:.1f}) "
                    f"rotate({rotation:.1f})\">"
                    f"<title>{html.escape(title)}</title>"
                    "<polygon points=\"0,-12 6,2 0,-2 -6,2\" "
                    "stroke=\"#f8fbfd\" stroke-width=\"1\"/>"
                    "</g>"
                )
            else:
                parts.append(
                    f"<circle cx=\"{px:.1f}\" cy=\"{py:.1f}\" r=\"3\" "
                    f"fill=\"#1d5fd1\"><title>{html.escape(title)}</title></circle>"
                )

    parts.extend([
        (
            f"<text class=\"axis axis-left\" x=\"{CHART_LEFT - 8}\" "
            f"y=\"{CHART_TOP - 9}\" text-anchor=\"end\">km/h</text>"
        ),
        (
            f"<text class=\"axis axis-right\" x=\"{plot_right + 8}\" "
            f"y=\"{CHART_TOP - 9}\" text-anchor=\"start\">m</text>"
        ),
        (
            f"<text class=\"axis\" x=\"{plot_right}\" "
            f"y=\"{CHART_HEIGHT - 10}\" text-anchor=\"end\">AEST</text>"
        ),
        "</svg>",
    ])
    return "".join(parts)


def render_html(report: dict) -> str:
    rule = report["rules"]
    antecedent = report["antecedent"]
    stale = antecedent.get("stale_hours")
    e = html.escape

    rows = []
    wind_rows = []
    for block in report["blocks"]:
        time_label = block["time"].strftime("%a %d %b %H:%M")
        if not block["daylight"]:
            time_label += " <span class=\"night\">night</span>"
        swell = (
            f"{block['swell_height_m']:g}m {e(block.get('swell_direction') or '')}"
            if block.get("swell_height_m") is not None else "-"
        )
        period = f"{block['swell_period_s']}s" if block.get("swell_period_s") else "-"
        degrees = (
            f"{block['wind_direction_deg']:g}°"
            if block.get("wind_direction_deg") is not None else "-"
        )
        rows.append(
            "<tr class=\"row-{cls}\">"
            "<td>{time}</td><td>{swell}</td><td>{period}</td>"
            "<td><span class=\"pill {cls}\">{label}</span></td>"
            "<td class=\"why\">{why}</td></tr>".format(
                cls=_html_rating_class(block["rating"]),
                time=time_label,
                swell=swell,
                period=period,
                label=block["rating_label"],
                why=e(block["reason"]),
            )
        )
        wind_rows.append(
            "<tr class=\"{cls}\"><td>{time}</td><td>{speed}</td>"
            "<td>{direction}</td><td>{degrees}</td></tr>".format(
                cls="row-night" if not block["daylight"] else "",
                time=time_label,
                speed=(
                    f"{block['wind_speed_kmh']:g} km/h"
                    if block.get("wind_speed_kmh") is not None else "-"
                ),
                direction=e(block.get("wind_direction") or "-"),
                degrees=degrees,
            )
        )
    if not rows:
        rows.append("<tr><td colspan=\"5\">No forecast blocks available.</td></tr>")
    if not wind_rows:
        wind_rows.append("<tr><td colspan=\"4\">No wind data available.</td></tr>")

    tide_rows = []
    for event in report.get("tides", []):
        tide_rows.append(
            "<tr><td><strong>{kind}</strong></td><td>{time}</td><td>{height}</td></tr>".format(
                kind=event["type"].title(),
                time=_fmt_time(event["time"]),
                height=_fmt_num(event["height_m"], 2, "m"),
            )
        )
    if not tide_rows:
        tide_rows.append(
            "<tr><td colspan=\"3\" class=\"muted\">No tide data available.</td></tr>"
        )

    alert_class = report["verdict_level"]
    stale_html = ""
    if stale is not None and stale > 0:
        stale_html = (
            f"<div class=\"warning\">Wind observations are {stale:.1f}h old. "
            "The setup check is less reliable.</div>"
        )

    best_html = "<p class=\"muted\">No actionable window in the next 48 hours.</p>"
    if report["best_window"]:
        w = report["best_window"]
        best_html = (
            f"<p><strong>{_fmt_time(w['start'])} to {w['end'].strftime('%H:%M')}</strong> "
            f"&middot; {w['rating']} &middot; swell "
            f"{_fmt_num(w['min_swell_m'], 1, 'm')} to {_fmt_num(w['max_swell_m'], 1, 'm')} "
            f"&middot; alertable: {'yes' if w['alertable'] else 'no'}</p>"
        )

    next_html = (
        f"<p class=\"muted\">"
        f"{e(report.get('next_potential_note') or 'No sub-1m swell block in the 7-day table.')}"
        f"</p>"
    )
    if report["next_potential"]:
        nxt = report["next_potential"]
        next_html = (
            f"<p><strong>{_fmt_time(nxt['time'])}</strong> &middot; "
            f"swell {nxt['swell_height_m']:g}m {e(nxt.get('swell_direction') or '')} "
            f"&middot; wind {e(nxt.get('wind_direction') or '')} "
            f"{_fmt_num(nxt.get('wind_speed_kmh'), 0, ' km/h')}</p>"
        )

    latest_html = "<span class=\"muted\">Latest observation unavailable.</span>"
    if antecedent.get("latest"):
        latest = antecedent["latest"]
        latest_html = (
            f"Latest at {_fmt_time(latest['time'])}: "
            f"<strong>{e(latest.get('direction') or '')} "
            f"{_fmt_num(latest.get('speed_kmh'), 0, ' km/h')}</strong>"
            f" (gust {_fmt_num(latest.get('gust_kmh'), 0, ' km/h')})"
        )

    combined_chart = render_combined_chart(report)

    sidebar_html = f"""
<aside class="sidebar" aria-label="Rating legend">
  <section class="legend-card">
    <h2>Rating guide</h2>
    <div class="legend-list">
      <div class="legend-item">
        <span class="pill reef">REEF</span>
        <div>
          <strong>Under {rule['swell_reef_max_m']:g}m</strong>
          <p>Worth a look at the reef. Alert sent.</p>
        </div>
      </div>
      <div class="legend-item">
        <span class="pill good">GOOD</span>
        <div>
          <strong>Under {rule['swell_good_max_m']:g}m</strong>
          <p>Southerly setup passed and the wind is a gentle southerly. Alert sent.</p>
        </div>
      </div>
      <div class="legend-item">
        <span class="pill watch">WATCH</span>
        <div>
          <strong>Close, but not on</strong>
          <p>No actionable window yet. No alert.</p>
        </div>
      </div>
      <div class="legend-item">
        <span class="pill no">NO</span>
        <div>
          <strong>{rule['swell_no_min_m']:g}m or more</strong>
          <p>Too much swell, or strong wind. No alert.</p>
        </div>
      </div>
    </div>
    <div class="legend-note">
      <strong>Alerts:</strong> only REEF and GOOD send a macOS, ntfy or email
      alert. Night blocks never alert. Tide is shown for planning, not scored.
    </div>
  </section>
</aside>
"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kirra freedive outlook</title>
<style>
  :root {{
    color-scheme: light;
    --ink: #17212b;
    --muted: #607080;
    --line: #dce3ea;
    --reef: #087f5b;
    --reef-bg: #e6f7f0;
    --good: #1d5fd1;
    --good-bg: #e8f0ff;
    --watch: #9a6700;
    --watch-bg: #fff5d6;
    --no: #5c6672;
    --no-bg: #eef1f4;
    --unknown: #a33a2b;
    --unknown-bg: #fbe9e6;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: #f4f7fa;
    color: var(--ink);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .page {{
    max-width: 1340px;
    margin: 0 auto;
    padding: 28px 18px 48px;
    display: grid;
    grid-template-columns: minmax(0, 1fr) 280px;
    grid-template-areas: "hero side" "main side";
    gap: 20px;
    align-items: start;
  }}
  header {{
    grid-area: hero;
  }}
  main {{ grid-area: main; min-width: 0; }}
  .sidebar {{
    grid-area: side;
    position: sticky;
    top: 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }}
  .legend-card {{
    background: white;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 16px;
  }}
  .sidebar h2 {{ margin: 0 0 10px; font-size: 15px; }}
  .legend-list {{ display: flex; flex-direction: column; gap: 11px; }}
  .legend-item {{ display: flex; gap: 9px; align-items: flex-start; }}
  .legend-item .pill {{ flex: 0 0 auto; margin-top: 2px; }}
  .legend-item strong {{ display: block; font-size: 13px; }}
  .legend-item p {{
    margin: 2px 0 0;
    color: var(--muted);
    font-size: 12px;
    line-height: 1.45;
  }}
  .legend-note {{
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: 12px;
    line-height: 1.5;
  }}
  .legend-note strong {{ color: var(--ink); }}
  .sidebar .badge {{ margin-top: 0; }}
  .sidebar .legend-headline {{ font-size: 13px; color: var(--muted); margin: 8px 0 0; }}
  header {{
    background: linear-gradient(135deg, #082f49, #0b4f6c);
    color: white;
    border-radius: 18px;
    padding: 24px 26px;
    box-shadow: 0 10px 30px rgba(8, 47, 73, .18);
  }}
  header h1 {{ margin: 0 0 4px; font-size: 28px; letter-spacing: -.02em; }}
  header .sub {{ color: #c9e5f2; font-size: 14px; }}
  .badge {{
    display: inline-block;
    margin-top: 16px;
    padding: 7px 12px;
    border-radius: 999px;
    background: white;
    color: #0b4f6c;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: .04em;
    text-transform: uppercase;
  }}
  .badge.reef {{ background: var(--reef-bg); color: var(--reef); }}
  .badge.good {{ background: var(--good-bg); color: var(--good); }}
  .badge.watch {{ background: var(--watch-bg); color: var(--watch); }}
  .badge.no {{ background: var(--no-bg); color: var(--no); }}
  .headline {{ margin: 12px 0 0; font-size: 17px; color: #eaf6fb; }}
  .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 18px 0; }}
  .card {{
    background: white;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 14px 16px;
  }}
  .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }}
  .card .value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
  .card .detail {{ color: var(--muted); font-size: 13px; margin-top: 2px; }}
  .status {{ font-weight: 700; }}
  .status.pass {{ color: var(--reef); }}
  .status.partial {{ color: var(--watch); }}
  .status.fail {{ color: var(--unknown); }}
  .status.unknown {{ color: var(--muted); }}
  h2 {{ font-size: 18px; margin: 28px 0 10px; }}
  .panel {{
    background: white;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 18px;
    overflow-x: auto;
  }}
  table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
  table.tide-table {{ min-width: 420px; }}
  th, td {{ text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
  tr:last-child td {{ border-bottom: 0; }}
  .why {{ color: var(--muted); font-size: 13px; }}
  .night {{ color: var(--muted); font-size: 11px; text-transform: uppercase; }}
  .pill {{
    display: inline-block; padding: 3px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 700; letter-spacing: .03em; white-space: nowrap;
  }}
  .pill.reef {{ background: var(--reef-bg); color: var(--reef); }}
  .pill.good {{ background: var(--good-bg); color: var(--good); }}
  .pill.watch {{ background: var(--watch-bg); color: var(--watch); }}
  .pill.no {{ background: var(--no-bg); color: var(--no); }}
  .pill.unknown {{ background: var(--unknown-bg); color: var(--unknown); }}
  .row-reef td {{ background: #fbfffd; }}
  .row-good td {{ background: #fbfdff; }}
  .warning {{
    background: var(--unknown-bg); color: var(--unknown);
    border-radius: 10px; padding: 10px 12px; margin: 12px 0; font-size: 14px;
  }}
  .muted {{ color: var(--muted); }}
  .callout {{
    background: #f8fbfd; border-left: 4px solid #0b4f6c;
    padding: 12px 14px; border-radius: 0 10px 10px 0;
  }}
  .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
  .chart-panel {{ padding: 12px 14px 8px; }}
  .chart {{ display: block; width: 100%; height: auto; }}
  .chart text {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  .chart .grid {{ stroke: #dce3ea; stroke-width: 1; }}
  .chart .axis {{ fill: #607080; font-size: 10px; }}
  .chart .axis-left {{ fill: #1d5fd1; }}
  .chart .axis-right {{ fill: #0e7490; }}
  .chart .tick {{ stroke: #0e7490; stroke-width: 1; }}
  .chart .wind-line {{ fill: none; stroke: #1d5fd1; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; }}
  .chart .wind-arrow {{ fill: #0b4f6c; }}
  .chart .swell-line {{ fill: none; stroke: #0f9d76; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; }}
  .chart .swell-dot {{ fill: #0f9d76; }}
  .chart .tide-area {{ fill: #e0f2fe; opacity: .6; }}
  .chart .tide-line {{ fill: none; stroke: #0e7490; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }}
  .chart .tide-label {{ fill: #0e7490; font-size: 10px; font-weight: 600; }}
  .chart-legend {{
    display: flex; justify-content: space-between; gap: 10px;
    color: var(--muted); font-size: 12px; margin: 0 2px 4px;
  }}
  .line-key {{ display: inline-block; width: 18px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 4px; }}
  .line-key.wind {{ background: #1d5fd1; }}
  .line-key.swell {{ background: #0f9d76; }}
  .line-key.tide {{ background: #0e7490; }}
  .chart-hint {{ margin-left: auto; }}
  details.details {{ margin-top: 8px; }}
  details.details summary {{
    cursor: pointer; color: #0b4f6c; font-size: 13px; font-weight: 600;
    padding: 8px 2px;
  }}
  details.details summary:hover {{ text-decoration: underline; }}
  details.details .panel {{ margin-top: 4px; }}
  details.details .panel + .panel {{ margin-top: 10px; }}
  .row-night td {{ color: var(--muted); }}
  footer {{ color: var(--muted); font-size: 12px; margin-top: 30px; line-height: 1.7; }}
  @media (max-width: 1100px) {{
    .page {{
      grid-template-columns: 1fr;
      grid-template-areas: "hero" "side" "main";
    }}
    .sidebar {{ position: static; }}
    .legend-list {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  }}
  @media (max-width: 760px) {{
    .grid, .two {{ grid-template-columns: 1fr 1fr; }}
    header h1 {{ font-size: 23px; }}
  }}
  @media (max-width: 480px) {{
    .grid, .two {{ grid-template-columns: 1fr; }}
    .legend-list {{ grid-template-columns: 1fr; }}
    .chart-legend {{ flex-direction: column; gap: 2px; }}
  }}
</style>
</head>
<body>
<div class="page">
  <header>
    <h1>Kirra freedive outlook</h1>
    <div class="sub">Generated {report['generated_at'].strftime('%A %d %B %Y, %I:%M %p %Z')} &middot; rule-based local outlook</div>
    <div class="badge {alert_class}">{e(report['verdict'])}</div>
    <p class="headline">{e(report['headline'])}</p>
  </header>

  <main>
  {stale_html}

  <div class="grid">
    <div class="card">
      <div class="label">Setup</div>
      <div class="value status {antecedent['status']}">{antecedent['status_label']}</div>
      <div class="detail">{antecedent['fraction'] * 100:.0f}% gentle southerly</div>
    </div>
    <div class="card">
      <div class="label">Longest run</div>
      <div class="value">{antecedent['longest_run_hours']:.1f}h</div>
      <div class="detail">Needs {rule['antecedent_min_run_hours']:g}h+</div>
    </div>
    <div class="card">
      <div class="label">Window</div>
      <div class="value">{'Yes' if report['best_window'] else 'None'}</div>
      <div class="detail">{'Alertable' if report['best_window'] and report['best_window']['alertable'] else 'No alert'}</div>
    </div>
    <div class="card">
      <div class="label">Confidence</div>
      <div class="value">{report['confidence'].title()}</div>
      <div class="detail">Heuristic, not a guarantee</div>
    </div>
  </div>

  <h2>Past 72 hours at Coolangatta</h2>
  <div class="panel">
    <p>{latest_html}</p>
    <p>{e(antecedent['note'])}</p>
    <p class="muted">Window: {_fmt_time(antecedent['window_start'])} to {_fmt_time(antecedent['window_end'])} &middot;
    mean wind {_fmt_num(antecedent['mean_speed_kmh'], 1, ' km/h')} &middot;
    {antecedent['gentle_southerly_count']}/{antecedent['total_count']} observations were gentle southerly.</p>
  </div>

  <h2>Swell, wind &amp; tide — next 48 hours</h2>
  <div class="panel chart-panel">
    <div class="chart-legend">
      <span><span class="line-key wind"></span> Wind (km/h)</span>
      <span><span class="line-key swell"></span> Swell (m)</span>
      <span><span class="line-key tide"></span> Tide (m)</span>
      <span class="chart-hint">Arrows point downwind</span>
    </div>
    {combined_chart}
  </div>
  <details class="details">
    <summary>Show wind speed &amp; direction details</summary>
    <div class="panel">
      <table class="wind-table">
        <thead><tr><th>Time (AEST)</th><th>Wind speed</th><th>Direction (from)</th><th>Degrees</th></tr></thead>
        <tbody>{''.join(wind_rows)}</tbody>
      </table>
    </div>
  </details>
  <details class="details">
    <summary>Show tide times &amp; heights</summary>
    <div class="panel">
      <table class="tide-table">
        <thead><tr><th>Tide</th><th>Time (AEST)</th><th>Height</th></tr></thead>
        <tbody>{''.join(tide_rows)}</tbody>
      </table>
    </div>
  </details>

  <h2>Forecast — next 48 hours</h2>
  <div class="panel">
    <table>
      <thead><tr><th>Time (AEST)</th><th>Swell</th><th>Period</th><th>Rating</th><th>Why</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>

  <div class="two">
    <div>
      <h2>Best window</h2>
      <div class="callout">{best_html}</div>
    </div>
    <div>
      <h2>Next potential</h2>
      <div class="callout">{next_html}<p class="muted">Beyond 48 hours; context only.</p></div>
    </div>
  </div>

  <h2>How the call is made</h2>
  <div class="panel">
    <p><strong>Southerly:</strong> {rule['southerly_min_deg']:g}° to {rule['southerly_max_deg']:g}° &middot;
    <strong>Gentle:</strong> ≤ {rule['gentle_max_kmh']:g} km/h &middot;
    <strong>Setup:</strong> ≥ {rule['antecedent_min_fraction'] * 100:.0f}% of the last {rule['antecedent_window_hours']:g}h and a run of ≥ {rule['antecedent_min_run_hours']:g}h.</p>
    <p><strong>Reef:</strong> swell &lt; {rule['swell_reef_max_m']:g}m &middot;
    <strong>Good:</strong> swell &lt; {rule['swell_good_max_m']:g}m after a southerly setup &middot;
    <strong>No:</strong> swell ≥ {rule['swell_no_min_m']:g}m.</p>
    <p class="muted">Night blocks are shown but do not trigger alerts. Daylight is approximated as {rule['daylight_start_hour']:02d}:00–{rule['daylight_end_hour']:02d}:00 AEST.</p>
  </div>

  <footer>
    <div>Wind history: {e(report['sources'].get('bom_attribution', 'Bureau of Meteorology'))}</div>
    <div>Forecast: {e(report['sources'].get('forecast_url', 'surf-forecast.com'))}</div>
    <div>Swell: NOAA WaveWatch III via surf-forecast.com</div>
    <div>Tide times/heights: surf-forecast.com</div>
    <div>This is a rule-based outlook built from local freediver experience. It is guidance, not a guarantee. Check the water and your own limits.</div>
  </footer>
  </main>

  {sidebar_html}
</div>
</body>
</html>
"""
