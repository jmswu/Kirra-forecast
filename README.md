# Kirra forecast scraper

Scrapes date, wind, and swell data from the
[surf-forecast.com Kirra page](https://www.surf-forecast.com/breaks/Kirra/forecasts/latest).
No third-party packages required (Python 3.9+).

## Usage

```bash
# 7-day forecast, CSV to stdout
python3 kirra_forecast.py

# Save to a file
python3 kirra_forecast.py -o kirra_forecast.csv

# 48-hour detailed forecast (every 3 hours)
python3 kirra_forecast.py --hourly

# JSON instead of CSV
python3 kirra_forecast.py --json

# Another break on the same site (set the local timezone too)
python3 kirra_forecast.py \
  --url https://www.surf-forecast.com/breaks/Snapper-Rocks/forecasts/latest/six_day \
  --tz Australia/Brisbane
```

Default range is the 7-day table with three forecasts per day (9am, 3pm, 9pm
local time). `--hourly` uses the 48-hour table, which has a forecast every
3 hours.

## Output columns

| Column | Meaning |
| --- | --- |
| `datetime_local` | Forecast time with its UTC offset |
| `date`, `time` | Local date and 24-hour time |
| `wind_speed_kmh` | Wind speed in km/h |
| `wind_direction` | 16-point compass direction the wind is coming **from** |
| `wind_direction_deg` | Direction in degrees clockwise from true north |
| `swell_height_m` | Combined open-water swell height in metres |
| `swell_direction` | 16-point compass direction the swell is coming **from** |
| `swell_direction_deg` | Direction in degrees clockwise from true north |
| `swell_period_s` | Swell period in seconds |

The site normally serves metric units; the scraper converts mph/ft to km/h/m if
the page is served in imperial units.

## Notes

- Forecast times are the site's local time. Kirra is `Australia/Brisbane`
  (AEST, UTC+10, no daylight saving), which is the default.
- The swell row on this page is surf-forecast.com's combined open-water swell
  (NOAA WaveWatch III), not the height of waves breaking at the beach.
- The page layout is parsed by its `data-row` attributes and table structure.
  If surf-forecast.com changes its markup, the script exits with a clear error
  rather than writing bad data.
- Be considerate when automating: cache the CSV and run it a few times per day,
  not in a tight loop.
