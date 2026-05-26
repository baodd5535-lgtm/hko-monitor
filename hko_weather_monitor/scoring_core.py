"""
Canonical scoring engine for HKO weather trading.
Both engine.py and pipeline.py import from this module.
"""
import logging
import time
import requests
from hko_weather_monitor.db import get_connection

logger = logging.getLogger(__name__)


def get_orderbook_snapshot(token_id, book_manager):
    """Two-tier fallback: WS → REST → DB. Returns (bids, asks) or (None, None)."""
    # Tier 0: WebSocket in-memory snapshot
    bids, asks = book_manager.get_snapshot(token_id)
    if bids and asks:
        return bids, asks

    # Tier 1: REST fallback to Polymarket CLOB
    try:
        res = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=2)
        if res.status_code == 200:
            data = res.json()
            bids = [(float(b['price']), float(b['size'])) for b in data.get('bids', [])]
            asks = [(float(a['price']), float(a['size'])) for a in data.get('asks', [])]
            if bids and asks:
                return bids, asks
    except Exception:
        pass

    # Tier 2: DB fallback
    fb_conn = get_connection()
    try:
        fb = fb_conn.execute("""
            SELECT best_bid, best_ask FROM orderbook_state
            WHERE token_id = ? AND best_bid IS NOT NULL
            ORDER BY id DESC LIMIT 1
        """, (token_id,)).fetchone()
        if fb and fb[1]:
            return [((fb[0], 100),)], [((fb[1], 100),)]
    finally:
        fb_conn.close()

    return None, None


def log_scoring_decision(conn, condition_id, bucket, hko_forecast,
                         model_prob, market_yes, edge, no_score,
                         conviction, kelly_frac, position_size,
                         decision, rationale):
    """Log a scoring decision to scoring_log table."""
    conn.execute("""
        INSERT INTO scoring_log (timestamp, condition_id, bucket, hko_forecast,
            model_prob, market_yes, edge, no_score, conviction, kelly_frac,
            position_size, decision, rationale)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (time.time(), condition_id, bucket, hko_forecast,
          model_prob, market_yes, edge, no_score, conviction,
          kelly_frac, position_size, decision, rationale))


def log_maker_order(conn, condition_id, bucket, side, price, size,
                    fair_value, spread_offset, rationale):
    """Log a maker order to maker_orders table."""
    conn.execute("""
        INSERT INTO maker_orders (timestamp, condition_id, bucket, side,
            price, size, fair_value, spread_offset, rationale)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (time.time(), condition_id, bucket, side,
          price, size, fair_value, spread_offset, rationale))


def determine_decision(market_yes, conviction, existing, no_score,
                       edge, threshold, kelly_frac, balance, CONVICTION_MIN):
    """Determine trade decision and rationale string."""
    if market_yes is None:
        return 'SKIP', 'No orderbook data'
    elif conviction < CONVICTION_MIN:
        return 'SKIP', f'Conviction {conviction:.3f} < {CONVICTION_MIN}'
    elif existing > 0:
        return 'SKIP', f'Already have {existing} NO position(s)'
    elif no_score > 0 and edge >= threshold and kelly_frac * balance > 10:
        return 'TRADE_CANDIDATE', f'Edge={edge:.4f} NO={no_score:.4f} Kelly={kelly_frac:.4f}'
    elif no_score <= 0:
        return 'SKIP', 'Market overpriced YES — no edge'
    elif edge < threshold:
        return 'SKIP', f'Edge {edge:.4f} < threshold {threshold:.4f}'
    else:
        return 'SKIP', 'Position too small'


def should_post_maker_order(bucket_prob, model_prob, MAKER_MIN_PROB, MAKER_SPREAD, horizon_days):
    """
    Determine if we should post a maker order on this bucket.
    Returns (mk_bid, mk_ask) or (None, None).
    Uses horizon-scaled spread: Spread = MAKER_SPREAD × (1 + 0.5 × horizon_days)
    """
    if bucket_prob is None or model_prob < MAKER_MIN_PROB:
        return None, None

    # Horizon-scaled spread
    spread = MAKER_SPREAD * (1 + 0.5 * horizon_days)
    fv = model_prob
    return fv - spread, fv + spread
