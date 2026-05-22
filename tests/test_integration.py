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
from hko_weather_monitor.db import init_db, get_connection, bulk_insert_readings, \
    bulk_insert_forecasts_daily, bulk_insert_forecasts_hourly


@pytest.fixture
def test_db_path(tmp_path):
    """Isolated temp database for each test."""
    return str(tmp_path / "test.db")


@pytest.fixture(autouse=True)
def _use_test_db(test_db_path):
    """Point all DB calls to the temp path."""
    os.environ["DATABASE_PATH"] = test_db_path
    init_db()
    yield
    # cleanup handled by tmp_path


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

    def test_full_pipeline_flow(self, mock_hko_text, mock_past_csv, mock_forecast_json):
        """API fetch → parse → DB insert → query roundtrip."""
        api = HKOApiClient(
            base_url="https://www.hko.gov.hk/wxinfo/awsgis",
            aws_datafile="latestReadings_AWS1_v2.txt",
            historical_temp_file="animate_J1.csv",
            timeout=10, max_retries=1, backoff_factor=0.5,
        )

        # Parse temperatures
        readings = api.parse_temperatures(mock_hko_text)
        assert len(readings) == 3, f"Expected 3 readings, got {len(readings)}"

        # Parse past temperatures
        past = api.parse_past_temperatures(mock_past_csv)
        assert len(past) == 3, f"Expected 3 past readings, got {len(past)}"

        # Insert into DB via db.py functions
        records = [(r["station_name"], {
            "temperature": r["temperature"],
            "recorded_at": r["recorded_at"].strftime("%Y/%m/%d %H:%M")
        }) for r in readings]
        bulk_insert_readings(records)
        past_records = [(r["station_name"], {
            "temperature": r["temperature"],
            "recorded_at": r["recorded_at"].strftime("%Y/%m/%d %H:%M")
        }) for r in past]
        bulk_insert_readings(past_records)

        # Insert forecast
        daily = api.parse_daily_forecast(mock_forecast_json)
        hourly = api.parse_hourly_forecast(mock_forecast_json)
        assert len(daily) == 1
        assert len(hourly) == 1

        daily_records = [(r["station_code"].upper(), r["forecast_date"].strftime("%Y%m%d"),
                          r["max_temperature"], r["min_temperature"],
                          r["chance_of_rain"], r["weather_code"], None, None) for r in daily]
        bulk_insert_forecasts_daily(daily_records)

        hourly_records = [(r["station_code"].upper(),
                           r["forecast_time"].strftime("%Y%m%d%H%M") if r["forecast_time"] else "",
                           r["temperature"],
                           float(r["humidity"]) if r["humidity"] is not None else None,
                           float(r["wind_speed"]) if r["wind_speed"] is not None else None,
                           float(r["wind_direction"]) if r["wind_direction"] is not None else None,
                           None, None) for r in hourly]
        bulk_insert_forecasts_hourly(hourly_records)

        # Verify DB roundtrip
        conn = get_connection()
        station_count = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
        reading_count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        daily_count = conn.execute("SELECT COUNT(*) FROM forecast_daily").fetchone()[0]
        hourly_count = conn.execute("SELECT COUNT(*) FROM forecast_hourly").fetchone()[0]
        conn.close()

        assert station_count >= 3, f"Expected at least 3 stations, got {station_count}"
        assert reading_count == 6, f"Expected 6 readings, got {reading_count}"
        assert daily_count == 1
        assert hourly_count == 1


class TestConcurrentReadWrite:
    """Part B.2 — Race condition simulation."""

    def test_concurrent_read_write(self):
        """Multiple writers and readers shouldn't crash."""
        errors = []
        written = [0]
        read_count = [0]

        def writer(thread_id):
            for i in range(20):
                try:
                    station = f"Thread{thread_id}Station{i % 3}"
                    bulk_insert_readings([(station, {
                        "temperature": 25.0 + i * 0.1,
                        "recorded_at": f"2026/05/22 14:{i:02d}:{thread_id*2:02d}"
                    })])
                    written[0] += 1
                except Exception as e:
                    errors.append(str(e))

        def reader():
            for _ in range(20):
                try:
                    conn = get_connection()
                    conn.execute("SELECT COUNT(*) FROM readings")
                    conn.close()
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

    def test_poller_full_cycle(self, mock_hko_text, mock_past_csv, mock_forecast_json):
        """Poller → API → DB full cycle."""
        cfg = Config
        cfg.STATION_WHITELIST = []
        cfg.HKO_API_URL = "https://www.hko.gov.hk/wxinfo/awsgis"
        cfg.AWS_DATAFILE = "latestReadings_AWS1_v2.txt"
        cfg.HISTORICAL_TEMP_FILE = "animate_J1.csv"
        cfg.FORECAST_URL = "https://www.hko.gov.hk/wxinfo/awsgis/forecast/{STATION}.xml"
        cfg.REQUEST_TIMEOUT = 10
        cfg.MAX_RETRIES = 1
        cfg.RETRY_BACKOFF_FACTOR = 0.5

        poller = WeatherPoller(cfg)

        with patch.object(poller.api_client, 'fetch_temperatures', return_value=mock_hko_text), \
             patch.object(poller.api_client, 'fetch_past_temperatures', return_value=mock_past_csv), \
             patch.object(poller.api_client, 'fetch_forecast', return_value=mock_forecast_json):
            count = poller.poll_once()

        assert count > 0, f"Expected some readings, got {count}"

        # Verify DB has data
        conn = get_connection()
        temp = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        daily = conn.execute("SELECT COUNT(*) FROM forecast_daily").fetchone()[0]
        hourly = conn.execute("SELECT COUNT(*) FROM forecast_hourly").fetchone()[0]
        conn.close()

        assert temp > 0, "No temperature readings stored"
        assert daily > 0, "No daily forecasts stored"
        assert hourly > 0, "No hourly forecasts stored"
