# Gemini Code Review: HKO Monitor Test & Verification Procedure

**Repository:** https://github.com/baodd5535-lgtm/hko-monitor
**Date:** 2026-05-22
**Reviewer:** Hermes Agent (verifying Gemini-generated procedure)

---

## Phase 1: Unit Testing — ✅ ACCURATE

All 36 tests pass:

| Test File | Gemini Claim | Verified |
|-----------|-------------|----------|
| `test_api_client.py` | 4 passed | ✅ 4 passed |
| `test_config.py` | 4 passed | ✅ 4 passed |
| `test_database.py` | 5 passed | ✅ 5 passed |
| `test_poller.py` | 3 passed | ✅ 3 passed |
| `test_engine_modules.py` | All sub-tests passed | ✅ 19 passed |
| `test_integration.py` | 3 passed | ✅ 3 passed |

---

## Phase 2: Integration Testing — ✅ ACCURATE

| Test | Gemini Claim | Verified |
|------|-------------|----------|
| `TestFullPipelineFlow` | PASSED | ✅ PASSED |
| `TestConcurrentReadWrite` | PASSED | ✅ PASSED |
| `TestPollerIntegration` | PASSED | ✅ PASSED |

---

## Phase 3: Frontend Dashboard — ⚠️ PARTIALLY CORRECT

### ✅ Confirmed accurate:
- Dashboard IS `http.server.SimpleHTTPRequestHandler` with inline HTML/JS (line 1267)
- 5 tabs: Observations, Forecasts, Polymarket, Paper Trading, NO Trading (lines 251-255)
- `/api/poll` POST endpoint (line 1723)
- `/api/poll_forecast` POST endpoint (line 1726)
- Port 8765, canvas chart + table view, dynamic probability bars

### ⚠️ SQL queries reference tables that don't exist:
- `SELECT ... FROM trigger_log` — table NEVER created anywhere in codebase
- `SELECT ... FROM forecast_hourly WHERE station_code='HKO'` — ✅ exists in `db.py`
- `SELECT best_bid, best_ask FROM orderbook_state` — ✅ exists in `db_migration.py`
- `SELECT cash_balance FROM accounts` — ✅ exists in `db_migration.py`

---

## Phase 4: E2E Operational Flow — 🔴 CRITICAL ISSUES

### 🔴 Bug 1: `trigger_log` Table Never Created
- **engine.py:49**: `INSERT INTO trigger_log` — silently fails (except: pass)
- **dashboard.py:1445**: `SELECT FROM trigger_log` — wrapped in try/except, falls through
- **No `CREATE TABLE trigger_log` exists anywhere** — heartbeat logging is dead code

### 🔴 Bug 2: `market_outcomes` Table Never Auto-Created
- **dashboard.py:1339,1467,1498**: `JOIN market_outcomes` — crashes if table missing
- **engine.py:160-163**: Checks for table, raises `RuntimeError` if missing
- **db_migration_v2.py:40**: Creates the table but is NEVER called from any startup path

### 🔴 Bug 3: `DATABASE_PATH` Env Var Is Dead Code
- **config.py:21**: `DATABASE_PATH = os.getenv("DATABASE_PATH", "weather_data.db")`
- Nobody imports `config.Config.DATABASE_PATH` in production code
- **db.py:8**: `DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hko_weather.db")` — hardcoded
- Gemini's `.env` with `DATABASE_PATH=weather_data.db` does nothing

### 🔴 Bug 4: Dual Database Architecture (db.py vs database.py)
| Module | DB Path | Tables | Used By |
|--------|---------|--------|---------|
| `db.py` | `hko_weather_monitor/data/hko_weather.db` | stations, readings, scrape_log, forecast_hourly, forecast_daily, forecast_nine_day | main.py, engine.py, dashboard.py |
| `database.py` | (relative path) | stations, temperature_readings, daily_forecasts, hourly_forecasts | test_database.py, test_integration.py, test_poller.py, poller.py |
| `db_migration.py` | `hko_weather_monitor/data/hko_weather.db` | markets, market_ticks, accounts, orderbook_state, paper_positions, paper_fills | Manual run only |
| `db_migration_v2.py` | `hko_weather_monitor/data/hko_weather.db` | markets, market_outcomes | Never called |

### 🔴 Bug 5: Trading Tables Not Auto-Initialized
- `db.py:init_db()` creates weather/forecast tables only
- Trading tables (`markets`, `accounts`, `paper_positions`, etc.) require manual `python -m hko_weather_monitor.db_migration`
- Gemini's E2E flow doesn't mention this — engine crashes on first trade

---

## Summary

| Phase | Accuracy | Issues |
|-------|----------|--------|
| Phase 1: Unit Tests | 100% | 0 |
| Phase 2: Integration Tests | 100% | 0 |
| Phase 3: Dashboard | ~80% | 2 nonexistent table references |
| Phase 4: E2E Flow | ~50% | 4 critical bugs, dead env var, missing migrations |

### Required Fixes
1. Add `trigger_log` table to `db.py:init_db()`
2. Auto-run `db_migration.py` + `db_migration_v2.py` on import
3. Wire `config.DATABASE_PATH` to `db.py` or remove dead code
4. Consolidate `database.py` into `db.py` — eliminate dual-schema confusion
5. Update Gemini's procedure to include migration step
