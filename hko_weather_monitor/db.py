"""SQLite time-series database for HKO weather data."""
import sqlite3
import os
import threading
from datetime import datetime
from typing import Optional

DB_PATH = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "data", "hko_weather.db"))

# Thread lock for write operations (adaptive poller vs HTTP handler)
_db_lock = threading.Lock()


def get_connection() -> sqlite3.Connection:
    """Get database connection, creating if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """Initialize database schema."""
    with _db_lock:
        conn = get_connection()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id INTEGER NOT NULL,
                temperature REAL,
                humidity INTEGER,
                recorded_at TIMESTAMP NOT NULL,
                scrape_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (station_id) REFERENCES stations(id),
                UNIQUE(station_id, recorded_at)
            );

            CREATE INDEX IF NOT EXISTS idx_readings_station ON readings(station_id);
            CREATE INDEX IF NOT EXISTS idx_readings_time ON readings(recorded_at);
            CREATE INDEX IF NOT EXISTS idx_readings_station_time ON readings(station_id, recorded_at);

            CREATE TABLE IF NOT EXISTS scrape_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                message TEXT,
                duration_seconds REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Forecast tables
            CREATE TABLE IF NOT EXISTS forecast_hourly (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_code TEXT NOT NULL,
                forecast_hour TEXT NOT NULL,
                temperature REAL,
                humidity REAL,
                wind_speed REAL,
                wind_direction REAL,
                model_time TEXT,
                last_modified TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(station_code, forecast_hour, model_time)
            );

            CREATE INDEX IF NOT EXISTS idx_fh_station ON forecast_hourly(station_code);
            CREATE INDEX IF NOT EXISTS idx_fh_hour ON forecast_hourly(forecast_hour);

            CREATE TABLE IF NOT EXISTS forecast_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_code TEXT NOT NULL,
                forecast_date TEXT NOT NULL,
                max_temperature REAL,
                min_temperature REAL,
                chance_of_rain TEXT,
                weather_code INTEGER,
                model_time TEXT,
                last_modified TEXT,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(station_code, forecast_date, model_time)
            );

            CREATE INDEX IF NOT EXISTS idx_fd_station ON forecast_daily(station_code);
            CREATE INDEX IF NOT EXISTS idx_fd_date ON forecast_daily(forecast_date);

            -- 9-day forecast from HKO JSON API
            CREATE TABLE IF NOT EXISTS forecast_nine_day (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                forecast_date TEXT NOT NULL,
                date_str TEXT,
                min_temp REAL,
                max_temp REAL,
                rh_range TEXT,
                rain_prob TEXT,
                weather_desc TEXT,
                wind_info TEXT,
                wx_icon TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_fnd_date ON forecast_nine_day(forecast_date);

            -- Trigger log (engine heartbeats + trade signals)
            CREATE TABLE IF NOT EXISTS trigger_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                type TEXT NOT NULL,
                message TEXT
            );

            -- Polymarket markets registry (categorical outcomes)
            CREATE TABLE IF NOT EXISTS markets (
                condition_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                slug TEXT NOT NULL,
                target_date TEXT NOT NULL,
                resolution_source TEXT NOT NULL,
                status TEXT DEFAULT 'ACTIVE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Market outcomes (one row per categorical bucket)
            CREATE TABLE IF NOT EXISTS market_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT NOT NULL REFERENCES markets(condition_id),
                outcome_name TEXT NOT NULL,
                temp_min REAL,
                temp_max REAL,
                yes_token_id TEXT NOT NULL,
                UNIQUE(condition_id, outcome_name)
            );
            CREATE INDEX IF NOT EXISTS idx_mo_condition ON market_outcomes(condition_id);
            CREATE INDEX IF NOT EXISTS idx_mo_token ON market_outcomes(yes_token_id);

            -- Market ticks (5-15 min snapshots)
            CREATE TABLE IF NOT EXISTS market_ticks (
                tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                polymarket_yes_price REAL,
                polymarket_no_price REAL,
                hko_predicted_value REAL,
                hko_forecast_horizon_days INTEGER,
                model_calculated_prob REAL,
                generated_signal TEXT,
                FOREIGN KEY(condition_id) REFERENCES markets(condition_id)
            );

            -- Accounts
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                cash_balance REAL DEFAULT 10000.00,
                allocated_margin REAL DEFAULT 0.00
            );

            -- Orderbook state snapshots (token_id is TEXT — 76-digit numbers exceed SQLite INTEGER)
            CREATE TABLE IF NOT EXISTS orderbook_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT,
                token_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                side TEXT,
                price REAL,
                size REAL,
                best_bid REAL,
                best_ask REAL,
                updated_at TEXT
            );

            -- Paper positions (token_id is TEXT)
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT,
                condition_id TEXT,
                token_id TEXT,
                side TEXT,
                qty REAL,
                avg_entry_price REAL,
                status TEXT DEFAULT 'OPEN',
                pnl REAL DEFAULT 0.00,
                opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME,
                FOREIGN KEY(account_id) REFERENCES accounts(account_id),
                FOREIGN KEY(condition_id) REFERENCES markets(condition_id)
            );

            -- Paper fills (execution ledger, token_id is TEXT)
            CREATE TABLE IF NOT EXISTS paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT,
                condition_id TEXT,
                token_id TEXT,
                order_side TEXT,
                requested_value REAL,
                filled_qty REAL,
                avg_fill_price REAL,
                slippage_paid REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Engine status (heartbeats)
            CREATE TABLE IF NOT EXISTS engine_status (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.execute("INSERT OR IGNORE INTO accounts (account_id, cash_balance) VALUES ('paper_user', 10000.0)")
        conn.commit()
        conn.close()


def upsert_station(name: str) -> int:
    """Add station if new, return station ID."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM stations WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row["id"]
    with _db_lock:
        cur.execute("INSERT OR IGNORE INTO stations (name) VALUES (?)", (name,))
        conn.commit()
        station_id = cur.lastrowid
    conn.close()
    return station_id


def bulk_insert_readings(records: list):
    """Bulk insert weather readings (station_name, data_dict) — single transaction."""
    # First, upsert all stations in one go
    station_names = {r[0] for r in records}
    conn = get_connection()
    with _db_lock:
        cur = conn.cursor()
        # Upsert stations
        cur.executemany(
            "INSERT OR IGNORE INTO stations (name) VALUES (?)",
            [(n,) for n in station_names],
        )
        conn.commit()

        # Lookup station IDs
        cur.execute("SELECT id, name FROM stations WHERE name IN ({})".format(
            ",".join("?" * len(station_names))
        ), list(station_names))
        name_to_id = {row["name"]: row["id"] for row in cur.fetchall()}

        # Bulk insert readings
        insert_data = []
        for station_name, data in records:
            sid = name_to_id[station_name]
            insert_data.append((
                sid,
                data.get("temperature"),
                data.get("humidity"),
                data.get("recorded_at", datetime.now().isoformat()),
            ))
        cur.executemany("""
            INSERT OR REPLACE INTO readings
            (station_id, temperature, humidity, recorded_at)
            VALUES (?, ?, ?, ?)
        """, insert_data)
        conn.commit()
    conn.close()
    return len(insert_data)


def insert_reading(station_name: str, data: dict):
    """Insert a weather reading for a station (upsert by station+time)."""
    bulk_insert_readings([(station_name, data)])


def log_scrape(status: str, message: str = "", duration: float = 0):
    """Log a scrape attempt."""
    conn = get_connection()
    with _db_lock:
        conn.execute(
            "INSERT INTO scrape_log (status, message, duration_seconds) VALUES (?, ?, ?)",
            (status, message, duration),
        )
        conn.commit()
    conn.close()


def get_all_stations() -> list:
    """Get all stations."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM stations ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_readings() -> list:
    """Get the most recent reading for each station."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT s.name, r.temperature, r.humidity, r.recorded_at
        FROM readings r
        JOIN stations s ON r.station_id = s.id
        WHERE r.recorded_at = (
            SELECT MAX(r2.recorded_at) FROM readings r2 WHERE r2.station_id = r.station_id
        )
        ORDER BY r.temperature ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_temperature_history(station_name: str, hours: int = 24) -> list:
    """Get temperature history for a station."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.temperature, r.humidity, r.recorded_at
        FROM readings r
        JOIN stations s ON r.station_id = s.id
        WHERE s.name = ? AND r.recorded_at >= datetime('now', ? || ' hours')
        ORDER BY r.recorded_at
    """, (station_name, f"-{hours}")).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_history(hours: int = 24) -> list:
    """Get all temperature history for charting."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT s.name, r.temperature, r.humidity, r.recorded_at
        FROM readings r
        JOIN stations s ON r.station_id = s.id
        WHERE r.recorded_at >= datetime('now', ? || ' hours')
        ORDER BY r.recorded_at, s.name
    """, (f"-{hours}",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_history_table(station_name: str, offset: int = 0, limit: int = 50) -> list:
    """Get paginated history for a station, newest first."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.temperature, r.humidity, r.recorded_at
        FROM readings r
        JOIN stations s ON r.station_id = s.id
        WHERE s.name = ?
        ORDER BY r.recorded_at DESC
        LIMIT ? OFFSET ?
    """, (station_name, limit, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Forecast queries ──────────────────────────────────────────


def bulk_insert_forecasts_hourly(records: list):
    """Bulk insert hourly forecasts. records: list of (station_code, hour, temp, rh, ws, wd, model_time, last_modified)"""
    conn = get_connection()
    with _db_lock:
        conn.executemany("""
            INSERT OR REPLACE INTO forecast_hourly
            (station_code, forecast_hour, temperature, humidity, wind_speed, wind_direction, model_time, last_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        conn.commit()
    conn.close()
    return len(records)


def bulk_insert_forecasts_daily(records: list):
    """Bulk insert daily forecasts. records: list of (station_code, date, maxt, mint, chance_of_rain, weather_code, model_time, last_modified)"""
    conn = get_connection()
    with _db_lock:
        conn.executemany("""
            INSERT OR REPLACE INTO forecast_daily
            (station_code, forecast_date, max_temperature, min_temperature, chance_of_rain, weather_code, model_time, last_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        conn.commit()
    conn.close()
    return len(records)


def get_latest_forecasts_hourly(station_code: str = "HKO") -> list:
    """Get most recent hourly forecast for a station."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM forecast_hourly
        WHERE station_code = ?
        AND model_time = (
            SELECT MAX(model_time) FROM forecast_hourly WHERE station_code = ?
        )
        ORDER BY forecast_hour
    """, (station_code, station_code)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_forecasts_daily(station_code: str = "HKO") -> list:
    """Get most recent daily forecast for a station."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM forecast_daily
        WHERE station_code = ?
        AND model_time = (
            SELECT MAX(model_time) FROM forecast_daily WHERE station_code = ?
        )
        ORDER BY forecast_date
    """, (station_code, station_code)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_forecast_station_codes() -> list:
    """Get all forecast station codes from the DB."""
    conn = get_connection()
    rows = conn.execute("SELECT DISTINCT station_code FROM forecast_hourly ORDER BY station_code").fetchall()
    conn.close()
    return [r["station_code"] for r in rows]


# Initialize on import
init_db()


def bulk_insert_nine_day_forecast(records):
    """Bulk insert 9-day forecast data."""
    conn = get_connection()
    with _db_lock:
        conn.execute("DELETE FROM forecast_nine_day")
        for r in records:
            conn.execute(
                "INSERT INTO forecast_nine_day (forecast_date, date_str, min_temp, max_temp, rh_range, rain_prob, weather_desc, wind_info, wx_icon) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r['forecast_date'], r['date_str'], r['min_temp'], r['max_temp'], r['rh_range'], r.get('rain_prob'), r.get('weather_desc'), r.get('wind_info'), r.get('wx_icon'))
            )
        conn.commit()
    conn.close()
    return len(records)


def get_nine_day_forecast():
    """Get the most recent 9-day forecast data."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT forecast_date, date_str, min_temp, max_temp, rh_range, rain_prob, weather_desc, wind_info, wx_icon
        FROM forecast_nine_day
        ORDER BY forecast_date
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]
