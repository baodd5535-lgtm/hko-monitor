# DB Consolidation Plan — HKO Monitor

**Date:** 2026-05-22
**Goal:** Eliminate dual-schema confusion, fix missing tables, auto-initialize all DB tables

---

## Problem

| Module | DB Path | Tables | Used By |
|--------|---------|--------|---------|
| `db.py` ✅ CANONICAL | `hko_weather_monitor/data/hko_weather.db` | stations, readings, scrape_log, forecast_hourly, forecast_daily, forecast_nine_day | main.py, engine.py, dashboard.py, backtester.py, pipeline.py |
| `database.py` ❌ LEGACY | relative path (WeatherDatabase class) | stations(station_id,code,name), temperature_readings, daily_forecasts, hourly_forecasts | poller.py, test_database.py, test_integration.py, test_poller.py |
| `db_migration.py` ❌ MANUAL | `hko_weather_monitor/data/hko_weather.db` | markets, market_ticks, accounts, orderbook_state, paper_positions, paper_fills | Never auto-run |
| `db_migration_v2.py` ❌ MANUAL | `hko_weather_monitor/data/hko_weather.db` | markets (v2), market_outcomes | Never auto-run |

### Critical Bugs
1. `trigger_log` table NEVER created — engine.py/dashboard.py INSERTs crash silently
2. `market_outcomes` table NEVER created — engine.py raises RuntimeError
3. `config.DATABASE_PATH` dead code except poller.py
4. Trading tables require manual migration run

---

## Solution

### Files to Modify
1. **`db.py`** — Add ALL missing tables to `init_db()`: trigger_log, markets, market_outcomes, market_ticks, accounts, orderbook_state, paper_positions, paper_fills. Wire `config.DATABASE_PATH` env var.
2. **`config.py`** — Keep DATABASE_PATH but make db.py use it
3. **`poller.py`** — Switch from `database.WeatherDatabase` to `db.py` functions
4. **`test_database.py`** — Rewrite to test `db.py` functions directly
5. **`test_integration.py`** — Switch from `database.WeatherDatabase` to `db.py`
6. **`test_poller.py`** — Switch from `database.WeatherDatabase` to `db.py`

### Files to Delete
1. `database.py` — legacy, different schema
2. `db_migration.py` — tables merged into db.py
3. `db_migration_v2.py` — market_outcomes merged into db.py

---

## Execution Order

1. Patch `db.py` — add all missing tables + env var support
2. Patch `config.py` — wire DATABASE_PATH to db.py
3. Patch `poller.py` — switch to db.py
4. Patch `test_database.py` — rewrite to test db.py
5. Patch `test_integration.py` — switch to db.py
6. Patch `test_poller.py` — switch to db.py
7. Delete `database.py`, `db_migration.py`, `db_migration_v2.py`
8. Run tests
9. Push to GitHub
10. Re-index codegraph & agentmemory
