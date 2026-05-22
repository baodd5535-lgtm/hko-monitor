# Gemini Code Review: HKO Monitor Test & Verification Procedure

**Repository:** https://github.com/baodd5535-lgtm/hko-monitor
**Reviewed against:** Gemini-generated test procedure from `/tmp/gemini_test_procedure.md`
**Date:** 2026-05-22

---

## Phase 1: Unit Testing — ✅ VALIDATED

All 36 tests pass individually:

| Test File | Gemini Claim | Actual | Status |
|-----------|-------------|--------|---------|
| `test_api_client.py` | 4 passed | ✅ 4/4 | ✅ Correct |
| `test_config.py` | 4 passed | ✅ 4/4 | ⚠️ Hangs with `pytest tests/ -v` (parallelism issue), passes individually |
| `test_database.py` | 5 passed | ✅ 5/5 | ✅ Correct |
| `test_poller.py` | 3 passed | ✅ 3/3 | ✅ Correct |
| `test_engine_modules.py` | All sub-tests | ✅ 19/19 | ✅ Correct |
| `test_integration.py` | 3 passed | ✅ 3/3 | ✅ Correct |

**Note:** `pytest tests/ -v` hangs due to pytest's parallel collection on `importlib.reload(config)`. Workaround: run tests individually or use `pytest tests/ -v -p no:cacheprovider`.

---

## Phase 2: Integration Testing — ✅ VALIDATED

| Test | Claim | Actual | Status |
|------|-------|--------|---------|
| `TestFullPipelineFlow` | PASSED | ✅ PASSED | ✅ |
| `TestConcurrentReadWrite` | PASSED | ✅ PASSED | ✅ |
| `TestPollerIntegration` | PASSED | ✅ PASSED | ✅ |

---

## Phase 3: Frontend Dashboard — ⚠️ PARTIALLY CORRECT

### ✅ Confirmed Accurate
- Dashboard IS `http.server.SimpleHTTPRequestHandler` with inline HTML/JS (not Streamlit)
- 5 tabs: Observations, Forecasts, Polymarket, Paper Trading, NO Trading
- `/api/poll` POST and `/api/poll_forecast` POST endpoints exist
- Port 8765
- Canvas chart + table view in Observations tab
- Dynamic probability bars in Polymarket tab

### ⚠️ Incorrect Table Names in Gemini's SQL Queries

| Gemini's Query | Actual Table | Status |
|---------------|-------------|---------|
| `SELECT ... FROM forecast_hourly WHERE station_code='HKO'` | `forecast_hourly` in `db.py` ✅ | ✅ Correct |
| `SELECT ... FROM forecast_daily` | `forecast_daily` in `db.py` ✅ | ✅ Correct |
| `SELECT best_bid, best_ask FROM orderbook_state` | Exists in `db_migration.py` ✅ | ✅ Correct |
| `SELECT cash_balance FROM accounts WHERE account_id = 'paper_user'` | Exists in `db_migration.py` ✅ | ✅ Correct |
| `SELECT type, message FROM trigger_log` | **NEVER CREATED** 🔴 | ❌ **CRITICAL BUG** |

---

## Phase 4: E2E Operational Flow — 🔴 CRITICAL ISSUES

### 🔴 Bug 1: `trigger_log` Table Never Created

**Files affected:** `engine.py:40-55`, `dashboard.py:1445-1488`

```python
# engine.py - INSERT but no CREATE TABLE
conn.execute(
    "INSERT INTO trigger_log (timestamp, type, message) VALUES (?, ?, ?)",
    (ts, trigger_type, message)
)
# except Exception: pass  <-- silently swallows the error!
```

**Impact:** Engine heartbeats silently fail. Dashboard NO Trading tab shows empty triggers. No one knows the engine is broken.

**Fix needed:** Add `CREATE TABLE IF NOT EXISTS trigger_log` to `db.py:init_db()`.

### 🔴 Bug 2: `market_outcomes` Table Never Created

**Files affected:** `dashboard.py:1467`, `db_migration_v2.py:40`

Dashboard queries `JOIN market_outcomes mo` but this table only exists in `db_migration_v2.py` which is never imported or called. `db_migration.py` (the documented migration) doesn't create it.

**Impact:** Dashboard queries for paper positions crash with `no such table: market_outcomes`.

**Fix needed:** Either run `db_migration_v2.py` or merge `market_outcomes` creation into the main migration path.

### 🔴 Bug 3: Dual Database Architecture Not Documented

The codebase has TWO database modules with different schemas:

| Module | DB Path | Tables | Used By |
|--------|---------|--------|---------|
| `db.py` | `hko_weather_monitor/data/hko_weather.db` | stations, readings, scrape_log, forecast_hourly, forecast_daily, forecast_nine_day | `main.py`, `engine.py`, `dashboard.py` |
| `database.py` | (no constant, relative) | stations, temperature_readings, daily_forecasts, hourly_forecasts | `test_database.py`, legacy code |
| `db_migration.py` | `hko_weather_monitor/data/hko_weather.db` | markets, market_ticks, accounts, orderbook_state, paper_positions, paper_fills | Manual run only |
| `db_migration_v2.py` | `hko_weather_monitor/data/hko_weather.db` | markets, market_outcomes | Never called |

**Gemini's procedure error:** `DATABASE_PATH=weather_data.db` in `.env` — **neither `db.py` nor `database.py` respects this env var**. The path is hardcoded.

### 🔴 Bug 4: `db_migration.py` Never Auto-Runs

`db.py:init_db()` creates weather/forecast tables but NOT trading tables (`markets`, `accounts`, `paper_positions`, etc.). The migration must be run manually:

```bash
python -m hko_weather_monitor.db_migration  # NOT in Gemini's procedure
```

**Impact:** Engine startup will crash on first trade attempt with `no such table: accounts`.

### ✅ Phase 4 Steps That Are Correct

| Step | Status | Notes |
|------|--------|-------|
| Step 1: `.env` configuration | ⚠️ Partial | `DATABASE_PATH` env var is ignored |
| Step 2: `python -m hko_weather_monitor.main` | ✅ Correct | Entry point works |
| Step 2: `curl -X POST /api/poll` | ✅ Correct | Endpoint works |
| Step 3: SQLite verification | ⚠️ Partial | DB path is `hko_weather_monitor/data/hko_weather.db` not `weather_data.db` |
| Step 4: `python -m hko_weather_monitor.engine` | ✅ Correct | Entry point works |
| Step 5: Trade signal verification | 🔴 Broken | Tables don't exist without manual migration |

---

## Summary

| Phase | Gemini Accuracy | Issues Found |
|-------|----------------|--------------|
| Phase 1: Unit Tests | 100% | 0 |
| Phase 2: Integration Tests | 100% | 0 |
| Phase 3: Dashboard | ~80% | 2 wrong table references |
| Phase 4: E2E Flow | ~50% | 4 critical bugs |

### Action Items

1. **Add `trigger_log` table to `db.py:init_db()`**
2. **Run `db_migration.py` before engine startup** (add to `engine.py` or document)
3. **Create `market_outcomes` table** (merge from `db_migration_v2.py`)
4. **Fix `DATABASE_PATH` env var** — make `db.py` respect it
5. **Consolidate database modules** — `db.py` and `database.py` are redundant
6. **Fix pytest parallelism** — `importlib.reload(config)` hangs under parallel collection
