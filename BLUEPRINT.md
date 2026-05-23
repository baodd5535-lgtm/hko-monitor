HKO Weather Monitor: System Architecture & Implementation Brief
1. Architectural Overview

The HKO Weather Monitor is a production-grade, event-driven trading and weather analytics platform built with Python 3.11 and SQLite. It maps quantitative weather forecast errors against live prediction markets on Polymarket to identify mispriced structural probabilities, executing delta-neutral or variance-adjusted edge strategies.

┌────────────────────────────────────────────────────────────────────────┐
│                        WEATHER TRADING PLATFORM                        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
         ┌──────────────────────────┴──────────────────────────┐
         ▼                                                     ▼
┌─────────────────┐                                   ┌─────────────────┐
│ Poller Daemon   │                                   │ Trading Engine  │
│ (5-Min Interval)│                                   │ (Hybrid/Async)  │
└────────┬────────┘                                   └────────┬────────┘
         │ Scrapes Observations                                │ Live Orderbook Updates
         │ & Forecast Metrics                                  │ via CLOB WebSocket
         ▼                                                     ▼
┌────────────────────────────────────────────────────────────────────────┐
│ SQLite Time-Series DB (WAL Mode / Global Thread-Safe Write Lock)       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Multi-Threaded HTTP API Server & Inline-HTML Interactive Dashboard    │
└────────────────────────────────────────────────────────────────────────┘


The system operates via three decouple-ready primary execution threads:

The Poller Daemon (poller.py): Collects regional structural updates from the Hong Kong Observatory every 300 seconds.

The Trading Engine (engine.py): Manages real-time Polymarket WebSockets, tracks orderbook momentum, runs predictive scoring runs, and performs mock matching ledger trades.

The Web Dashboard (dashboard.py): Houses a clean multi-threaded visual analytics panel for active exposures on port 8765.

2. Canonical Database Schema

The database has been consolidated into a single unified instance (hko_weather.db) running with high-concurrency configurations. Database optimization constants enforce WAL journaling mode, busy-timeouts of 30,000ms, and standard transactional sync blocks. All writes across concurrent loops are bound behind a localized threading.Lock() block.

Core Weather Schema
SQL
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

Advanced Quantitative Trading Schema
SQL
CREATE TABLE IF NOT EXISTS trigger_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    type TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    target_date TEXT NOT NULL,
    resolution_source TEXT NOT NULL,
    status TEXT DEFAULT 'ACTIVE',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL REFERENCES markets(condition_id),
    outcome_name TEXT NOT NULL,
    temp_min REAL,
    temp_max REAL,
    yes_token_id TEXT NOT NULL,
    UNIQUE(condition_id, outcome_name)
);

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

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    cash_balance REAL DEFAULT 10000.00,
    allocated_margin REAL DEFAULT 0.00
);

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

3. Core Component Analysis
A. Data Poller Module (poller.py)

Executes transactional time-series generation loops. It formats live observations alongside regional history matrices, using bulk_insert_readings mapping mechanisms to eliminate ingestion bottlenecks. It scans standard core forecast targets (HKO, KP, VP1, TMS, SHA), saving daily and sub-hourly atmospheric parameters natively.

B. Statistical Pipeline & Core Modeling Logic (pipeline.py)

Computes implied-to-actual variance arbitrage across discrete outcome blocks.

Prediction Distribution Engine: Evaluates historical accuracy arrays using seasonal metrics (N≥20) via an empirical error module. If historic data point frequency falls short (N<20), it activates an error function Gaussian fallback model:

σ=horizon_std(t)∈[0.5,5.0]⟹Z=
σ
bound−T
hko
	​

	​


Bayesian Price Blending: Re-allocates tail distribution risk dynamically using a horizon-weighted structural framework. Short horizons lean on model accuracy (α
model
	​

=0.75), whereas long-term intervals shift priority onto market aggregation insights (α
model
	​

=0.40).

Arbitrage Sorting Score: Computes individual variance-adjusted target metrics to track mispriced contracts for options entry:

Score=
P
market
	​

(1−P
market
	​

)
(P
market
	​

−P
model
	​

)
2
	​


Sizing Rules: Filters entry with Shannon entropy validation checks (Conviction≥0.3). Position allocations use a conservative quarter-Kelly strategy, capped strictly at a maximum exposure limit of 10% per contract.

C. Automated Trading Engine (engine.py)

Coordinates low-latency, event-driven trading execution loops via an asynchronous framework.

WebSocket Integration: Connects straight to the Polymarket Central Limit Order Book (CLOB) to evaluate depth arrays.

Momentum Trigger Logic: Monitors price deltas across a sliding 10-minute time window. If movements breach a ±$0.02 barrier, it logs structural actions and initiates a contract re-scoring routine.

Atmospheric Modification Matrices: Adjusts baseline forecasts using advanced environmental factors to calculate thermal probability ranges:

High Cloud Covers (>75%): Adjusts temperature expectations downwards by −0.8
∘
C.

High Relative Humidity (>85%): Dampens target thresholds by −0.3
∘
C.

Coastal Wind Channels (East/Southeast winds exceeding 15 km/h): Subtracts −0.5
∘
C for maritime cooling effects.

Execution Boundary Logic: Rejects position entries after 18:00 HKT for same-day maturities to protect capital against late-stage settlement risks.

D. Web UI Dashboard Server (dashboard.py)

Implements a multi-threaded web infrastructure handling real-time charting and performance visibility metrics. It maps out historic temperature tracking, interactive weather graphs, and real-time orderbook displays. The dashboard displays full paper trading ledgers, execution fills, and multi-factor atmospheric modifiers via low-overhead long-polling channels.

4. Current Implementation State & Rectification Log

A deep code verification review has successfully resolved several critical implementation bugs:

Bug Identified	Operational Impact	Resolution Status
trigger_log Table Omission	Sub-hourly tracking writes crashed silently, breaking dashboard updates.	Resolved: Core SQL creation commands are now embedded into canonical db.py initialization arrays.
market_outcomes Missing	Engine analysis routines hit hard termination errors on initial execution.	Resolved: Integrated directly into the main init_db() automated startup pipeline.
Dead Code inside DATABASE_PATH	Overrode active configurations by enforcing a hardcoded internal string variable path.	Resolved: Created a lazy evaluation routing method that correctly checks configuration parameters first.
Dual Database Architecture Conflict	Split active records between two detached models (database.py vs db.py).	Resolved: Decommissioned database.py. Redirected all system scripts to utilize core features inside db.py.
Trading Account Initialization Gap	Mock trades triggered crash states because user ledger tracking profiles were missing.	Resolved: Added automated seed sequences inside init_db() to register standard mock balances instantly.
5. Engineering Plan: Next Steps & System Enhancements

To prepare the system for full live execution, follow this structured engineering guide to clean up legacy dependencies and scale modeling features:

Step 1: Decommission Legacy Ingestion Utilities
Bash
# Purge deprecated files to avoid module routing conflicts
rm hko_weather_monitor/database.py
rm hko_weather_monitor/db_migration.py
rm hko_weather_monitor/db_migration_v2.py

Step 2: Integrate Automated Log Rotation

Create an active configuration module within hko_weather_monitor/main.py using a RotatingFileHandler to prevent unmanaged storage growth from streaming logging entries:

Python
from logging.handlers import RotatingFileHandler

def setup_production_logging(config):
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # Limit files to 25MB, rotating across a 5-file retention array
    file_handler = RotatingFileHandler(
        config.LOG_FILE, maxBytes=26214400, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(log_formatter)
    logging.getLogger().addHandler(file_handler)

Step 3: Implement WebSockets for Real-Time UI Updates

Refactor data communication inside dashboard.py by introducing an asynchronous SimpleWebSocketServer port alongside the HTTP listener, shifting the front-end dashboard away from 15-second polling limits to push execution updates immediately:

Python
# Insert into hko_weather_monitor/dashboard.py to handle immediate updates
async def push_tick_to_ui(websocket, payload_dict):
    await websocket.send(json.dumps(payload_dict))

Step 4: Expand Predictive Engine Testing Scope

Introduce synthetic option variance challenges to increase testing coverage before deploying live capital:

Python
# Add into tests/test_engine_modules.py
def test_kelly_sizing_at_boundary_conditions():
    # Verify risk controls apply hard execution drops when pricing metrics report extreme values
    assert calculate_no_score(market_yes=0.99, model_yes=0.01) > 50.0
