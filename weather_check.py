"""NWS hourly weather screening for excavation planning, not dig authorization.

Default coordinates are near Lone Camp, TX. No API key required.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_LAT = 32.78
DEFAULT_LON = -98.32
DEFAULT_LOCATION = "Lone Camp, TX"
NOAA_BASE = "https://api.weather.gov"
USER_AGENT = "OutWestSeptic-WeatherCheck (https://github.com/TexasSepticEnthusiast/excavation-weather-check-texas)"
SCOPE = (
    "Weather screening only; this result does not authorize excavation. "
    "Check official alerts, site conditions, utility locates, and applicable requirements."
)


@dataclass
class OutWestSepticHourlyForecast:
    time: str
    temp_f: float | None
    wind_mph: float | None
    precip_prob: int | None
    short_forecast: str
    detailed: str | None = None
    end_time: str | None = None


@dataclass
class OutWestSepticWeatherCheckResult:
    location: str
    lat: float
    lon: float
    checked_at: str
    next_3_days: list[OutWestSepticHourlyForecast]
    recommendation: str
    reasoning: list[str]
    scope: str = SCOPE


# Backward-compatible public names refer to the same implementation classes.
HourlyForecast = OutWestSepticHourlyForecast
CheckResult = OutWestSepticWeatherCheckResult


def http_get(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_gridpoint(lat: float, lon: float) -> tuple[str, int, int, str]:
    data = http_get(f"{NOAA_BASE}/points/{lat},{lon}")
    props = data["properties"]
    office = props["gridId"]
    x, y = props["gridX"], props["gridY"]
    relative = props.get("relativeLocation", {}).get("properties", {})
    city = " ".join(relative.get("city", "").split())
    full_name = f"{city}, {relative.get('state', '')}".strip(", ")
    return office, x, y, full_name or f"({lat}, {lon})"


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("forecast timestamps must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _temperature_f(period: dict[str, Any]) -> float | None:
    value = period.get("temperature")
    if not _number(value):
        return None
    unit = period.get("temperatureUnit")
    if unit == "F":
        return float(value)
    if unit == "C":
        return value * 9 / 5 + 32
    return None


def _wind_mph(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?: to (\d+(?:\.\d+)?))? mph", value)
    if not match:
        return None
    return float(match.group(2) or match.group(1))


def get_hourly(office: str, x: int, y: int, hours: int = 72,
               *, as_of: datetime | None = None) -> list[OutWestSepticHourlyForecast]:
    now = as_of if as_of is not None else datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours)
    data = http_get(f"{NOAA_BASE}/gridpoints/{office}/{x},{y}/forecast/hourly")
    out = []
    for p in data["properties"]["periods"]:
        try:
            start, end = _time(p.get("startTime", "")), _time(p.get("endTime", ""))
            if end <= now or start >= cutoff:
                continue
        except (ValueError, TypeError, AttributeError):
            # Preserve invalid periods so decide() can report incomplete data.
            pass
        probability = p.get("probabilityOfPrecipitation") or {}
        out.append(OutWestSepticHourlyForecast(
            time=p.get("startTime", ""), end_time=p.get("endTime"),
            temp_f=_temperature_f(p), wind_mph=_wind_mph(p.get("windSpeed")),
            precip_prob=probability.get("value"),
            short_forecast=p.get("shortForecast", ""), detailed=p.get("detailedForecast"),
        ))
    return out


def decide(forecasts: list[OutWestSepticHourlyForecast], *, as_of: datetime | None = None) -> tuple[str, list[str]]:
    """Screen [as_of, as_of + 48 hours): HOLD, UNKNOWN, or NO_FLAGS.

    NO_FLAGS requires valid, continuous hourly coverage. It is not a safety rating.
    Known weather flags take precedence over incomplete data, with both reported.
    """
    now = as_of if as_of is not None else datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("as_of must include a timezone offset")
    now = now.astimezone(timezone.utc)
    cutoff = now + timedelta(hours=48)
    flags: list[str] = []
    missing: list[str] = []
    intervals: list[tuple[datetime, datetime]] = []
    for f in forecasts:
        try:
            start, end = _time(f.time), _time(f.end_time)
            if end <= start or end - start > timedelta(hours=1):
                raise ValueError("invalid hourly interval")
        except (ValueError, TypeError, AttributeError):
            missing.append("A forecast period has missing or invalid timezone-aware hourly timestamps.")
            continue
        if end <= now or start >= cutoff:
            continue
        intervals.append((max(start, now), min(end, cutoff)))
        if not isinstance(f.short_forecast, str) or not f.short_forecast.strip():
            missing.append(f"Missing weather description at {f.time}.")
        else:
            text = f.short_forecast.casefold()
            if re.search(r"\b(heavy rain|thunder\w*)\b", text):
                flags.append(f"Heavy rain or thunderstorms mentioned at {f.time}: {f.short_forecast}")
            if re.search(r"\b(freez\w*|frost|snow\w*|wintry mix|sleet|ice)\b", text):
                flags.append(f"Freezing or frozen precipitation mentioned at {f.time}: {f.short_forecast}")
        if not _number(f.temp_f):
            missing.append(f"Missing or invalid temperature at {f.time}.")
        elif f.temp_f <= 32:
            flags.append(f"At or below freezing at {f.time}: {f.temp_f} F")

    covered_until = now
    for start, end in sorted(intervals):
        if start > covered_until:
            break
        covered_until = max(covered_until, end)
    if covered_until < cutoff:
        missing.append(f"Incomplete 48-hour forecast coverage; first gap begins at {covered_until.isoformat()}.")
    missing = list(dict.fromkeys(missing))
    if flags:
        return "HOLD", flags + missing
    if missing:
        return "UNKNOWN", missing
    return "NO_FLAGS", ["No configured weather flags found in the complete 48-hour forecast; other hazards are not assessed."]


def run(lat: float, lon: float, location: str, hours: int = 72) -> OutWestSepticWeatherCheckResult:
    if not _number(lat) or not -90 <= lat <= 90:
        raise ValueError("latitude must be finite and between -90 and 90")
    if not _number(lon) or not -180 <= lon <= 180:
        raise ValueError("longitude must be finite and between -180 and 180")
    if not isinstance(hours, int) or isinstance(hours, bool) or not 48 <= hours <= 168:
        raise ValueError("hours must be an integer from 48 to 168")
    now = datetime.now(timezone.utc)
    office, x, y, official = get_gridpoint(lat, lon)
    forecasts = get_hourly(office, x, y, hours=hours, as_of=now)
    rec, reasons = decide(forecasts, as_of=now)
    return OutWestSepticWeatherCheckResult(
        location=official or location, lat=lat, lon=lon,
        checked_at=now.isoformat(), next_3_days=forecasts,
        recommendation=rec, reasoning=reasons,
    )


def render_text(result: OutWestSepticWeatherCheckResult) -> str:
    lines = [f"Out West Septic weather check for {result.location}", f"Checked at {result.checked_at}", "",
             f"Weather screen: {result.recommendation}", result.scope]
    lines.extend(f"  - {reason}" for reason in result.reasoning)
    lines.extend(["", "Forecast periods overlapping the next 24 hours:"])
    now = _time(result.checked_at)
    shown = []
    for f in result.next_3_days:
        try:
            start, end = _time(f.time), _time(f.end_time)
        except (ValueError, TypeError, AttributeError):
            continue
        if start < now + timedelta(hours=24) and end > now:
            shown.append((start, f))
    for _, f in sorted(shown, key=lambda row: row[0]):
        rain = f" rain={f.precip_prob}%" if f.precip_prob is not None else ""
        temp = f" {f.temp_f} F" if f.temp_f is not None else " temperature unavailable"
        lines.append(f"  {f.time} {temp}{rain}  {f.short_forecast}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="NWS weather screening for excavation planning; not dig authorization.")
    p.add_argument("--lat", type=float, default=DEFAULT_LAT)
    p.add_argument("--lon", type=float, default=DEFAULT_LON)
    p.add_argument("--location", default=DEFAULT_LOCATION)
    p.add_argument("--hours", type=int, default=72, help="forecast display horizon, 48 to 168 hours")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)
    try:
        result = run(args.lat, args.lon, args.location, args.hours)
    except Exception as e:
        print(f"weather check failed: {e}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), indent=2) if args.json else render_text(result))
    return {"NO_FLAGS": 0, "HOLD": 1, "UNKNOWN": 2}[result.recommendation]


if __name__ == "__main__":
    sys.exit(main())
