import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from kirra_outlook import (
    build_outlook,
    build_report,
    compass_to_degrees,
    evaluate_block,
    render_html,
    render_markdown,
    render_tide_chart,
    render_wind_chart,
    summarize_antecedent,
)

TZ = ZoneInfo("Australia/Brisbane")
RULES = {
    "southerly_min_deg": 135,
    "southerly_max_deg": 225,
    "gentle_max_kmh": 20,
    "strong_wind_kmh": 25,
    "antecedent_window_hours": 72,
    "antecedent_min_fraction": 0.5,
    "antecedent_min_run_hours": 24,
    "antecedent_partial_fraction": 0.3,
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
}


def observation_series(now, direction, speed, hours):
    observations = []
    start = now - timedelta(hours=hours)
    count = int(hours * 2)
    for index in range(count + 1):
        moment = start + timedelta(minutes=30 * index)
        observations.append({
            "time": moment,
            "speed_kmh": speed,
            "gust_kmh": speed + 5,
            "direction": direction,
            "direction_deg": compass_to_degrees(direction),
        })
    return observations


def block(hour, swell, wind_speed=10, wind_direction="S", period=9):
    moment = datetime(2026, 11, 17, hour, 0, tzinfo=TZ)
    return {
        "time": moment,
        "wind_speed_kmh": wind_speed,
        "wind_direction": wind_direction,
        "wind_direction_deg": compass_to_degrees(wind_direction),
        "swell_height_m": swell,
        "swell_direction": "E",
        "swell_direction_deg": 90,
        "swell_period_s": period,
    }


class CompassTests(unittest.TestCase):
    def test_compass_points(self):
        self.assertEqual(compass_to_degrees("N"), 0)
        self.assertEqual(compass_to_degrees("ESE"), 112.5)
        self.assertEqual(compass_to_degrees("SSW"), 202.5)
        self.assertIsNone(compass_to_degrees("CALM"))


class AntecedentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 17, 12, 0, tzinfo=TZ)

    def test_sustained_southerly_passes(self):
        observations = observation_series(self.now, "SSW", 10, 72)
        result = summarize_antecedent(observations, RULES, end=self.now)
        self.assertEqual(result["status"], "pass")
        self.assertGreater(result["fraction"], 0.9)
        self.assertGreater(result["longest_run_hours"], 60)

    def test_northerly_fails(self):
        observations = observation_series(self.now, "N", 10, 72)
        result = summarize_antecedent(observations, RULES, end=self.now)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["fraction"], 0)

    def test_one_day_run_passes(self):
        observations = (
            observation_series(self.now - timedelta(hours=48), "N", 15, 48)
            + observation_series(self.now, "S", 10, 24)
        )
        result = summarize_antecedent(observations, RULES, end=self.now)
        self.assertEqual(result["status"], "pass")
        self.assertGreaterEqual(result["longest_run_hours"], 24)

    def test_short_run_is_partial(self):
        observations = (
            observation_series(self.now - timedelta(hours=60), "N", 15, 60)
            + observation_series(self.now, "S", 10, 12)
        )
        result = summarize_antecedent(observations, RULES, end=self.now)
        self.assertEqual(result["status"], "partial")


class RatingTests(unittest.TestCase):
    def setUp(self):
        self.pass_antecedent = {"status": "pass"}
        self.fail_antecedent = {"status": "fail"}

    def test_reef_daylight(self):
        result = evaluate_block(block(10, 0.4), self.pass_antecedent, RULES)
        self.assertEqual(result["rating"], "REEF")
        self.assertTrue(result["alertable"])

    def test_reef_at_night_is_not_alertable(self):
        result = evaluate_block(block(22, 0.4), self.pass_antecedent, RULES)
        self.assertEqual(result["rating"], "REEF")
        self.assertFalse(result["alertable"])

    def test_good_requires_setup_and_wind(self):
        good = evaluate_block(block(10, 0.8), self.pass_antecedent, RULES)
        self.assertEqual(good["rating"], "GOOD")
        bad_setup = evaluate_block(block(10, 0.8), self.fail_antecedent, RULES)
        self.assertEqual(bad_setup["rating"], "WATCH")
        wrong_wind = evaluate_block(
            block(10, 0.8, wind_direction="NE"),
            self.pass_antecedent,
            RULES,
        )
        self.assertEqual(wrong_wind["rating"], "WATCH")

    def test_large_swell_is_no(self):
        result = evaluate_block(block(10, 2.0), self.pass_antecedent, RULES)
        self.assertEqual(result["rating"], "NO")

    def test_strong_wind_downgrades_reef(self):
        result = evaluate_block(
            block(10, 0.4, wind_speed=30, wind_direction="S"),
            self.pass_antecedent,
            RULES,
        )
        self.assertEqual(result["rating"], "WATCH")


class OutlookTests(unittest.TestCase):
    def test_best_window_is_contiguous(self):
        now = datetime(2026, 11, 17, 9, 0, tzinfo=TZ)
        blocks = [
            block(7, 2.0),
            block(10, 0.8),
            block(13, 0.8),
            block(16, 0.8),
            block(19, 2.0),
        ]
        outlook = build_outlook(blocks, {"status": "pass"}, RULES, now)
        self.assertEqual(outlook["verdict"], "GOOD WINDOW")
        window = outlook["best_window"]
        self.assertEqual(window["start"].hour, 10)
        self.assertEqual(window["end"].hour, 19)
        self.assertEqual(window["rating"], "GOOD")

    def test_reef_window_wins(self):
        now = datetime(2026, 11, 17, 9, 0, tzinfo=TZ)
        blocks = [block(10, 0.4), block(13, 0.4)]
        outlook = build_outlook(blocks, {"status": "pass"}, RULES, now)
        self.assertEqual(outlook["verdict"], "REEF WINDOW")
        self.assertIsNotNone(outlook["alert"])

    def test_no_window_with_large_swell(self):
        now = datetime(2026, 11, 17, 9, 0, tzinfo=TZ)
        blocks = [block(10, 2.0), block(13, 2.2)]
        outlook = build_outlook(blocks, {"status": "pass"}, RULES, now)
        self.assertEqual(outlook["verdict"], "NO WINDOW")
        self.assertIsNone(outlook["alert"])


class ReportTests(unittest.TestCase):
    def test_tides_are_filtered_and_included(self):
        now = datetime(2026, 11, 17, 9, 0, tzinfo=TZ)
        observations = observation_series(now, "S", 10, 72)
        tides = [
            {"time": now - timedelta(hours=2), "type": "high", "height_m": 1.5},
            {"time": now + timedelta(hours=4), "type": "low", "height_m": 0.5},
            {"time": now + timedelta(hours=50), "type": "high", "height_m": 1.6},
        ]
        report = build_report(
            [block(10, 0.8)],
            observations,
            now,
            RULES,
            {"location": "Kirra"},
            tides=tides,
        )
        self.assertEqual(len(report["tides"]), 1)
        self.assertEqual(report["next_tide"]["type"], "low")
        markdown = render_markdown(report)
        html = render_html(report)
        self.assertIn("Tides (next 48 hours)", markdown)
        self.assertIn("Wind — next 48 hours", html)
        self.assertIn("Tide — next 48 hours", html)
        self.assertIn("<svg class=\"chart\"", html)
        self.assertIn("Show wind speed &amp; direction details", html)
        self.assertIn("Show tide chart &amp; times", html)
        self.assertIn("Rating guide", html)
        self.assertIn("No actionable window yet", html)
        self.assertIn("0.50m", markdown)
        wind_chart = render_wind_chart(report)
        tide_chart = render_tide_chart(report)
        self.assertIn("wind-arrow", wind_chart)
        self.assertIn("tide-marker", tide_chart)
        self.assertIn("L 0.50m", tide_chart)


if __name__ == "__main__":
    unittest.main()
