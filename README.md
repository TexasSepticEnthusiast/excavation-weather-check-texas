# Out West Septic weather check

A small Python CLI for Out West Septic excavation planning workflows. It screens an NWS hourly forecast for a limited set of weather flags over the next 48 hours. The default location is near Lone Camp, Texas; use coordinates for the actual job site.

**No result authorizes excavation or establishes that a site is safe to dig.** This tool does not assess utility locations, excavation stability, soil saturation, permits, or official weather alerts. Out West Septic project branding does not imply NWS endorsement.

## Run

Python 3.10 or newer; standard library only.

```sh
python weather_check.py
python weather_check.py --lat 32.75 --lon -98.40
python weather_check.py --json
python weather_check.py --hours 72
```

Coordinates must be finite and within latitude/longitude ranges. `--hours` accepts 48 through 168 and controls the retrieved/displayable horizon; screening always covers 48 elapsed hours from `checked_at`.

## Python API

`OutWestSepticHourlyForecast` and `OutWestSepticWeatherCheckResult` are the public data classes. `HourlyForecast` and `CheckResult` remain exact aliases for existing callers.

```python
from weather_check import OutWestSepticWeatherCheckResult, run

result: OutWestSepticWeatherCheckResult = run(32.78, -98.32, "Lone Camp, TX")
print(result.recommendation)
print(result.scope)
```

## Results and compatibility

| `recommendation` | Meaning | Exit code |
| --- | --- | --- |
| `NO_FLAGS` | Complete usable hourly coverage, with none of this tool's configured flags found. Other hazards remain unassessed. | 0 |
| `HOLD` | At least one configured weather flag was found. Missing data is also reported when present. | 1 |
| `UNKNOWN` | No configured flag was found, but missing or invalid data prevents a complete screen. | 2 |
| No result | Invalid arguments, API failure, or unreadable response; see stderr. | 2 |

`NO_FLAGS` replaces the former `DIG` value. Update scripts that consume `recommendation`; exit code zero means the limited screen completed without configured flags, not permission to start work. Text and JSON output both include the tool's scope. The JSON array retains its existing `next_3_days` name for compatibility, even when `--hours` changes its horizon; each period now includes `end_time`.

## Screening rules

The window includes the check time and excludes exactly 48 hours later. Timezone-aware `startTime`/`endTime` intervals select the forecast, including the remainder of the current hour. Sorting or duplicating rows cannot substitute for continuous coverage.

The project-defined flags are case-insensitive mentions of heavy rain, thunderstorms, freezing, frost, snow, wintry mix, sleet, or ice, plus temperature at or below 32 F. These are screening heuristics, not NWS excavation criteria. Ordinary rain descriptions may not trigger a flag. Precipitation probability is displayed but is not a rainfall amount and does not determine the result; wind is not screened.

A complete result requires continuous hourly coverage and a usable temperature and weather description for every included period. Empty, stale, truncated, gapped, or malformed data produces `UNKNOWN` unless a known flag already requires `HOLD`. Missing temperatures are not treated as warm weather. Celsius values are converted to Fahrenheit; unsupported units remain unknown. Timestamps without offsets are rejected instead of assuming a local timezone.

## Data source and limits

[NWS API documentation](https://www.weather.gov/documentation/services-web-api) describes the coordinate lookup, hourly forecast endpoint, ISO 8601 timestamps, and required identifying User-Agent. NWS provides the API without a fee and applies rate limits. This project's User-Agent identifies OutWestSeptic-WeatherCheck and links to this public repository; it does not supply an unverified contact email. Reviewed October 18, 2026.

The tool requests `/points/{latitude},{longitude}` and then the corresponding hourly grid forecast. It does not retrieve observed rainfall, ground conditions, warnings, or watches. Forecast coverage is checked by valid time, not by issuance freshness. Use current official forecasts and alerts and appropriate site assessment alongside this limited screen.

## Tests

```sh
python -m unittest -v
```

Tests use fixed timestamps and mocked API responses, with no network calls. They cover elapsed-time boundaries, timezone offsets, current partial hours, missing data, temperature units, input validation, and CLI result/exit-code behavior.

## License

MIT. See `LICENSE`.
