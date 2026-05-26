"""
End-to-end trading pipeline - connects HKO forecast analysis with Polymarket orderbook.
Empirical error distribution model with seasonal filtering and multi-station ensemble.
"""
import asyncio
import json
import sqlite3
import math
import time
import logging
import re
from datetime import datetime, date

from hko_weather_monitor.orderbook_manager import PolymarketOrderbookManager
from hko_weather_monitor.execution_engine import PaperExecutionEngine
from hko_weather_monitor.polymarket import fetch_active_hk_polymarket
from hko_weather_monitor.db import DB_PATH, get_connection
from hko_weather_monitor.empirical_model import (
    load_and_clean_data,
    calculate_empirical_probability,
    market_awareness_check,
)
from hko_weather_monitor.station_correlations import (
    compute_station_correlations,
    get_ensemble,
)

logger = logging.getLogger(__name__)

# Maker params
MAKER_SPREAD = 0.02          # 2-cent offset from fair value
MAKER_MAX_POSITION = 500     # Max $500 per maker bucket
MAKER_MIN_PROB = 0.05        # Skip buckets with <5% model probability

# Cache historical errors globally
_error_df = None


def _get_error_df():
    """Load or return cached historical forecast errors."""
    global _error_df
    if _error_df is None:
        _error_df = load_and_clean_data()
        logger.info(f"Loaded {len(_error_df)} historical forecast-actual pairs for empirical model")
    return _error_df


def calculate_bucket_probability(hko_max_temp, bucket_temp, horizon_days, target_date=None):
    """
    Calculate bucket probability using empirical error distribution.

    Falls back to Gaussian if not enough historical data.
    """
    if hko_max_temp is None:
        return 0.5

    # Bucket boundaries
    bucket_low = bucket_temp - 0.5
    bucket_high = bucket_temp + 0.5
    if bucket_temp <= 22:
        bucket_low = -100.0
    elif bucket_temp >= 31:
        bucket_high = 100.0

    if target_date is None:
        target_date = date.today()
    if isinstance(target_date, date):
        target_date = target_date.strftime("%Y-%m-%d")

    df = _get_error_df()
    if len(df) < 20:
        logger.warning("Not enough historical data for empirical model, using Gaussian fallback")
        return _gaussian_fallback(hko_max_temp, bucket_temp, horizon_days)

    prob = calculate_empirical_probability(
        df, target_date, hko_max_temp, horizon_days,
        bucket_low, bucket_high,
    )
    return prob


def _gaussian_fallback(hko_max_temp, bucket_temp, horizon_days):
    """Original Gaussian model — fallback when empirical data is insufficient."""
    horizon_std = {
        0: 0.5, 1: 1.0, 2: 1.5, 3: 2.0,
        4: 2.5, 5: 3.0, 6: 3.5, 7: 4.0, 8: 4.5, 9: 5.0,
    }
    sigma = horizon_std.get(max(0, min(horizon_days, 9)), 5.0)
    bucket_low = bucket_temp - 0.5
    bucket_high = bucket_temp + 0.5
    if bucket_temp <= 22:
        bucket_low = -100.0
    elif bucket_temp >= 31:
        bucket_high = 100.0
    z_low = (bucket_low - hko_max_temp) / sigma
    z_high = (bucket_high - hko_max_temp) / sigma
    return max(0.001, min(0.999, 0.5 * (1 + math.erf(z_high / math.sqrt(2))) - 0.5 * (1 + math.erf(z_low / math.sqrt(2)))))


def get_edge_threshold(horizon_days):
    """Minimum edge threshold for NO trades. Higher for farther horizons."""
    thresholds = {
        0: 0.02, 1: 0.025, 2: 0.03, 3: 0.04,
        4: 0.05, 5: 0.06, 6: 0.08, 7: 0.10,
        8: 0.15, 9: 0.20,
    }
    return thresholds.get(max(0, min(horizon_days, 9)), 0.05)


def calculate_conviction(probs):
    """Shannon entropy-based conviction score (0-1).
    1.0 = single bucket dominates, 0.0 = uniform distribution.
    """
    if not probs or not any(p > 0 for p in probs):
        return 0.0
    total = sum(probs)
    probs = [p / total for p in probs]
    entropy = -sum(p * math.log2(p) for p in probs if p > 1e-9)
    max_entropy = math.log2(len(probs)) if len(probs) > 1 else 1.0
    return max(0.0, 1.0 - (entropy / max_entropy))


def calculate_no_score(market_yes, model_yes):
    """Variance-Adjusted Edge: single best metric to rank buckets for NO trades.
    Score = (market_YES - model_YES)^2 / (market_YES * (1 - market_YES))
    Higher = better NO trade.
    """
    edge = market_yes - model_yes
    if edge <= 0:
        return 0.0
    variance = market_yes * (1.0 - market_yes)
    if variance <= 1e-9:
        return 0.0
    return (edge ** 2) / variance


def bayesian_blend_probs(
    model_probs: list, 
    market_prices: list, 
    horizon_days: int
) -> list:
    """Dynamic Bayesian blending of model and market probabilities.
    
    Uses horizon-weighted alpha: shorter horizons trust model more,
    longer horizons trust market more.
    
    Args:
        model_probs: Model-calculated bucket probabilities
        market_prices: Market-implied probabilities (orderbook midpoints)
        horizon_days: Forecast horizon (0-3+)
        
    Returns:
        Blended posterior probabilities
    """
    if not model_probs or not market_prices:
        return model_probs or []
    
    # Horizon-weighted blending weights
    # Short horizons (0-1 days): model is reliable → alpha_model=0.75
    # Medium horizons (2 days): balanced → alpha_model=0.65
    # Long horizons (3+ days): market has aggregated info → alpha_model=0.45
    model_weight = max(0.4, 0.75 - (horizon_days * 0.10))
    market_weight = 1.0 - model_weight
    
    # Normalize both distributions
    total_model = sum(model_probs)
    total_market = sum(market_prices)
    
    if total_model == 0 or total_market == 0:
        return model_probs
    
    norm_model = [p / total_model for p in model_probs]
    norm_market = [p / total_market for p in market_prices]
    
    # Bayesian blend
    blended = [
        model_weight * norm_model[i] + market_weight * norm_market[i]
        for i in range(len(model_probs))
    ]
    
    return blended


# Constants
MAX_PORTFOLIO_EXPOSURE_PCT = 0.20  # Max 20% of balance per market
CONVICTION_MIN = 0.3               # Minimum conviction score to trade


def parse_bucket_temp(outcome_name):
    """Extract temperature from outcome name like '23°C' or '31+°C'."""
    match = re.search(r'(\d+)', outcome_name)
    return float(match.group(1)) if match else None


def get_hko_forecast_for_date(date_str_yyyymmdd):
    """
    Get HKO forecast using correlation-weighted multi-station ensemble.
    Falls back to single station if ensemble data unavailable.
    """
    # Try correlation-weighted ensemble first
    ens = get_ensemble(target_date=date_str_yyyymmdd)
    if ens and "mean" in ens:
        logger.info(f"Correlation-weighted ensemble: mean={ens['mean']:.1f}°C, "
                     f"std={ens.get('std', 0):.1f}, "
                     f"n={ens['stations']} stations")
        return ens["mean"]

    # Fallback: single station daily forecast
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT max_temperature, min_temperature
            FROM forecast_daily
            WHERE station_code = 'HKO' AND forecast_date = ?
            ORDER BY fetched_at DESC LIMIT 1
        """, (date_str_yyyymmdd,)).fetchone()

        if row:
            return row[0]

        # Fallback to 9-day forecast
        row = conn.execute("""
            SELECT max_temp FROM forecast_nine_day
            WHERE forecast_date = ?
            LIMIT 1
        """, (date_str_yyyymmdd,)).fetchone()

        return row[0] if row else None
    finally:
        conn.close()


async def run_signal_engine():
    """
    Main analysis and trading loop.
    - Queries market_outcomes for active markets
    - Connects WebSocket to ALL tokens
    - Calculates model probability for each bucket
    - Compares against live orderbook midpoint
    - Executes if edge > threshold
    """
    # Get all active condition_ids from market_outcomes
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT condition_id FROM market_outcomes")
        active_conditions = [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()
    
    if not active_conditions:
        logger.warning("No active markets in market_outcomes table")
        return
    
    logger.info(f"Starting pipeline for {len(active_conditions)} markets")
    
    book_manager = PolymarketOrderbookManager()
    execution_engine = PaperExecutionEngine(DB_PATH, book_manager)
    
    # Connect WebSocket for ALL tokens across all active markets
    for condition_id in active_conditions:
        try:
            await book_manager.connect(condition_id)
            logger.info(f"Connected WebSocket for {condition_id}")
        except Exception as e:
            logger.error(f"Failed to connect for {condition_id}: {e}")
    
    # Wait for initial snapshot
    await asyncio.sleep(5)
    
    # Main analysis loop
    for condition_id in active_conditions:
        try:
            # Get HKO forecast for this market
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', condition_id)
            if not date_match:
                continue

            date_str = date_match.group(1)
            hko_max = get_hko_forecast_for_date(date_str.replace('-', ''))

            if hko_max is None:
                logger.info(f"No HKO forecast for {date_str}, skipping")
                continue

            # Calculate horizon
            target_date = date.fromisoformat(date_str)
            today = date.today()
            horizon = (target_date - today).days

            # Intraday trading cutoff: 18:00 HKT for same-day markets
            from datetime import datetime as dt_now, timedelta, timezone
            HKT = timezone(timedelta(hours=8))
            now_hkt = dt_now.now(HKT)
            if target_date == now_hkt.date() and now_hkt.hour >= 18:
                logger.info(f"[{condition_id}] Same-day market past 18:00 HKT cutoff ({now_hkt.strftime('%H:%M')}), skipping")
                continue

            threshold = get_edge_threshold(horizon)

            # Get all outcomes for this condition
            conn = get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT yes_token_id, outcome_name, temp_min, temp_max
                    FROM market_outcomes 
                    WHERE condition_id = ?
                """, (condition_id,))
                outcomes = cursor.fetchall()
            finally:
                conn.close()

            # Phase 1: Score ALL outcomes (no trades yet)
            scored_buckets = []
            all_probs = []
            for outcome in outcomes:
                token_id = outcome[0]
                outcome_name = outcome[1]
                bucket_temp = parse_bucket_temp(outcome_name)
                if bucket_temp is None:
                    continue

                model_prob = calculate_bucket_probability(hko_max, bucket_temp, horizon)
                all_probs.append(model_prob)

                bids, asks = book_manager.get_snapshot(token_id)
                if not bids or not asks:
                    scored_buckets.append({
                        'token_id': token_id, 'outcome_name': outcome_name,
                        'model_prob': model_prob, 'market_yes': None,
                    })
                    continue

                best_bid = bids[0][0]
                best_ask = asks[0][0]
                market_yes = (best_bid + best_ask) / 2.0

                scored_buckets.append({
                    'token_id': token_id, 'outcome_name': outcome_name,
                    'model_prob': model_prob, 'market_yes': market_yes,
                })

            # Phase 1.5: Bayesian blend with market prices
            all_market_prices = [b.get('market_yes', 0.0) if b.get('market_yes') is not None else 0.0 for b in scored_buckets]
            blended_probs = bayesian_blend_probs(all_probs, all_market_prices, horizon)

            # Map blended probabilities back to buckets
            for idx, bucket in enumerate(scored_buckets):
                if idx < len(blended_probs):
                    bucket['model_prob'] = blended_probs[idx]

            # Phase 2: Score each bucket for NO trades
            for bucket in scored_buckets:
                if bucket['market_yes'] is not None:
                    bucket['no_score'] = calculate_no_score(
                        bucket['market_yes'], bucket['model_prob']
                    )
                else:
                    bucket['no_score'] = 0.0

            # Sort by no_score descending (best NO trade first)
            scored_buckets.sort(key=lambda x: x['no_score'], reverse=True)

            # Conviction check
            conviction = calculate_conviction(all_probs) if all_probs else 0.0
            logger.info(f"[{condition_id}] Conviction: {conviction:.3f} (min: {CONVICTION_MIN})")

            # Get balance early (needed for scoring decisions)
            balance_conn = get_connection()
            try:
                balance = balance_conn.execute(
                    "SELECT cash_balance FROM accounts WHERE account_id = 'paper_user'"
                ).fetchone()[0]
            finally:
                balance_conn.close()

            # ─── PHASE 3: Log ALL scoring decisions to DB ───
            scoring_conn = get_connection()
            try:
                for bucket in scored_buckets:
                    edge_val = (bucket.get('market_yes', 0) or 0) - bucket['model_prob']
                    no_sc = bucket.get('no_score', 0)
                    kf = max(0, 0.25 * edge_val / (1.0 - (bucket.get('market_yes', 0) or 0))) if edge_val > 0 else 0

                    # Determine decision & rationale
                    if bucket['market_yes'] is None:
                        decision = 'SKIP'
                        rationale = 'No orderbook data'
                    elif conviction < CONVICTION_MIN:
                        decision = 'SKIP'
                        rationale = f'Conviction {conviction:.3f} < {CONVICTION_MIN}'
                    elif no_sc > 0 and edge_val >= threshold and kf * balance > 10:
                        decision = 'TRADE_CANDIDATE'
                        rationale = f'Edge={edge_val:.4f}, NO_score={no_sc:.6f}, Kelly={kf:.4f}'
                    elif no_sc <= 0:
                        decision = 'SKIP'
                        rationale = f'Market overpriced YES (model>{market}: no edge)'
                    elif edge_val < threshold:
                        decision = 'SKIP'
                        rationale = f'Edge {edge_val:.4f} < threshold {threshold:.4f}'
                    else:
                        decision = 'SKIP'
                        rationale = f'Position size too small'

                    scoring_conn.execute("""
                        INSERT INTO scoring_log (timestamp, condition_id, bucket, hko_forecast,
                            model_prob, market_yes, edge, no_score, conviction, kelly_frac,
                            position_size, decision, rationale)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (time.time(), condition_id, bucket['outcome_name'],
                          hko_max, bucket['model_prob'], bucket.get('market_yes'),
                          edge_val, no_sc, conviction, kf,
                          kf * balance if balance else 0, decision, rationale))

                scoring_conn.commit()
            finally:
                scoring_conn.close()

            if conviction < CONVICTION_MIN:
                logger.info(f"[{condition_id}] Skipping — conviction {conviction:.3f} below {CONVICTION_MIN}")

            # ─── PHASE 4: Post maker limit orders on high-prob buckets ───
            maker_conn = get_connection()
            try:
                for bucket in scored_buckets:
                    if bucket['market_yes'] is None or bucket['model_prob'] < MAKER_MIN_PROB:
                        continue

                    fair_value = bucket['model_prob']
                    mk_bid = fair_value - MAKER_SPREAD   # We BUY YES here
                    mk_ask = fair_value + MAKER_SPREAD   # We SELL YES here

                    bids, asks = book_manager.get_snapshot(bucket['token_id'])
                    best_bid = bids[0][0] if bids else 0
                    best_ask = asks[0][0] if asks else 1

                    # Only post if we're improving the book (our bid >= their bid, or our ask <= their ask)
                    if mk_bid >= best_bid:
                        maker_conn.execute("""
                            INSERT INTO maker_orders (timestamp, condition_id, bucket, side, price, size, fair_value, spread_offset, rationale)
                            VALUES (?, ?, ?, 'BUY_YES', ?, ?, ?, ?, ?)
                        """, (time.time(), condition_id, bucket['outcome_name'],
                              mk_bid, MAKER_MAX_POSITION, fair_value, MAKER_SPREAD,
                              f'Model={fair_value:.3f} bid@{mk_bid:.3f} vs market@{best_bid:.3f}'))

                    if mk_ask <= best_ask:
                        maker_conn.execute("""
                            INSERT INTO maker_orders (timestamp, condition_id, bucket, side, price, size, fair_value, spread_offset, rationale)
                            VALUES (?, ?, ?, 'SELL_YES', ?, ?, ?, ?, ?)
                        """, (time.time(), condition_id, bucket['outcome_name'],
                              mk_ask, MAKER_MAX_POSITION, fair_value, MAKER_SPREAD,
                              f'Model={fair_value:.3f} ask@{mk_ask:.3f} vs market@{best_ask:.3f}'))

                maker_conn.commit()
            finally:
                maker_conn.close()

            # Get single best NO candidate
            best = scored_buckets[0]
            no_score = best['no_score']
            edge = best.get('market_yes', 0) - best['model_prob'] if best.get('market_yes') else 0
            threshold = get_edge_threshold(horizon)

            if no_score <= 0 or edge < threshold:
                logger.info(f"[{condition_id}] No strong NO candidate (score={no_score:.6f}, edge={edge:.4f})")
                continue

            outcome_name = best['outcome_name']
            market_yes = best['market_yes']
            token_id = best['token_id']

            logger.info(
                f"[{condition_id}] BEST NO: {outcome_name} | "
                f"Model: {best['model_prob']:.4f} | Market YES: {market_yes:.4f} | "
                f"Edge: {edge:.4f} | Score: {no_score:.6f} | Conviction: {conviction:.3f}"
            )

            # Portfolio exposure check
            conn = get_connection()
            try:
                balance = conn.execute(
                    "SELECT cash_balance FROM accounts WHERE account_id = 'paper_user'"
                ).fetchone()[0]
            finally:
                conn.close()

            # Kelly sizing for NO trade: f* = edge / (1 - market_yes)
            kelly_frac = edge / (1.0 - market_yes)
            # Fractional Kelly (quarter for safety)
            kelly_frac = max(0, 0.25 * kelly_frac)
            position_size = min(balance * kelly_frac, balance * 0.10)  # Hard cap 10%

            if position_size < 10:
                logger.info(f"[{condition_id}] Position size too small (${position_size:.0f})")
                continue

            # Record in market_ticks
            tick_conn = get_connection()
            try:
                tick_conn.execute("""
                    INSERT INTO market_ticks 
                    (condition_id, polymarket_yes_price, hko_predicted_value, 
                     hko_forecast_horizon_days, model_calculated_prob, generated_signal)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (condition_id, market_yes, hko_max, horizon, best['model_prob'], "SELL"))
                tick_conn.commit()
            finally:
                tick_conn.close()

            # Execute: Sell YES (buy NO)
            fill = execution_engine.execute_paper_sell(
                'paper_user', condition_id, token_id, position_size, market_yes
            )

            if fill['status'] == 'FILLED':
                logger.info(
                    f"SELL YES (BUY NO): {outcome_name} | "
                    f"Size: ${position_size:.0f} | Filled: {fill['qty']:.2f} @ {fill['avg_price']:.4f} | "
                    f"Edge: {edge:.4f} | Conviction: {conviction:.3f}"
                )
            else:
                logger.info(f"SELL rejected for {outcome_name}: {fill.get('reason', 'unknown')}")
        
        except Exception as e:
            logger.error(f"Error processing {condition_id}: {e}")
            import traceback
            traceback.print_exc()
    
    # Clean up
    book_manager.disconnect()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )
    asyncio.run(run_signal_engine())
