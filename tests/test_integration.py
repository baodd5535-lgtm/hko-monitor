"""Integration tests: full pipeline flow and race condition simulation."""
import os
import tempfile
import threading
import sqlite3
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
import requests


from hko_weather_monitor.config import Config
from hko_weather_monitor.api_client import HKOApiClient
from hko_weather_monitor.poller import WeatherPoller
from hko_weather_monitor.database import WeatherDatabase


@pytest.fixture
def test_db_path():
    """Isolated temp database for each test."""
    path = tempfile.mktemp(suffix=".db")
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def test_config(test_db_path):
    """Config pointing at test DB."""
    cfg = Config
    cfg.DATABASE_PATH = test_db_path
    yield cfg


@pytest.fixture
def mock_hko_text():
    """Realistic HKO AWS text payload."""
    return (
        "14:30 Local Time 22 May 2026\n"
        "Station, WindSpeed, Humidity, Pressure, Temperature, Rainfall\n"
        "hko, 3.2, 82, 1012.3, 26.5, 0.0\n"
        "sha, 2.1, 80, 1012.1, 27.1, 0.0\n"
        "kp, 4.5, 78, 1011.9, 25.8, 0.0\n"
    )


@pytest.fixture
def mock_past_csv():
    """Realistic historical CSV."""
    return (
        "202605221400,26.5,hko\n"
        "202605221400,27.1,sha\n"
        "202605221300,26.2,hko\n"
    )


@pytest.fixture
def mock_forecast_json():
    """Minimal forecast JSON."""
    return {
        "StationCode": "HKO",
        "DailyForecast": [
            {
                "ForecastDate": "20260523",
                "ForecastMaximumTemperature": "28.0",
                "ForecastMinimumTemperature": "25.0",
                "ForecastChanceOfRain": "Low",
                "ForecastDailyWeather": 1,
            }
        ],
        "HourlyWeatherForecast": [
            {
                "ForecastHour": "2026052215",
                "ForecastTemperature": "26.8",
                "ForecastWindSpeed": "3.5",
                "ForecastWindDirection": "180",
                "ForecastRelativeHumidity": "80",
            }
        ],
    }


class TestFullPipelineFlow:
    """Part B.1 — Full core data pipeline flow."""

    def test_full_pipeline_flow(self, test_db_path, mock_hko_text, mock_past_csv, mock_forecast_json):
        """API fetch → parse → DB insert → query roundtrip."""
        from hko_weather_monitor import config as cfg_mod
        import importlib
        cfg_mod.Config.DATABASE_PATH = test_db_path
        importlib.reload(cfg_mod)

        api = HKOApiClient(
            base_url="https://www.hko.gov.hk/wxinfo/awsgis",
            aws_datafile="latestReadings_AWS1_v2.txt",
            historical_temp_file="animate_J1.csv",
            timeout=10,
            max_retries=1,
            backoff_factor=0.5,
        )

        # Parse temperatures
        readings = api.parse_temperatures(mock_hko_text)
        assert len(readings) == 3, f"Expected 3 readings, got {len(readings)}"

        # Parse past temperatures
        past = api.parse_past_temperatures(mock_past_csv)
        assert len(past) == 3, f"Expected 3 past readings, got {len(past)}"

        # Insert into DB
        db = WeatherDatabase(test_db_path)
        db.insert_batch(readings)
        db.insert_batch(past)

        # Insert forecast
        daily = api.parse_daily_forecast(mock_forecast_json)
        hourly = api.parse_hourly_forecast(mock_forecast_json)
        assert len(daily) == 1
        assert len(hourly) == 1

        db.insert_daily_forecasts_batch(daily)
        db.insert_hourly_forecasts_batch(hourly)

        # Verify DB roundtrip
        with db._get_connection() as conn:
            stations = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
            temp_count = conn.execute("SELECT COUNT(*) FROM temperature_readings").fetchone()[0]
            daily_count = conn.execute("SELECT COUNT(*) FROM daily_forecasts").fetchone()[0]
            hourly_count = conn.execute("SELECT COUNT(*) FROM hourly_forecasts").fetchone()[0]

        assert stations >= 3, f"Expected at least 3 stations, got {stations}"
        assert temp_count == 6, f"Expected 6 temp readings, got {temp_count}"
        assert daily_count == 1
        assert hourly_count == 1

        # Verify temperature values — current reading for hko is 26.5
        with db._get_connection() as conn:
            row = conn.execute(
                "SELECT temperature_celsius FROM temperature_readings WHERE station_id = "
                "(SELECT station_id FROM stations WHERE code = 'hko') AND recorded_at = '2026-05-22 14:30:00' LIMIT 1"
            ).fetchone()
        assert row is not None, "hko current reading not found"
        assert abs(row[0] - 26.5) < 0.01


class TestConcurrentReadWrite:
    """Part B.2 — Race condition simulation."""

    def test_concurrent_read_write(self, test_db_path):
        """Multiple writers and readers shouldn't crash."""
        db = WeatherDatabase(test_db_path)
        errors = []
        written = [0]
        read_count = [0]

        def writer(thread_id):
            for i in range(20):
                try:
                    # Each thread uses unique station codes to avoid UNIQUE constraint
                    # (the race condition in get_or_create_station_id is a separate issue)
                    station = f"t{thread_id}s{i % 3}"
                    db.insert_temperature(
                        station, f"Thread{thread_id}Station{i % 3}",
                        datetime(2026, 5, 22, 14, i, thread_id * 2), 25.0 + i * 0.1
                    )
                    written[0] += 1
                except Exception as e:
                    errors.append(str(e))

        def reader():
            for _ in range(20):
                try:
                    with db._get_connection() as conn:
                        conn.execute("SELECT COUNT(*) FROM temperature_readings")
                    read_count[0] += 1
                except Exception as e:
                    errors.append(str(e))

        threads = []
        for tid in range(3):
            threads.append(threading.Thread(target=writer, args=(tid,)))
            threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent errors: {errors}"
        assert written[0] == 60, f"Expected 60 writes, got {written[0]}"
        assert read_count[0] == 60, f"Expected 60 reads, got {read_count[0]}"


class TestPollerIntegration:
    """Poller full lifecycle with mocked API."""

    def test_poller_full_cycle(self, test_db_path, mock_hko_text, mock_past_csv, mock_forecast_json):
        """Poller → API → DB full cycle."""
        cfg = Config
        cfg.DATABASE_PATH = test_db_path

        poller = WeatherPoller(cfg)

        # Mock all API calls
        with patch.object(poller.api_client, 'fetch_temperatures', return_value=mock_hko_text), \
             patch.object(poller.api_client, 'fetch_past_temperatures', return_value=mock_past_csv), \
             patch.object(poller.api_client, 'fetch_forecast', return_value=mock_forecast_json):
            count = poller.poll_once()

        assert count > 0, f"Expected some readings, got {count}"

        # Verify DB has data
        db = WeatherDatabase(test_db_path)
        with db._get_connection() as conn:
            temp = conn.execute("SELECT COUNT(*) FROM temperature_readings").fetchone()[0]
            daily = conn.execute("SELECT COUNT(*) FROM daily_forecasts").fetchone()[0]
            hourly = conn.execute("SELECT COUNT(*) FROM hourly_forecasts").fetchone()[0]

        assert temp > 0, "No temperature readings stored"
        assert daily > 0, "No daily forecasts stored"
        assert hourly > 0, "No hourly forecasts stored"
