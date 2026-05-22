"""Main entry point — HKO per-minute temperature monitor with adaptive ETag polling."""
import logging
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hko_weather_monitor.db import (
    init_db, bulk_insert_readings, log_scrape,
    bulk_insert_forecasts_hourly, bulk_insert_forecasts_daily,
)
from hko_weather_monitor.fetcher import (
    fetch_all_per_minute,
    parse_timestamp,
    sentinel_changed,
    reset_sentinel,
    fetch_all_forecasts,
    FORECAST_BASE_URL,
)

logger = logging.getLogger(__name__)

# Polling config
FALLBACK_INTERVAL = 600  # 10 min — fallback if adaptive fails
MAX_WAIT = 300           # 5 min max per adaptive cycle before fallback
POLL_INTERVAL = 1        # 1-second ETag checks during adaptive window
FORECAST_POLL_INTERVAL = 3600  # 1 hour for forecasts (OCF updates every ~6h)


def poll_once():
    """Fetch and store per-minute temperature readings for all stations (bulk insert)."""
    start = time.time()
    try:
        rows = fetch_all_per_minute()
        if not rows:
            logger.warning("No data returned")
            return 0

        # Collect all valid readings
        records = []
        for row in rows:
            if row["temperature"] is None:
                continue
            dt = parse_timestamp(row["timestamp"])
            records.append((
                row["station"],
                {
                    "temperature": row["temperature"],
                    "humidity": row["humidity"],
                    "recorded_at": dt.strftime("%Y/%m/%d %H:%M"),
                },
            ))

        # Single bulk insert — one transaction, one lock hold
        stored = bulk_insert_readings(records)

        duration = time.time() - start
        log_scrape("success", f"Stored {stored} per-minute readings", duration)
        logger.info("Poll OK: %d per-minute readings (%.1fs)", stored, duration)
        return stored
    except Exception as e:
        duration = time.time() - start
        log_scrape("error", str(e), duration)
        logger.error("Poll failed: %s", e, exc_info=True)
        return 0


def poll_forecasts():
    """Fetch and store OCF forecast data for all 16 stations."""
    start = time.time()
    try:
        hourly_records, daily_records = fetch_all_forecasts()
        stored_h = 0
        stored_d = 0
        if hourly_records:
            stored_h = bulk_insert_forecasts_hourly(hourly_records)
        if daily_records:
            stored_d = bulk_insert_forecasts_daily(daily_records)
        duration = time.time() - start
        log_scrape("success", f"Stored {stored_h} hourly, {stored_d} daily forecasts", duration)
        logger.info("Forecast poll OK: %d hourly, %d daily (%.1fs)", stored_h, stored_d, duration)
        return stored_h + stored_d
    except Exception as e:
        duration = time.time() - start
        log_scrape("error", f"Forecast poll failed: {e}", duration)
        logger.error("Forecast poll failed: %s", e, exc_info=True)
        return 0


def adaptive_poll():
    """Adaptive ETag polling — 1-second HEAD on sentinel station until change detected.
    
    Strategy:
    - HEAD hko.csv every second (lightweight, no body)
    - When ETag changes → full GET of all 38 stations once
    - Circuit breaker after MAX_WAIT seconds → fallback to 10-min polling
    """
    init_db()
    logger.info("Starting adaptive ETag poller (max_wait=%ds, fallback=%ds)",
                MAX_WAIT, FALLBACK_INTERVAL)

    # Prime the sentinel with current ETag
    reset_sentinel()
    sentinel_changed()  # cache current ETag

    while True:
        cycle_start = time.time()

        # Phase 1: Aggressive 1-second ETag polling
        logger.info("=== Adaptive polling window (up to %ds) ===", MAX_WAIT)
        detected = False
        while time.time() - cycle_start < MAX_WAIT:
            if sentinel_changed():
                logger.info("ETag changed at %s — fetching all stations",
                            time.strftime("%H:%M:%S"))
                count = poll_once()
                reset_sentinel()  # clear cached ETag for next cycle
                sentinel_changed()  # re-cache new ETag
                detected = True
                break
            time.sleep(POLL_INTERVAL)

        if not detected:
            # Phase 2: Circuit breaker — no update in MAX_WAIT, fallback
            logger.warning("No ETag change in %ds — falling back to %ds polling",
                           MAX_WAIT, FALLBACK_INTERVAL)
            poll_once()
            reset_sentinel()
            sentinel_changed()
            time.sleep(FALLBACK_INTERVAL)
        else:
            # Small pause before next adaptive cycle
            time.sleep(5)


def run_forever():
    """Legacy mode — poll every 10 minutes (kept for backward compat)."""
    init_db()
    logger.info("Starting legacy poller (interval=%ds)", FALLBACK_INTERVAL)
    while True:
        poll_once()
        time.sleep(FALLBACK_INTERVAL)


def poll_forecasts_only():
    """Poll forecasts only (for separate forecast service)."""
    init_db()
    logger.info("Starting forecast-only poller (interval=%ds)", FORECAST_POLL_INTERVAL)
    while True:
        poll_forecasts()
        time.sleep(FORECAST_POLL_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    init_db()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "once":
            count = poll_once()
            print(f"Stored {count} per-minute readings")
        elif cmd == "forecast":
            count = poll_forecasts()
            print(f"Stored {count} forecast records")
        elif cmd == "adaptive":
            adaptive_poll()
        elif cmd == "forecasts":
            poll_forecasts_only()
        else:
            print("Usage: python -m hko_weather_monitor.main [once|forecast|adaptive|forecasts]")
    else:
        adaptive_poll()
