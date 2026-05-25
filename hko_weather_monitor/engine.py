"""
Hybrid trading engine: event-driven + scheduled re-scoring.

Architecture:
  - Persistent WebSocket to Polymarket CLOB (real-time orderbook tracking)
  - 30-min HKO forecast sync (multi-factor: temp, humidity, wind, rain, clouds)
  - Three triggers:
    1. Orderbook momentum > threshold (price delta over 10-min window)
    2. 30-min heartbeat (re-score everything, check risk)
    3. HKO forecast update detected (Last-Modified or value change)

  Each trigger calls the scoring engine from pipeline.py, which evaluates
  variance-adjusted edge for all buckets, picks the single best NO trade,
  and executes if conditions are met.
"""
import asyncio
import json
import time
import sqlite3
import logging
import re
from datetime import date, datetime
from typing import Dict, Optional, List

from hko_weather_monitor.orderbook_manager import PolymarketOrderbookManager
from hko_weather_monitor.execution_engine import PaperExecutionEngine
from hko_weather_monitor.db import DB_PATH, get_connection
from hko_weather_monitor.polymarket import fetch_active_hk_polymarket
from hko_weather_monitor.temporal_tracker import TemporalTracker
from hko_weather_monitor.factors import WeatherMultiFactorScorer
from hko_weather_monitor.fetcher import (
    fetch_all_forecasts, check_last_modified, FORECAST_BASE_URL,
    WEATHER_CODES, FORECAST_STATIONS,
)

logger = logging.getLogger(__name__)

# Global trigger logger - writes to DB for dashboard
def log_trigger(trigger_type: str, message: str):
    """Log a trigger event to the trigger_log table and console."""
    import time
    ts = time.time()
    logger.info(f"[TRIGGER:{trigger_type}] {message}")
    conn = None
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO trigger_log (timestamp, type, message) VALUES (?, ?, ?)",
            (ts, trigger_type, message)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()


def _weather_code_to_cloud_coverage(code: int) -> float:
    """Map HKO weather code to approximate cloud coverage percentage."""
    mapping = {
        0: 0.0,    # Clear
        1: 15.0,   # Mostly Clear
        2: 65.0,   # Mostly Cloudy
        3: 85.0,   # Cloudy
        50: 30.0,  # Sunny Intervals
        51: 10.0,  # Sunny
        52: 30.0,  # Sunny Intervals
        53: 10.0,  # Sunny
        54: 25.0,  # Mostly Sunny
        60: 75.0,  # Cloudy
        61: 65.0,  # Mostly Cloudy
        62: 90.0,  # Light Rain
        63: 95.0,  # Overcast Showers
        64: 90.0,  # Showers
        71: 95.0,  # Moderate Rain
        72: 100.0, # Heavy Rain
        73: 100.0, # Thunderstorm
        74: 100.0, # Thunderstorm & Heavy Rain
        76: 100.0, # Severe Thunderstorm
        81: 50.0,  # Haze
        82: 40.0,  # Smog
        83: 45.0,  # Mist
    }
    return mapping.get(code, 50.0)


def _get_date_from_condition_id(condition_id: str) -> Optional[str]:
    """Extract YYYY-MM-DD from condition_id like hk_temp_2026-05-21T12:00:00Z."""
    match = re.search(r'(\d{4}-\d{2}-\d{2})', condition_id)
    return match.group(1) if match else None


def _get_date_hko_format(date_iso: str) -> str:
    """Convert '2026-05-21' to '20260521' for DB queries."""
    return date_iso.replace('-', '')


class WeatherTradingEngine:
    """
    Long-running hybrid engine that coordinates:
    - Real-time orderbook monitoring via WebSocket
    - Periodic HKO forecast sync with multi-factor data
    - Temporal tracking of price momentum and forecast changes
    - Event-driven re-scoring and trade execution
    """

    def __init__(self):
        self.tracker = TemporalTracker(
            orderbook_window_sec=600,    # 10-min momentum window
            forecast_window_slots=12,     # ~12 forecast updates
        )
        self.scorer = WeatherMultiFactorScorer()

        # Triggers
        self.price_momentum_threshold = 0.02  # 2-cent change triggers re-score
        self.hko_sync_interval = 1800         # 30 minutes
        self.heartbeat_interval = 1800        # 30 minutes

        # State
        self.running = False
        self.last_hko_sync = 0.0
        self.last_heartbeat = 0.0
        self.last_rescore = 0.0
        self.rescore_cooldown = 120          # Don't re-score more than once per 2 min

        # HKO state
        self.hko_forecasts = {}              # date_iso -> {max_temp, humidity, ...}
        self.last_modified_cache = {}        # station_code -> Last-Modified header

        # Components (lazy-init)
        self.book_manager: Optional[PolymarketOrderbookManager] = None
        self.execution_engine: Optional[PaperExecutionEngine] = None
        self.active_conditions: List[str] = []

    async def start(self):
        """Start the hybrid engine."""
        import os
        self.running = True
        import asyncio

        # Write PID file for dashboard engine status tracking
        try:
            with open('/tmp/hko_engine.pid', 'w') as f:
                f.write(str(os.getpid()))
            logger.info(f"Engine PID file written: {os.getpid()}")
        except Exception as e:
            logger.error(f"Failed to write PID file: {e}")

        logger.info("=" * 60)
        logger.info("Starting hybrid weather trading engine...")
        logger.info(f"  Momentum threshold: {self.price_momentum_threshold}")
        logger.info(f"  HKO sync interval: {self.hko_sync_interval}s")
        logger.info(f"  Heartbeat interval: {self.heartbeat_interval}s")
        logger.info(f"  Re-score cooldown: {self.rescore_cooldown}s")
        logger.info("=" * 60)

        # Load active conditions — filter out resolved & expired
        from datetime import datetime, timezone, timedelta
        HKT = timezone(timedelta(hours=8))

        # Verify required tables exist
        conn = get_connection()
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if 'market_outcomes' not in tables:
                logger.error("market_outcomes table missing — run seed_markets.py first")
                raise RuntimeError("Missing market_outcomes table")

            all_conditions = conn.execute("""
                SELECT DISTINCT mo.condition_id, m.status
                FROM market_outcomes mo
                LEFT JOIN markets m ON mo.condition_id = m.condition_id
            """).fetchall()
        finally:
            conn.close()

        self.active_conditions = []
        now_utc = datetime.now(timezone.utc)
        now_hkt = datetime.now(HKT)
        today_hkt = now_hkt.date()

        for (condition_id, market_status) in all_conditions:
            if market_status == 'RESOLVED':
                logger.info(f"Skipping resolved market: {condition_id}")
                continue
            target_str = condition_id.split('_')[-1]
            try:
                raw_date = datetime.fromisoformat(target_str.replace('Z', '+00:00'))
                resolution_hkt = raw_date.replace(hour=23, minute=59, tzinfo=HKT)
                resolution_utc = resolution_hkt.astimezone(timezone.utc)
            except Exception:
                continue
            hours_left = (resolution_utc - now_utc).total_seconds() / 3600
            if hours_left <= 1:
                logger.info(f"Skipping expired market: {condition_id} ({hours_left:.1f}h left)")
                continue
            # Trading cutoff: 18:00 HKT for same-day markets
            if resolution_hkt.date() == today_hkt and now_hkt.hour >= 18:
                logger.info(f"Skipping past cutoff: {condition_id} ({now_hkt.strftime('%H:%M')} HKT)")
                continue
            self.active_conditions.append(condition_id)

        # Initial HKO sync
        await self.sync_hko_forecasts()

        # Initial scoring
        await self.evaluate_and_execute_trades()

        # Run both coroutines concurrently
        try:
            await asyncio.gather(
                self._run_websocket_loop(),
                self._run_periodic_heartbeat(),
            )
        except asyncio.CancelledError:
            logger.info("Engine cancelled")
        finally:
            self.running = False
            if self.book_manager:
                self.book_manager.disconnect()
            # Clean up PID file on exit
            import os
            try:
                if os.path.exists('/tmp/hko_engine.pid'):
                    os.remove('/tmp/hko_engine.pid')
            except Exception:
                pass
            logger.info("Engine stopped")

    async def stop(self):
        """Gracefully stop the engine."""
        self.running = False

    # ─── WebSocket Orderbook Loop ───────────────────────────────

    async def _run_websocket_loop(self):
        """
        Persistent WebSocket connection to Polymarket CLOB.
        Hooks into book_manager's listener via callback — no separate listener needed.
        """
        while self.running:
            try:
                logger.info("Connecting WebSocket to Polymarket CLOB...")
                book = PolymarketOrderbookManager()
                
                # Register callback for real-time price tracking
                book.on_price_update = self._on_price_update
                
                self.book_manager = book
                self.execution_engine = PaperExecutionEngine(DB_PATH, book)

                # Connect to ALL active conditions
                for condition_id in self.active_conditions:
                    try:
                        await book.connect(condition_id)
                        logger.info(f"  WebSocket connected: {condition_id}")
                    except Exception as e:
                        logger.error(f"  Failed to connect {condition_id}: {e}")

                # Wait for initial snapshots
                await asyncio.sleep(5)

                # Write engine heartbeat to DB
                log_trigger('heartbeat', 'Engine started, WebSocket connected')
                self._update_engine_timestamp()

                # Keep alive — book_manager handles all messages via callback
                while self.running:
                    await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"WebSocket loop error: {e}")
                if self.running:
                    logger.info("Reconnecting in 10s...")
                    await asyncio.sleep(10)

    def _on_price_update(self, token_id: str, best_bid: float, best_ask: float):
        """Called by book_manager on every price update. Tracks temporal changes."""
        if best_bid is not None and best_ask is not None:
            self.tracker.record_orderbook(token_id, best_bid, best_ask)
            momentum = self.tracker.get_orderbook_momentum(token_id)
            if abs(momentum) >= self.price_momentum_threshold:
                now = time.time()
                # Only log trigger if outside the re-score cooldown window (prevents DB flooding)
                if now - self.last_rescore >= self.rescore_cooldown:
                    logger.info(f"[MOMENTUM] {token_id[:20]}...: price delta = {momentum:.4f}")
                    log_trigger('momentum', f"Token {token_id[:20]}... delta={momentum:.4f}")
                asyncio.ensure_future(self._trigger_rescore(f"momentum:{token_id[:20]}"))

    # ─── HKO Forecast Sync ──────────────────────────────────────

    async def sync_hko_forecasts(self):
        """
        Fetch HKO forecasts, check for changes, record temporal state.
        Uses both OCF hourly/daily forecasts and 9-day forecast.
        """
        import requests

        # 1. Check if HKO data changed (Last-Modified headers)
        hko_changed = False
        for code in FORECAST_STATIONS[:4]:  # Check key stations
            url = FORECAST_BASE_URL.format(station=code)
            lm = check_last_modified(url)
            if lm and lm != self.last_modified_cache.get(code):
                hko_changed = True
                self.last_modified_cache[code] = lm

        # 2. Fetch all forecasts
        try:
            hourly_records, daily_records = fetch_all_forecasts()
        except Exception as e:
            logger.error(f"Failed to fetch HKO forecasts: {e}")
            return

        # 3. Parse and store multi-factor data per target date
        # For each daily forecast record, extract:
        # (station_code, forecast_date, max_temp, min_temp, rain_prob, weather_code, ...)
        for record in daily_records:
            station_code = record[0]
            forecast_date = record[1]  # YYYYMMDD
            max_temp = record[2]
            min_temp = record[3]
            rain_prob = record[4]
            weather_code = record[5]

            if not forecast_date or max_temp is None:
                continue

            # Convert to ISO format for our tracker
            date_iso = f"{forecast_date[:4]}-{forecast_date[4:6]}-{forecast_date[6:8]}"

            # Get cloud coverage from weather code
            cloud_coverage = _weather_code_to_cloud_coverage(weather_code)

            # Store the most recent forecast per date (prefer HKO station)
            if date_iso not in self.hko_forecasts or station_code == 'HKO':
                self.hko_forecasts[date_iso] = {
                    'max_temp': max_temp,
                    'min_temp': min_temp,
                    'rain_probability': rain_prob,
                    'weather_code': weather_code,
                    'cloud_coverage': cloud_coverage,
                    'station': station_code,
                }

        # 4. Also fetch hourly data for wind/humidity (current conditions)
        for record in hourly_records:
            station = record[0]
            forecast_hour = record[1]  # YYYYMMDDHHmm
            if not forecast_hour:
                continue

            date_iso = (
                f"{forecast_hour[:4]}-{forecast_hour[4:6]}-{forecast_hour[6:8]}"
            )
            temp = record[2]
            humidity = record[3]
            wind_speed = record[4]
            wind_dir = record[5]

            if date_iso in self.hko_forecasts:
                # Enrich with hourly wind/humidity if not present
                if humidity is not None:
                    self.hko_forecasts[date_iso]['humidity'] = humidity
                if wind_speed is not None:
                    self.hko_forecasts[date_iso]['wind_speed'] = wind_speed
                if wind_dir is not None:
                    self.hko_forecasts[date_iso]['wind_direction'] = wind_dir

        # 5. Also fetch 9-day forecast for extended horizon
        try:
            from hko_weather_monitor.fetcher import fetch_nine_day_forecast
            from hko_weather_monitor.db import bulk_insert_nine_day_forecast
            nine_day = fetch_nine_day_forecast()
            if nine_day:
                bulk_insert_nine_day_forecast(nine_day)
            for entry in nine_day:
                # Parse date from entry
                date_iso = entry.get('forecast_date', '')[:10]  # First 10 chars
                if date_iso:
                    self.hko_forecasts[date_iso] = {
                        'max_temp': entry.get('max_temp'),
                        'min_temp': entry.get('min_temp'),
                        'rain_probability': self._parse_rain_prob(entry.get('rain_prob', '')),
                        'weather_desc': entry.get('weather_desc', ''),
                        'wind_info': entry.get('wind_info', ''),
                        'cloud_coverage': 50.0,  # Default
                    }
        except Exception as e:
            logger.debug(f"9-day forecast fetch failed: {e}")

        # 6. Record in temporal tracker for each active condition
        for condition_id in self.active_conditions:
            date_iso = _get_date_from_condition_id(condition_id)
            if not date_iso:
                continue

            forecast = self.hko_forecasts.get(date_iso)
            if forecast:
                max_temp = forecast.get('max_temp', 0)
                rain_prob = forecast.get('rain_probability', 0)
                if max_temp is not None:
                    self.tracker.record_hko_forecast(
                        date_iso,
                        float(max_temp),
                        self._parse_rain_prob(rain_prob),
                    )

        self.last_hko_sync = time.time()

        if hko_changed:
            logger.info("[HKO] Forecast data changed — triggering re-score")
            await self._trigger_rescore("hko_changed")
        else:
            logger.info(f"[HKO] Synced forecasts for {len(self.hko_forecasts)} dates")

    def _parse_rain_prob(self, val) -> float:
        """Parse rain probability string/number to float (0-100)."""
        if val is None:
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        val_str = str(val).strip()
        if not val_str or val_str in ('0', 'N/A', '-'):
            return 0.0
        # Handle "70-80%" format
        match = re.search(r'(\d+)', val_str)
        return float(match.group(1)) if match else 0.0

    def _get_hko_multi_factor_data(self, date_iso: str) -> Dict:
        """
        Get multi-factor weather data for a date, enriched with adjustments.
        Returns dict ready for WeatherMultiFactorScorer.
        """
        forecast = self.hko_forecasts.get(date_iso, {})
        if not forecast:
            return {}

        base_temp = forecast.get('max_temp')
        if base_temp is None:
            return {}

        return {
            'humidity': forecast.get('humidity', 75.0),
            'cloud_coverage': forecast.get('cloud_coverage', 50.0),
            'wind_speed': forecast.get('wind_speed', 10.0),
            'wind_direction': forecast.get('wind_direction', 'E'),
            'rain_probability': self._parse_rain_prob(forecast.get('rain_probability', 0)),
            'weather_code': forecast.get('weather_code'),
            'base_temp': base_temp,
        }

    def get_adjusted_hko_temp(self, date_iso: str) -> Optional[float]:
        """
        Get HKO max temperature adjusted by multi-factor analysis.
        Returns adjusted temperature or None if no data.
        """
        data = self._get_hko_multi_factor_data(date_iso)
        if not data:
            return None

        base_temp = data.pop('base_temp')
        return self.scorer.calculate_adjusted_temperature_probability(
            float(base_temp), data
        )

    # ─── Periodic Heartbeat ─────────────────────────────────────

    async def _run_periodic_heartbeat(self):
        """30-min heartbeat: sync HKO, check risk, re-score."""
        while self.running:
            await asyncio.sleep(60)  # Tick every minute

            now = time.time()

            # HKO sync every 30 min
            if now - self.last_hko_sync >= self.hko_sync_interval:
                logger.info("[HEARTBEAT] Syncing HKO forecasts...")
                await self.sync_hko_forecasts()

            # Full re-score every 30 min
            if now - self.last_heartbeat >= self.heartbeat_interval:
                logger.info("[HEARTBEAT] Running periodic re-score...")
                await self.evaluate_and_execute_trades()
                self.last_heartbeat = now

            # Risk management: check PnL exposure
            await self._check_risk_management()

    async def _check_risk_management(self):
        """Check portfolio risk: unrealized losses, total exposure."""
        conn = get_connection()
        try:
            balance = conn.execute(
                "SELECT cash_balance FROM accounts WHERE account_id = 'paper_user'"
            ).fetchone()
            if not balance:
                return
            balance = balance[0]

            # Count open positions
            positions = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN qty > 0 THEN qty ELSE -qty END), 0) FROM paper_positions WHERE qty != 0"
            ).fetchone()

            num_positions, total_shares = positions
            if num_positions > 0:
                avg_exposure = (total_shares / num_positions) / balance * 100
                logger.debug(
                    f"[RISK] {num_positions} positions, "
                    f"avg exposure: {avg_exposure:.1f}% of balance"
                )
        except Exception as e:
            logger.debug(f"Risk check failed: {e}")
        finally:
            conn.close()

    async def _trigger_rescore(self, reason: str):
        """Debounce-protected re-score trigger."""
        now = time.time()
        if now - self.last_rescore < self.rescore_cooldown:
            logger.debug(
                f"[RESCORE] Skipping ({reason}) — cooldown "
                f"{self.rescore_cooldown - (now - self.last_rescore):.0f}s remaining"
            )
            return

        logger.info(f"[RESCORE] Triggered by: {reason}")
        self.last_rescore = now
        await self.evaluate_and_execute_trades()

    # ─── Scoring Engine (reuses pipeline.py logic) ──────────────

    async def evaluate_and_execute_trades(self):
        """
        Full scoring pass: for each market, calculate probabilities,
        find best NO trade, execute if conditions met.
        Reuses pipeline.py scoring logic with multi-factor adjusted temperatures.
        """
        from hko_weather_monitor.pipeline import (
            calculate_bucket_probability,
            calculate_conviction,
            calculate_no_score,
            parse_bucket_temp,
            get_edge_threshold,
            CONVICTION_MIN,
            MAX_PORTFOLIO_EXPOSURE_PCT,
        )

        if not self.book_manager:
            logger.warning("No orderbook connection — skipping scoring")
            return

        for condition_id in self.active_conditions:
            try:
                date_iso = _get_date_from_condition_id(condition_id)
                if not date_iso:
                    continue

                date_hko = _get_date_hko_format(date_iso)

                # Get multi-factor adjusted temperature
                adjusted_temp = self.get_adjusted_hko_temp(date_iso)
                if adjusted_temp is None:
                    # Fallback: get raw from DB
                    from hko_weather_monitor.pipeline import get_hko_forecast_for_date
                    adjusted_temp = get_hko_forecast_for_date(date_hko)

                if adjusted_temp is None:
                    logger.info(f"[{condition_id}] No HKO forecast, skipping")
                    continue

                # Calculate horizon — skip near-resolved markets (<1hr left)
                from datetime import datetime, timezone, timedelta
                HKT = timezone(timedelta(hours=8))
                now_utc = datetime.now(timezone.utc)
                raw_date = datetime.fromisoformat(condition_id.split('_')[-1].replace('Z', '+00:00'))
                # Resolution is 23:59 HKT, not 12:00 UTC
                resolution_hkt = raw_date.replace(hour=23, minute=59, tzinfo=HKT)
                resolution_utc = resolution_hkt.astimezone(timezone.utc)
                hours_left = (resolution_utc - now_utc).total_seconds() / 3600
                if hours_left <= 1:
                    logger.info(f"[{condition_id}] Near resolution ({hours_left:.1f}h), skipping")
                    continue

                # Trading cutoff: peak temp 15-17 HKT, skip if resolves today and past 18:00 HKT
                now_hkt = datetime.now(HKT)
                if resolution_hkt.date() == now_hkt.date() and now_hkt.hour >= 18:
                    logger.info(f"[{condition_id}] Resolves today but past 18:00 HKT cutoff ({now_hkt.strftime('%H:%M')}), skipping")
                    continue

                horizon = max(0, (raw_date.astimezone().date() - datetime.now().date()).days)
                threshold = get_edge_threshold(horizon)

                # Get temporal context
                forecast_delta = self.tracker.get_forecast_delta(date_iso)

                logger.info(
                    f"[{condition_id}] Scoring: adjusted_temp={adjusted_temp:.1f}°C, "
                    f"horizon={horizon}d, threshold={threshold:.3f}, "
                    f"forecast_delta={forecast_delta}"
                )

                # Get all outcomes
                conn = get_connection()
                try:
                    outcomes = conn.execute(
                        "SELECT yes_token_id, outcome_name, temp_min, temp_max "
                        "FROM market_outcomes WHERE condition_id = ?",
                        (condition_id,)
                    ).fetchall()
                finally:
                    conn.close()

                # Score all buckets
                scored_buckets = []
                all_probs = []
                all_market_prices = []

                for outcome in outcomes:
                    token_id = outcome[0]
                    outcome_name = outcome[1]
                    bucket_temp = parse_bucket_temp(outcome_name)
                    if bucket_temp is None:
                        continue

                    # Use adjusted temperature for probability calculation
                    model_prob = calculate_bucket_probability(
                        adjusted_temp, bucket_temp, horizon
                    )
                    all_probs.append(model_prob)

                    bids, asks = self.book_manager.get_snapshot(token_id)
                    if not bids or not asks:
                        all_market_prices.append(0.0)  # No liquidity
                        continue

                    best_bid = bids[0][0]
                    best_ask = asks[0][0]
                    market_yes = (best_bid + best_ask) / 2.0
                    all_market_prices.append(market_yes)

                    # Track orderbook price
                    self.tracker.record_orderbook(token_id, best_bid, best_ask)

                    no_score = calculate_no_score(market_yes, model_prob)

                    # Factor in orderbook momentum
                    momentum = self.tracker.get_orderbook_momentum(token_id)
                    # If YES price is dropping (negative momentum), NO thesis strengthens
                    adjusted_no_score = no_score
                    if momentum < 0:
                        # Price dropping = our NO position gaining value
                        adjusted_no_score *= (1.0 + abs(momentum) * 5)
                    elif momentum > 0:
                        # Price rising = market disagrees with our NO
                        adjusted_no_score *= max(0.5, 1.0 - momentum * 2)

                    scored_buckets.append({
                        'token_id': token_id,
                        'outcome_name': outcome_name,
                        'model_prob': model_prob,
                        'market_yes': market_yes,
                        'no_score': adjusted_no_score,
                        'momentum': momentum,
                    })

                if not scored_buckets:
                    continue

                # Bayesian blend with market prices
                from hko_weather_monitor.pipeline import bayesian_blend_probs
                blended_probs = bayesian_blend_probs(all_probs, all_market_prices, horizon)

                # Sort by adjusted NO score
                scored_buckets.sort(key=lambda x: x['no_score'], reverse=True)

                # Conviction check on blended distribution
                conviction = calculate_conviction(blended_probs) if blended_probs else 0.0
                if conviction < CONVICTION_MIN:
                    logger.info(
                        f"[{condition_id}] Conviction {conviction:.3f} < {CONVICTION_MIN}"
                    )
                    continue

                # Best candidate
                best = scored_buckets[0]
                edge = best['market_yes'] - best['model_prob']

                if best['no_score'] <= 0 or edge < threshold:
                    logger.info(
                        f"[{condition_id}] No strong NO candidate "
                        f"(score={best['no_score']:.4f}, edge={edge:.4f})"
                    )
                    continue

                # Check if we already have a NO position on this condition
                conn = get_connection()
                try:
                    existing = conn.execute(
                        "SELECT COUNT(*) FROM paper_positions "
                        "WHERE condition_id = ? AND side = 'NO' AND status = 'OPEN'",
                        (condition_id,)
                    ).fetchone()[0]
                finally:
                    conn.close()

                if existing > 0:
                    logger.info(
                        f"[{condition_id}] Already have NO position ({existing}) — skipping"
                    )
                    continue

                logger.info(
                    f"[{condition_id}] BEST NO: {best['outcome_name']} | "
                    f"Model: {best['model_prob']:.4f} | "
                    f"Market YES: {best['market_yes']:.4f} | "
                    f"Edge: {edge:.4f} | Score: {best['no_score']:.4f} | "
                    f"Momentum: {best['momentum']:.4f} | "
                    f"Conviction: {conviction:.3f}"
                )

                # Kelly sizing
                kelly_frac = max(0, 0.25 * edge / (1.0 - best['market_yes']))
                conn = get_connection()
                try:
                    balance = conn.execute(
                        "SELECT cash_balance FROM accounts "
                        "WHERE account_id = 'paper_user'"
                    ).fetchone()[0]
                finally:
                    conn.close()

                position_size = min(
                    balance * kelly_frac,
                    balance * 0.10,  # Hard cap 10%
                )

                if position_size < 10:
                    logger.info(f"[{condition_id}] Position too small (${position_size:.0f})")
                    continue

                # Record tick
                tick_conn = get_connection()
                try:
                    tick_conn.execute(
                        "INSERT INTO market_ticks "
                        "(condition_id, polymarket_yes_price, hko_predicted_value, "
                        "hko_forecast_horizon_days, model_calculated_prob, generated_signal) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (condition_id, best['market_yes'], adjusted_temp,
                         horizon, best['model_prob'], "SELL")
                    )
                    tick_conn.commit()
                finally:
                    tick_conn.close()

                # Execute
                fill = self.execution_engine.execute_paper_sell(
                    'paper_user', condition_id, best['token_id'],
                    position_size, best['market_yes']
                )

                if fill['status'] == 'FILLED':
                    logger.info(
                        f"EXECUTED SELL: {best['outcome_name']} | "
                        f"${position_size:.0f} | "
                        f"{fill['qty']:.2f} @ {fill['avg_price']:.4f}"
                    )
                else:
                    logger.info(
                        f"REJECTED SELL: {best['outcome_name']} | "
                        f"{fill.get('reason', 'unknown')}"
                    )

            except Exception as e:
                logger.error(f"Error scoring {condition_id}: {e}")
                import traceback
                traceback.print_exc()

    def _update_engine_timestamp(self):
        """Update engine timestamp in DB for dashboard."""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO engine_status (key, value) VALUES ('last_heartbeat', ?)",
                (str(time.time()),)
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    def get_status(self) -> Dict:
        """Get engine status for dashboard/API."""
        return {
            'running': self.running,
            'active_conditions': len(self.active_conditions),
            'last_hko_sync': self.last_hko_sync,
            'last_heartbeat': self.last_heartbeat,
            'last_rescore': self.last_rescore,
            'tracked_dates': len(self.hko_forecasts),
            'orderbook_tokens_tracked': len(self.tracker.orderbook_history),
            'forecast_records': {
                k: len(v) for k, v in self.tracker.forecast_history.items()
            },
        }


async def run_engine():
    """Entry point: start the engine and run until interrupted."""
    engine = WeatherTradingEngine()

    try:
        await engine.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down")
        await engine.stop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )
    asyncio.run(run_engine())
