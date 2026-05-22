import pytest
import os
import tempfile
from datetime import datetime
import sqlite3
import sys

# Force test DB path before db.py is imported
_test_db = str(tempfile.mktemp(suffix=".db"))
os.environ["DATABASE_PATH"] = _test_db

from hko_weather_monitor.db import init_db, get_connection, bulk_insert_readings, DB_PATH
import hko_weather_monitor.db as db_mod


@pytest.fixture(autouse=True)
def _reset_test_db():
    """Each test gets a fresh DB."""
    global _test_db
    _test_db = str(tempfile.mktemp(suffix=".db"))
    os.environ["DATABASE_PATH"] = _test_db
    # Re-init with clean DB
    if os.path.exists(_test_db):
        os.remove(_test_db)
    init_db()
    yield
    if os.path.exists(_test_db):
        os.remove(_test_db)


def test_init_creates_tables():
    """Database initialization creates the required tables."""
    conn = get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()

    assert "stations" in tables
    assert "readings" in tables
    assert "forecast_hourly" in tables
    assert "forecast_daily" in tables
    assert "trigger_log" in tables
    assert "market_outcomes" in tables
    assert "paper_positions" in tables


def test_insert_single_temperature():
    """Insert a single temperature reading via bulk_insert_readings."""
    bulk_insert_readings([
        ("HK Observatory", {"temperature": 26.5, "recorded_at": "2026/05/20 12:30"})
    ])

    conn = get_connection()
    row = conn.execute("""
        SELECT s.name, r.temperature, r.recorded_at
        FROM readings r JOIN stations s ON r.station_id = s.id
    """).fetchone()
    conn.close()

    assert row["name"] == "HK Observatory"
    assert row["temperature"] == 26.5


def test_insert_batch_readings():
    """Insert multiple readings at once."""
    readings = [
        ("HK Observatory", {"temperature": 26.0, "recorded_at": "2026/05/20 12:00"}),
        ("Sha Tin", {"temperature": 27.5, "recorded_at": "2026/05/20 12:00"}),
        ("King's Park", {"temperature": 28.0, "recorded_at": "2026/05/20 12:00"}),
    ]
    count = bulk_insert_readings(readings)
    assert count == 3

    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) as count FROM readings").fetchone()["count"]
    conn.close()
    assert total == 3


def test_station_uniqueness():
    """Same station name should not create duplicate entries."""
    bulk_insert_readings([
        ("HK Observatory", {"temperature": 26.0, "recorded_at": "2026/05/20 12:00"}),
        ("HK Observatory", {"temperature": 26.5, "recorded_at": "2026/05/20 12:05"}),
    ])

    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) as count FROM stations WHERE name = 'HK Observatory'").fetchone()["count"]
    conn.close()
    assert count == 1


def test_get_connection_returns_row_factory():
    """get_connection should use sqlite3.Row for dict-like access."""
    conn = get_connection()
    row = conn.execute("SELECT 1 as val").fetchone()
    conn.close()
    assert row["val"] == 1
