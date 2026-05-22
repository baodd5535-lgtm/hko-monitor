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
                    code TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL
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
            # Forecast tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_id INTEGER NOT NULL,
                    forecast_date DATE NOT NULL,
                    max_temperature_celsius REAL,
                    min_temperature_celsius REAL,
                    chance_of_rain TEXT,
                    weather_code INTEGER,
                    FOREIGN KEY (station_id) REFERENCES stations(station_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hourly_forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_id INTEGER NOT NULL,
                    forecast_time TIMESTAMP NOT NULL,
                    temperature_celsius REAL NOT NULL,
                    wind_speed REAL,
                    wind_direction REAL,
                    humidity REAL,
                    FOREIGN KEY (station_id) REFERENCES stations(station_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hourly_station_time
                ON hourly_forecasts(station_id, forecast_time)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_station_date
                ON daily_forecasts(station_id, forecast_date)
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

    def get_or_create_station_id(self, station_code: str, station_name: str, conn: sqlite3.Connection = None) -> int:
        """Return station ID, creating if not exists.
        
        If conn is provided, use it (for batch operations within an existing transaction).
        Otherwise, open a new connection.
        """
        own_conn = conn is None
        if own_conn:
            with self._get_connection() as c:
                return self._get_or_create_station_id_inner(c, station_code, station_name)
        else:
            return self._get_or_create_station_id_inner(conn, station_code, station_name)

    def _get_or_create_station_id_inner(self, conn: sqlite3.Connection, station_code: str, station_name: str) -> int:
        cursor = conn.execute("SELECT station_id FROM stations WHERE code = ?", (station_code,))
        row = cursor.fetchone()
        if row:
            return row["station_id"]
        cursor = conn.execute("INSERT INTO stations (code, name) VALUES (?, ?)", (station_code, station_name))
        conn.commit()
        return cursor.lastrowid

    def insert_temperature(self, station_code: str, station_name: str, recorded_at: datetime, temperature: float):
        """Insert a single temperature reading."""
        station_id = self.get_or_create_station_id(station_code, station_name)
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO temperature_readings (station_id, recorded_at, temperature_celsius) VALUES (?, ?, ?)",
                (station_id, recorded_at, temperature)
            )
            conn.commit()
            logger.debug(f"Inserted: {station_name} {temperature}°C at {recorded_at}")

    def insert_batch(self, readings: List[Dict[str, Any]]):
        """
        Insert multiple readings within a single connection/transaction.
        Each dict: {'station_code': str, 'station_name': str, 'recorded_at': datetime, 'temperature': float}
        """
        with self._get_connection() as conn:
            for r in readings:
                station_id = self.get_or_create_station_id(r["station_code"], r["station_name"], conn=conn)
                conn.execute(
                    "INSERT INTO temperature_readings (station_id, recorded_at, temperature_celsius) VALUES (?, ?, ?)",
                    (station_id, r["recorded_at"], r["temperature"])
                )
            conn.commit()
            logger.info(f"Inserted {len(readings)} temperature readings")

    def insert_daily_forecast(self, station_code: str, station_name: str,
                               forecast_date, max_temp: float, min_temp: float,
                               chance_of_rain: str, weather_code: int):
        """Insert a daily forecast."""
        station_id = self.get_or_create_station_id(station_code, station_name)
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO daily_forecasts (station_id, forecast_date, max_temperature_celsius, "
                "min_temperature_celsius, chance_of_rain, weather_code) VALUES (?, ?, ?, ?, ?, ?)",
                (station_id, forecast_date, max_temp, min_temp, chance_of_rain, weather_code)
            )
            conn.commit()
            logger.debug(f"Inserted daily forecast: {station_name} {forecast_date} max={max_temp} min={min_temp}")

    def insert_daily_forecasts_batch(self, forecasts: List[Dict[str, Any]]):
        """Insert multiple daily forecasts."""
        with self._get_connection() as conn:
            for f in forecasts:
                station_id = self.get_or_create_station_id(f["station_code"], f["station_name"], conn=conn)
                conn.execute(
                    "INSERT INTO daily_forecasts (station_id, forecast_date, max_temperature_celsius, "
                    "min_temperature_celsius, chance_of_rain, weather_code) VALUES (?, ?, ?, ?, ?, ?)",
                    (station_id, f["forecast_date"], f["max_temperature"], f["min_temperature"],
                     f["chance_of_rain"], f["weather_code"])
                )
            conn.commit()
            logger.info(f"Inserted {len(forecasts)} daily forecasts")

    def insert_hourly_forecast(self, station_code: str, station_name: str,
                                forecast_time: datetime, temperature: float,
                                wind_speed=None, wind_direction=None, humidity=None):
        """Insert a single hourly forecast."""
        station_id = self.get_or_create_station_id(station_code, station_name)
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO hourly_forecasts (station_id, forecast_time, temperature_celsius, "
                "wind_speed, wind_direction, humidity) VALUES (?, ?, ?, ?, ?, ?)",
                (station_id, forecast_time, temperature, wind_speed, wind_direction, humidity)
            )
            conn.commit()

    def insert_hourly_forecasts_batch(self, forecasts: List[Dict[str, Any]]):
        """Insert multiple hourly forecasts."""
        with self._get_connection() as conn:
            for f in forecasts:
                if f.get("forecast_time") is None:
                    continue
                station_id = self.get_or_create_station_id(f["station_code"], f["station_name"], conn=conn)
                conn.execute(
                    "INSERT INTO hourly_forecasts (station_id, forecast_time, temperature_celsius, "
                    "wind_speed, wind_direction, humidity) VALUES (?, ?, ?, ?, ?, ?)",
                    (station_id, f["forecast_time"], f["temperature"],
                     f.get("wind_speed"), f.get("wind_direction"), f.get("humidity"))
                )
            conn.commit()
            logger.info(f"Inserted {len(forecasts)} hourly forecasts")
