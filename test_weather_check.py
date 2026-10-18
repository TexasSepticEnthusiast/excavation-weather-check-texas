"""Deterministic offline tests: forecast intervals, missing data, API parsing, and CLI."""
import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import weather_check as weather

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def make_fcst(hour=0, short="Sunny", temp=60.0, *, offset=timezone.utc):
    start = (NOW + timedelta(hours=hour)).astimezone(offset)
    end = (NOW + timedelta(hours=hour + 1)).astimezone(offset)
    return weather.HourlyForecast(time=start.isoformat(), end_time=end.isoformat(),
                                  temp_f=temp, wind_mph=5.0, precip_prob=0,
                                  short_forecast=short)


def complete_forecast():
    return [make_fcst(hour) for hour in range(48)]


class TestDecide(unittest.TestCase):
    def decide(self, forecasts, now=NOW):
        return weather.decide(forecasts, as_of=now)

    def test_complete_clear_forecast_has_no_configured_flags(self):
        rec, reasons = self.decide(complete_forecast())
        self.assertEqual(rec, "NO_FLAGS")
        self.assertIn("other hazards are not assessed", reasons[0])

    def test_hazard_terms_are_case_insensitive(self):
        for short in ("heavy rain", "Showers And THUNDERSTORMS", "Snow", "Freezing Fog",
                      "Frost", "Wintry Mix", "Sleet", "Ice"):
            with self.subTest(short=short):
                self.assertEqual(self.decide([make_fcst(short=short)])[0], "HOLD")

    def test_temperature_boundary(self):
        for temp, expected in ((32, "HOLD"), (31.9, "HOLD"), (32.1, "NO_FLAGS")):
            with self.subTest(temp=temp):
                forecasts = complete_forecast()
                forecasts[0].temp_f = temp
                self.assertEqual(self.decide(forecasts)[0], expected)

    def test_outside_48h_window(self):
        forecasts = complete_forecast() + [make_fcst(72, "Heavy Rain")]
        self.assertEqual(self.decide(forecasts)[0], "NO_FLAGS")

    def test_end_boundary_is_exclusive(self):
        forecasts = complete_forecast() + [make_fcst(48, "Thunderstorms")]
        self.assertEqual(self.decide(forecasts)[0], "NO_FLAGS")
        forecasts[47].short_forecast = "Thunderstorms"
        self.assertEqual(self.decide(forecasts)[0], "HOLD")

    def test_past_period_ending_now_is_excluded(self):
        forecasts = [make_fcst(-1, "Heavy Rain")] + complete_forecast()
        self.assertEqual(self.decide(forecasts)[0], "NO_FLAGS")

    def test_current_partial_hour_is_included(self):
        forecasts = complete_forecast() + [make_fcst(48)]
        forecasts[0].short_forecast = "Thunderstorms"
        self.assertEqual(self.decide(forecasts, NOW + timedelta(minutes=30))[0], "HOLD")
        forecasts[0].short_forecast = "Sunny"
        self.assertEqual(self.decide(forecasts, NOW + timedelta(minutes=30))[0], "NO_FLAGS")
        self.assertEqual(self.decide(forecasts[:-1], NOW + timedelta(minutes=30))[0], "UNKNOWN")

    def test_timezone_offsets_and_unordered_periods(self):
        local = timezone(timedelta(hours=-5))
        forecasts = [make_fcst(hour, offset=local) for hour in reversed(range(48))]
        self.assertEqual(self.decide(forecasts)[0], "NO_FLAGS")

    def test_empty_stale_future_and_truncated_data_are_unknown(self):
        examples = ([], [make_fcst(-1)], [make_fcst(48)], complete_forecast()[:24],
                    [make_fcst(hour) for hour in range(1, 49)])
        for forecasts in examples:
            with self.subTest(count=len(forecasts)):
                rec, reasons = self.decide(forecasts)
                self.assertEqual(rec, "UNKNOWN")
                self.assertTrue(any("coverage" in reason for reason in reasons))

    def test_gap_is_not_filled_by_duplicate_rows(self):
        forecasts = complete_forecast()
        forecasts[20] = make_fcst(19)
        self.assertEqual(len(forecasts), 48)
        self.assertEqual(self.decide(forecasts)[0], "UNKNOWN")

    def test_sparse_outside_window_fixture_is_unknown_not_no_flags(self):
        self.assertEqual(self.decide([make_fcst(), make_fcst(72, "Heavy Rain")])[0], "UNKNOWN")

    def test_invalid_hourly_timestamps_are_unknown(self):
        for start, end in (("", ""), (NOW.isoformat(), None),
                           ("2026-10-17T12:00:00", "2026-10-17T13:00:00"),
                           (NOW.isoformat(), NOW.isoformat()),
                           (NOW.isoformat(), (NOW + timedelta(hours=48)).isoformat())):
            with self.subTest(start=start, end=end):
                forecasts = complete_forecast()
                forecasts[0].time, forecasts[0].end_time = start, end
                self.assertEqual(self.decide(forecasts)[0], "UNKNOWN")

    def test_missing_or_invalid_temperatures_are_unknown(self):
        for temp in (None, float("nan"), float("inf"), "60", False):
            with self.subTest(temp=temp):
                forecasts = complete_forecast()
                forecasts[0].temp_f = temp
                self.assertEqual(self.decide(forecasts)[0], "UNKNOWN")

    def test_missing_description_is_unknown(self):
        for description in (None, "", "   "):
            with self.subTest(description=description):
                forecasts = complete_forecast()
                forecasts[0].short_forecast = description
                self.assertEqual(self.decide(forecasts)[0], "UNKNOWN")

    def test_known_hazard_and_missing_data_are_both_reported(self):
        rec, reasons = self.decide([make_fcst(short="Thunderstorms", temp=None)])
        self.assertEqual(rec, "HOLD")
        self.assertTrue(any("thunderstorms" in reason for reason in reasons))
        self.assertTrue(any("temperature" in reason for reason in reasons))
        self.assertTrue(any("coverage" in reason for reason in reasons))

    def test_naive_reference_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.decide([], NOW.replace(tzinfo=None))


class TestAPIParsing(unittest.TestCase):
    @staticmethod
    def period(hour):
        forecast = make_fcst(hour)
        return {"startTime": forecast.time, "endTime": forecast.end_time,
                "temperature": 60, "temperatureUnit": "F", "windSpeed": "5 to 10 mph",
                "probabilityOfPrecipitation": {"value": None}, "shortForecast": "Sunny"}

    def test_requested_hours_are_elapsed_time_not_row_count(self):
        periods = [self.period(hour) for hour in reversed(range(-2, 74))]
        with patch.object(weather, "http_get", return_value={"properties": {"periods": periods}}):
            forecasts = weather.get_hourly("FWD", 1, 2, 48, as_of=NOW + timedelta(minutes=30))
        self.assertEqual(len(forecasts), 49)
        self.assertEqual(weather.decide(forecasts, as_of=NOW + timedelta(minutes=30))[0], "NO_FLAGS")
        self.assertTrue(all(f.wind_mph == 10.0 for f in forecasts))
        self.assertTrue(all(f.precip_prob is None for f in forecasts))

    def test_temperature_units_and_null_precipitation(self):
        periods = [self.period(hour) for hour in range(3)]
        periods[0].update(temperature=0, temperatureUnit="C", probabilityOfPrecipitation=None)
        periods[1].update(temperature=60, temperatureUnit="unknown")
        periods[2].update(temperature=None)
        with patch.object(weather, "http_get", return_value={"properties": {"periods": periods}}):
            forecasts = weather.get_hourly("FWD", 1, 2, as_of=NOW)
        self.assertEqual(forecasts[0].temp_f, 32)
        self.assertIsNone(forecasts[0].precip_prob)
        self.assertIsNone(forecasts[1].temp_f)
        self.assertIsNone(forecasts[2].temp_f)
        self.assertEqual(weather.decide(forecasts, as_of=NOW)[0], "HOLD")

    def test_missing_end_time_reaches_unknown_screen(self):
        period = self.period(0)
        del period["endTime"]
        with patch.object(weather, "http_get", return_value={"properties": {"periods": [period]}}):
            forecasts = weather.get_hourly("FWD", 1, 2, as_of=NOW)
        self.assertEqual(weather.decide(forecasts, as_of=NOW)[0], "UNKNOWN")

    def test_grid_id_and_multiword_city(self):
        payload = {"properties": {"gridId": "FWD", "gridX": 1, "gridY": 2,
                   "relativeLocation": {"properties": {"city": "Mineral Wells", "state": "TX"}}}}
        with patch.object(weather, "http_get", return_value=payload):
            self.assertEqual(weather.get_gridpoint(32.78, -98.32), ("FWD", 1, 2, "Mineral Wells, TX"))


class TestRunAndCLI(unittest.TestCase):
    def result(self, status):
        return weather.CheckResult("Lone Camp, TX", 32.78, -98.32, NOW.isoformat(),
                                   complete_forecast(), status, ["Test fixture"])

    def test_run_uses_same_clock_for_fetch_and_screen(self):
        with patch.object(weather, "datetime", wraps=datetime) as clock, \
             patch.object(weather, "get_gridpoint", return_value=("FWD", 1, 2, "Lone Camp, TX")), \
             patch.object(weather, "get_hourly", return_value=complete_forecast()) as hourly:
            clock.now.return_value = NOW
            result = weather.run(32.78, -98.32, "Lone Camp, TX")
        self.assertEqual(result.recommendation, "NO_FLAGS")
        self.assertEqual(result.checked_at, NOW.isoformat())
        hourly.assert_called_once_with("FWD", 1, 2, hours=72, as_of=NOW)

    def test_input_validation_precedes_network(self):
        cases = ((float("nan"), 0, 72), (91, 0, 72), (0, float("inf"), 72),
                 (0, -181, 72), (0, 0, -1), (0, 0, 47), (0, 0, 169), (0, 0, True))
        with patch.object(weather, "get_gridpoint") as lookup:
            for lat, lon, hours in cases:
                with self.subTest(lat=lat, lon=lon, hours=hours), self.assertRaises(ValueError):
                    weather.run(lat, lon, "Test", hours)
        lookup.assert_not_called()

    def test_cli_statuses_and_scope_in_json(self):
        for status, code in (("NO_FLAGS", 0), ("HOLD", 1), ("UNKNOWN", 2)):
            with self.subTest(status=status), patch.object(weather, "run", return_value=self.result(status)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(weather.main(["--json"]), code)
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["recommendation"], status)
                self.assertIn("does not authorize excavation", payload["scope"])

    def test_cli_network_error_is_exit_two(self):
        with patch.object(weather, "run", side_effect=OSError("offline")), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(weather.main([]), 2)
        self.assertIn("offline", err.getvalue())

    def test_text_output_shows_offsets_and_scope(self):
        result = self.result("NO_FLAGS")
        text = weather.render_text(result)
        self.assertIn("does not authorize excavation", text)
        self.assertIn(NOW.isoformat(), text)
        self.assertIn("Weather screen: NO_FLAGS", text)
        self.assertNotIn(make_fcst(24).time + " ", text)


if __name__ == "__main__":
    unittest.main()
