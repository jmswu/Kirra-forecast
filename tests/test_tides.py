import unittest
from zoneinfo import ZoneInfo

from kirra_forecast import Node, parse_tides

TZ = ZoneInfo("Australia/Brisbane")


def tide_cell(clock, height, units="m"):
    container = Node("div", {"class": "tide-time"})
    time_node = Node("span", {"class": "tide-time__time"})
    time_node.children.append(clock)
    height_node = Node("span", {"class": "tide-time__height"})
    height_node.children.append(str(height))
    units_node = Node("span", {"class": "tide-time__units"})
    units_node.children.append(units)
    container.children.extend([time_node, height_node, units_node])
    cell = Node("td")
    cell.children.append(container)
    return cell


def tide_row(cells):
    row = Node("tr")
    row.children.extend(cells)
    return row


class TideParserTests(unittest.TestCase):
    def test_overnight_event_rolls_to_next_day(self):
        rows = {
            "tide-high": tide_row([tide_cell("1:36AM", 1.03)]),
            "tide-low": tide_row([]),
        }
        tides = parse_tides(rows, ["2026-11-16"], [21], TZ, 1.0)
        self.assertEqual(len(tides), 1)
        self.assertEqual(tides[0]["time"].isoformat(), "2026-11-17T01:36:00+10:00")
        self.assertEqual(tides[0]["type"], "high")
        self.assertEqual(tides[0]["height_m"], 1.03)

    def test_afternoon_event_stays_on_column_day(self):
        rows = {
            "tide-high": tide_row([]),
            "tide-low": tide_row([tide_cell(" 7:52PM", 0.62)]),
        }
        tides = parse_tides(rows, ["2026-11-16"], [19], TZ, 1.0)
        self.assertEqual(len(tides), 1)
        self.assertEqual(tides[0]["time"].isoformat(), "2026-11-16T19:52:00+10:00")
        self.assertEqual(tides[0]["type"], "low")

    def test_feet_are_converted_to_metres(self):
        rows = {
            "tide-high": tide_row([tide_cell("12:33PM", 5.0, "ft")]),
            "tide-low": tide_row([]),
        }
        tides = parse_tides(rows, ["2026-11-16"], [13], TZ, 1.0)
        self.assertEqual(tides[0]["height_m"], 1.52)


if __name__ == "__main__":
    unittest.main()
