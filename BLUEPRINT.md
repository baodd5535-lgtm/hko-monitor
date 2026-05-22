Corrected Implementation Plan: HKO Weather Monitoring Service (Python + SQLite)
Overview

A production‑ready Python service that polls the Hong Kong Observatory (HKO) Open Data API every 5 minutes, extracts temperatures from all 27 stations, and logs them to a local SQLite database. The service runs as a background daemon, handles API errors gracefully, and supports configuration via environment variables.

File Structure
text
Copy
Download
hko_weather_monitor/
├── README.md
├── requirements.txt
├── .env.example
├── hko_monitor/
│   ├── __init__.py
│   ├── config.py
│   ├── database.py
│   ├── api_client.py
│   ├── poller.py
│   └── main.py
├── tests/
│   ├── __init__.py
│   ├── test_api_client.py
│   ├── test_database.py
│   └── test_poller.py
└── scripts/
    └── install_service.sh          # optional: systemd service installer
1. Configuration Module (hko_monitor/config.py)

Handles environment variables and constants. Use python-dotenv for local development.

python
Copy
Download
# hko_monitor/config.py
import os
from dotenv import load_dotenv

load_dotenv()  # loads from .env if present

class Config:
    # API settings
    HKO_API_URL = os.getenv("HKO_API_URL", "https://data.weather.gov.hk/weatherAPI/opendata/weather.php")
    HKO_DATA_TYPE = os.getenv("HKO_DATA_TYPE", "rhrread")
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
    RETRY_BACKOFF_FACTOR = float(os.getenv("RETRY_BACKOFF_FACTOR", "1.0"))

    # Polling interval (seconds)
    POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 minutes

    # Database path (absolute or relative)
    DATABASE_PATH = os.getenv("DATABASE_PATH", "weather_data.db")

    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = os.getenv("LOG_FILE", "hko_monitor.log")

    # Optional: filter specific stations (comma-separated names, empty = all)
    STATION_WHITELIST = [s.strip() for s in os.getenv("STATION_WHITELIST", "").split(",") if s.strip()]
2. Database Module (hko_monitor/database.py)

SQLite schema and insert logic. Uses sqlite3 (stdlib).

python
Copy
Download
# hko_monitor/database.py
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class WeatherDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stations (
                    station_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS temperature_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_id INTEGER NOT NULL,
                    recorded_at TIMESTAMP NOT NULL,
                    temperature_celsius REAL NOT NULL,
                    FOREIGN KEY (station_id) REFERENCES stations(station_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_readings_station_time 
                ON temperature_readings(station_id, recorded_at)
            """)
            conn.commit()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get_or_create_station_id(self, station_name: str) -> int:
        """Return station ID, creating if not exists."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT station_id FROM stations WHERE name = ?", (station_name,))
            row = cursor.fetchone()
            if row:
                return row["station_id"]
            cursor = conn.execute("INSERT INTO stations (name) VALUES (?)", (station_name,))
            conn.commit()
            return cursor.lastrowid

    def insert_temperature(self, station_name: str, recorded_at: datetime, temperature: float):
        """Insert a single temperature reading."""
        station_id = self.get_or_create_station_id(station_name)
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO temperature_readings (station_id, recorded_at, temperature_celsius) VALUES (?, ?, ?)",
                (station_id, recorded_at, temperature)
            )
            conn.commit()
            logger.debug(f"Inserted: {station_name} {temperature}°C at {recorded_at}")

    def insert_batch(self, readings: List[Dict[str, Any]]):
        """
        Insert multiple readings.
        Each dict: {'station_name': str, 'recorded_at': datetime, 'temperature': float}
        """
        with self._get_connection() as conn:
            for r in readings:
                station_id = self.get_or_create_station_id(r["station_name"])
                conn.execute(
                    "INSERT INTO temperature_readings (station_id, recorded_at, temperature_celsius) VALUES (?, ?, ?)",
                    (station_id, r["recorded_at"], r["temperature"])
                )
            conn.commit()
            logger.info(f"Inserted {len(readings)} temperature readings")
3. API Client Module (hko_monitor/api_client.py)

Robust wrapper with retries, correct JSON path (data.temperature.recordTime and data.temperature.data).

python
Copy
Download
# hko_monitor/api_client.py
import requests
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

class HKOApiClient:
    def __init__(self, base_url: str, data_type: str, timeout: int, max_retries: int, backoff_factor: float):
        self.base_url = base_url
        self.data_type = data_type
        self.timeout = timeout
        self.session = self._create_session(max_retries, backoff_factor)

    def _create_session(self, max_retries: int, backoff_factor: float) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def fetch_temperatures(self) -> Optional[Dict[str, Any]]:
        """Fetch raw JSON from HKO API."""
        url = f"{self.base_url}?dataType={self.data_type}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            logger.debug("API response received successfully")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            return None
        except ValueError as e:
            logger.error(f"Invalid JSON response: {e}")
            return None

    def parse_temperatures(self, raw_data: Dict[str, Any], station_whitelist: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Extract temperatures from the correct nested structure.
        Input: raw_data as returned by fetch_temperatures()
        Output: list of dicts with keys: station_name, recorded_at, temperature
        """
        try:
            # Correct API path: data.temperature.recordTime and data.temperature.data
            temperature_block = raw_data.get("temperature")
            if not temperature_block:
                logger.error("Missing 'temperature' key in API response")
                return []

            record_time_str = temperature_block.get("recordTime")
            if not record_time_str:
                logger.error("Missing 'recordTime' inside temperature block")
                return []

            # Parse ISO8601 timestamp (e.g., "2026-05-19T12:34:56+08:00")
            recorded_at = datetime.fromisoformat(record_time_str.replace("Z", "+00:00"))

            stations_data = temperature_block.get("data", [])
            if not isinstance(stations_data, list):
                logger.error("'data' field is not a list")
                return []

            readings = []
            for station in stations_data:
                place = station.get("place")
                value = station.get("value")
                if place is None or value is None:
                    logger.warning(f"Skipping station with missing place/value: {station}")
                    continue

                # Optional station whitelist filtering
                if station_whitelist and place not in station_whitelist:
                    continue

                readings.append({
                    "station_name": place,
                    "recorded_at": recorded_at,
                    "temperature": float(value)  # ensure float
                })

            logger.info(f"Parsed {len(readings)} temperature readings at {recorded_at}")
            return readings

        except Exception as e:
            logger.exception(f"Failed to parse API response: {e}")
            return []
4. Poller Module (hko_monitor/poller.py)

Main loop with 5‑minute interval, error handling, logging, and graceful shutdown.

python
Copy
Download
# hko_monitor/poller.py
import logging
import time
import signal
import sys
from datetime import datetime
from typing import Optional

from .config import Config
from .database import WeatherDatabase
from .api_client import HKOApiClient

logger = logging.getLogger(__name__)

class WeatherPoller:
    def __init__(self, config: Config):
        self.config = config
        self.db = WeatherDatabase(config.DATABASE_PATH)
        self.api_client = HKOApiClient(
            base_url=config.HKO_API_URL,
            data_type=config.HKO_DATA_TYPE,
            timeout=config.REQUEST_TIMEOUT,
            max_retries=config.MAX_RETRIES,
            backoff_factor=config.RETRY_BACKOFF_FACTOR
        )
        self.running = True

    def poll_once(self) -> int:
        """Perform one poll cycle. Returns number of readings stored."""
        logger.info("Polling HKO API...")
        raw_data = self.api_client.fetch_temperatures()
        if raw_data is None:
            logger.warning("Skipping this poll due to API error")
            return 0

        readings = self.api_client.parse_temperatures(raw_data, self.config.STATION_WHITELIST)
        if not readings:
            logger.warning("No valid temperature readings extracted")
            return 0

        # Batch insert
        self.db.insert_batch(readings)
        return len(readings)

    def run(self):
        """Main polling loop. Handles SIGINT/SIGTERM."""
        logger.info(f"Starting weather poller, interval={self.config.POLL_INTERVAL_SECONDS}s")
        while self.running:
            try:
                count = self.poll_once()
                logger.info(f"Poll complete. Stored {count} readings.")
                # Sleep, but break early if interrupted
                for _ in range(self.config.POLL_INTERVAL_SECONDS):
                    if not self.running:
                        break
                    time.sleep(1)
            except Exception as e:
                logger.exception(f"Unexpected error in poll cycle: {e}")
                # Wait a bit before retrying
                time.sleep(10)

        logger.info("Poller stopped.")

    def stop(self):
        self.running = False
5. Main Entry Point (hko_monitor/main.py)

Sets up logging, parses CLI arguments (optional), starts poller, handles signals.

python
Copy
Download
# hko_monitor/main.py
import logging
import signal
import sys
from pathlib import Path

from .config import Config
from .poller import WeatherPoller

def setup_logging(config: Config):
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format=log_format,
        handlers=[
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

def main():
    config = Config()
    setup_logging(config)
    logger = logging.getLogger(__name__)

    poller = WeatherPoller(config)

    def signal_handler(sig, frame):
        logger.info("Received shutdown signal, stopping poller...")
        poller.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        poller.run()
    except KeyboardInterrupt:
        # Already handled by signal, but just in case
        pass
    finally:
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    main()
6. Requirements (requirements.txt)
text
Copy
Download
requests>=2.31.0
python-dotenv>=1.0.0
pytest>=7.4.0
pytest-mock>=3.12.0
7. Environment Example (.env.example)
text
Copy
Download
HKO_API_URL=https://data.weather.gov.hk/weatherAPI/opendata/weather.php
HKO_DATA_TYPE=rhrread
REQUEST_TIMEOUT=10
MAX_RETRIES=3
RETRY_BACKOFF_FACTOR=1.0
POLL_INTERVAL_SECONDS=300
DATABASE_PATH=/var/lib/hko_monitor/weather_data.db
LOG_LEVEL=INFO
LOG_FILE=/var/log/hko_monitor.log
# STATION_WHITELIST=King's Park,Tsim Sha Tsui  # optional, empty = all
8. Deployment Instructions
8.1 Local / Development
bash
Copy
Download
# Clone or create project
mkdir hko_weather_monitor && cd hko_weather_monitor
python -m venv venv
source venv/bin/activate  # or .\venv\Scripts\activate on Windows
pip install -r requirements.txt

# Copy .env.example to .env and edit as needed
cp .env.example .env

# Run once for testing
python -m hko_monitor.main

# Run as daemon in background (Linux/Mac)
nohup python -m hko_monitor.main > /dev/null 2>&1 &
8.2 Production – systemd Service (Linux)

Create a service file: /etc/systemd/system/hko-monitor.service

text
Copy
Download
[Unit]
Description=HKO Weather Monitor
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/hko_weather_monitor
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/python -m hko_monitor.main
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target

Then:

bash
Copy
Download
sudo systemctl daemon-reload
sudo systemctl enable hko-monitor
sudo systemctl start hko-monitor
sudo systemctl status hko-monitor
# View logs
journalctl -u hko-monitor -f
8.3 Docker Container (optional)

Dockerfile:

dockerfile
Copy
Download
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "hko_monitor.main"]

Build & run:

bash
Copy
Download
docker build -t hko-monitor .
docker run -d --name hko-monitor --restart always -v $(pwd)/data:/app/data hko-monitor
9. Testing

Unit test example (tests/test_api_client.py):

python
Copy
Download
import pytest
from datetime import datetime
from hko_monitor.api_client import HKOApiClient

def test_parse_temperatures_correct_structure():
    client = HKOApiClient("", "", 10, 1, 1.0)
    sample = {
        "temperature": {
            "recordTime": "2026-05-19T12:34:56+08:00",
            "data": [
                {"place": "Station A", "value": 25.5, "unit": "C"},
                {"place": "Station B", "value": 26.0, "unit": "C"}
            ]
        }
    }
    readings = client.parse_temperatures(sample)
    assert len(readings) == 2
    assert readings[0]["station_name"] == "Station A"
    assert readings[0]["temperature"] == 25.5
    assert isinstance(readings[0]["recorded_at"], datetime)

def test_parse_temperatures_missing_temperature():
    client = HKOApiClient("", "", 10, 1, 1.0)
    sample = {}
    readings = client.parse_temperatures(sample)
    assert readings == []

Run tests:

bash
Copy
Download
pytest tests/ -v
10. Verification Checklist

Service polls every 5 minutes (check logs for timestamps).

Database weather_data.db is created with tables stations and temperature_readings.

All 27 stations are recorded (query SELECT COUNT(DISTINCT station_id) FROM temperature_readings after two polls).

On API failure, service retries and does not crash.

On SIGTERM, service shuts down cleanly.

Log rotation is configured (optional, but recommended).

This plan is production‑ready, modular, and directly addresses the original goal – no frontend, no D3.js, no incorrect API paths.