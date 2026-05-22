import pytest
import os
import tempfile
from datetime import datetime
from hko_weather_monitor.database import WeatherDatabase


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def weather_db(db_path):
    db = WeatherDatabase(db_path)
    yield db
    # Cleanup happens automatically with tmp_path


def test_init_creates_tables(weather_db, db_path):
    """Database initialization creates the required tables."""
    assert os.path.exists(db_path)
    
    # Verify tables exist by querying sqlite_master
    import sqlite3
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    assert "stations" in tables
    assert "temperature_readings" in tables


def test_insert_single_temperature(weather_db):
    """Insert a single temperature reading."""
    recorded_at = datetime(2026, 5, 20, 12, 30)
    weather_db.insert_temperature("hko", "HK Observatory", recorded_at, 26.5)
    
    # Verify the data was inserted
    import sqlite3
    conn = sqlite3.connect(weather_db.db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT s.name, t.temperature_celsius, t.recorded_at 
        FROM temperature_readings t 
        JOIN stations s ON t.station_id = s.station_id
    """)
    row = cursor.fetchone()
    conn.close()
    
    assert row["name"] == "HK Observatory"
    assert row["temperature_celsius"] == 26.5


def test_insert_batch_readings(weather_db):
    """Insert multiple readings at once."""
    readings = [
        {"station_code": "hko", "station_name": "HK Observatory", "recorded_at": datetime(2026, 5, 20, 12, 0), "temperature": 26.0},
        {"station_code": "sha", "station_name": "Sha Tin", "recorded_at": datetime(2026, 5, 20, 12, 0), "temperature": 27.5},
        {"station_code": "kp", "station_name": "King's Park", "recorded_at": datetime(2026, 5, 20, 12, 0), "temperature": 28.0},
    ]
    
    weather_db.insert_batch(readings)
    
    # Verify all readings were inserted
    import sqlite3
    conn = sqlite3.connect(weather_db.db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT COUNT(*) as count FROM temperature_readings")
    count = cursor.fetchone()["count"]
    conn.close()
    
    assert count == 3


def test_station_uniqueness(weather_db):
    """Same station code should not create duplicate entries."""
    # Insert same station twice
    weather_db.insert_temperature("hko", "HK Observatory", datetime(2026, 5, 20, 12, 0), 26.0)
    weather_db.insert_temperature("hko", "HK Observatory", datetime(2026, 5, 20, 12, 5), 26.5)
    
    # Should only have one station entry
    import sqlite3
    conn = sqlite3.connect(weather_db.db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT COUNT(*) as count FROM stations WHERE code = 'hko'")
    count = cursor.fetchone()["count"]
    conn.close()
    
    assert count == 1


def test_get_or_create_station_id(weather_db):
    """Get or create station ID functionality."""
    # First call should create the station
    station_id_1 = weather_db.get_or_create_station_id("hko", "HK Observatory")
    # Second call should return the same ID
    station_id_2 = weather_db.get_or_create_station_id("hko", "HK Observatory")
    
    assert station_id_1 == station_id_2
