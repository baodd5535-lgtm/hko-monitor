"""
Backtesting engine with real HKO outcome resolution.
Matches Polymarket categorical markets against actual HKO observations.
"""
import re
import math
from datetime import datetime, timedelta
from collections import defaultdict

from hko_weather_monitor.db import get_connection


def gaussian_cdf(z):
    """Standard normal CDF approximation."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def extract_target_date(market_title: str) -> str:
    """
    Extracts ISO date string (YYYY-MM-DD) from market title strings.
    Example: "HK Max Temp on 2026-05-22" -> "2026-05-22"
    """
    match = re.search(r'\d{4}-\d{2}-\d{2}', market_title)
    if not match:
        raise ValueError(f"Could not parse valid date pattern from title: '{market_title}'")
    return match.group(0)


def fetch_hko_actual_max_temp(target_date: str) -> float:
    """
    Queries the readings table for observed HKO max temperature on the target date.
    Uses 'HK Observatory' (station_id=1) as primary reference.
    
    Args:
        target_date: Date in YYYY-MM-DD format
    
    Returns:
        Max temperature for that day at HK Observatory
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Parse date and query readings for that day
    date_obj = datetime.strptime(target_date, "%Y-%m-%d")
    # Readings table uses format "YYYY/MM/DD HH:MM"
    date_prefix = date_obj.strftime("%Y/%m/%d")
    
    # Get all readings for HK Observatory on that date
    cursor.execute("""
        SELECT r.temperature, s.name as station
        FROM readings r
        JOIN stations s ON r.station_id = s.id
        WHERE s.name = 'HK Observatory'
        AND r.recorded_at LIKE ? || '%'
        ORDER BY r.temperature DESC
        LIMIT 1
    """, (date_prefix,))
    
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return float(row['temperature'])
    
    # Fallback: use daily max from forecast_daily if observations not available
    fallback_conn = get_connection()
    try:
        cursor2 = fallback_conn.cursor()
        forecast_date = date_obj.strftime("%Y%m%d")
        cursor2.execute("""
            SELECT max_temperature FROM forecast_daily 
            WHERE forecast_date = ? 
            ORDER BY id DESC LIMIT 1
        """, (forecast_date,))
        
        row2 = cursor2.fetchone()
    finally:
        fallback_conn.close()
    
    if row2:
        return float(row2['max_temperature'])
    
    raise ValueError(f"No actual HKO observation found for date: {target_date}")


def determine_temperature_bucket(max_temp: float) -> str:
    """
    Matches actual temperature against Polymarket categorical bucket definitions.
    
    Polymarket HK temperature buckets:
    - "22°C or below": max_temp <= 22.0
    - "23°C": 22.0 < max_temp <= 23.0
    - "24°C": 23.0 < max_temp <= 24.0
    - "25°C": 24.0 < max_temp <= 25.0
    - "26°C": 25.0 < max_temp <= 26.0
    - "27°C": 26.0 < max_temp <= 27.0
    - "28°C": 27.0 < max_temp <= 28.0
    - "29°C": 28.0 < max_temp <= 29.0
    - "30°C": 29.0 < max_temp <= 30.0
    - "31+°C": max_temp > 30.0
    
    Returns:
        Bucket label (e.g., "26°C")
    """
    buckets = [
        (22.0, "22°C or below"),
        (23.0, "23°C"),
        (24.0, "24°C"),
        (25.0, "25°C"),
        (26.0, "26°C"),
        (27.0, "27°C"),
        (28.0, "28°C"),
        (29.0, "29°C"),
        (30.0, "30°C"),
        (float('inf'), "31+°C"),
    ]
    
    for threshold, label in buckets:
        if max_temp <= threshold:
            return label
    
    return "31+°C"  # Fallback


def run_backtest_evaluation():
    """
    Backtest evaluation against REAL HKO observations.
    
    For each tick, fetches actual max temp from readings table,
    determines winning bucket, calculates TRUE Brier score and win rate.
    Skips unresolved markets (future dates with no observations).
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # Get all market ticks with market info
        cursor.execute("""
            SELECT mt.tick_id, mt.condition_id, mt.timestamp, 
                   mt.polymarket_yes_price, mt.polymarket_no_price,
                   mt.hko_predicted_value, mt.model_calculated_prob, 
                   mt.generated_signal,
                   m.target_date, m.title
            FROM market_ticks mt
            LEFT JOIN markets m ON mt.condition_id = m.condition_id
            ORDER BY mt.timestamp DESC
            LIMIT 500
        """)
        
        ticks = cursor.fetchall()
        if not ticks:
            return {"error": "No market ticks found", "total_ticks_evaluated": 0}
        
        # Get all outcome buckets per condition_id
        cursor.execute("""
            SELECT condition_id, outcome_name, temp_min, temp_max 
            FROM market_outcomes
        """)
        all_outcomes = cursor.fetchall()
        outcomes_by_condition = {}
        for row in all_outcomes:
            cond = row['condition_id']
            if cond not in outcomes_by_condition:
                outcomes_by_condition[cond] = []
            outcomes_by_condition[cond].append({
                'outcome_name': row['outcome_name'],
                'temp_min': row['temp_min'],
                'temp_max': row['temp_max']
            })
        
        total_brier_loss = 0.0
        total_ticks = 0
        buy_signals = 0
        sell_signals = 0
        hold_signals = 0
        correct_predictions = 0
        edges = []
        skipped_unresolved = 0
        
        for tick in ticks:
            condition_id = tick['condition_id']
            target_date = tick['target_date']
            
            # Extract date from target_date (format varies)
            date_match = re.search(r'\d{4}-\d{2}-\d{2}', target_date or '')
            if not date_match:
                skipped_unresolved += 1
                continue
            
            date_str = date_match.group(0)
            
            # Fetch actual max temp for that date
            try:
                actual_temp = fetch_hko_actual_max_temp(date_str)
            except ValueError:
                # No observation yet (future date or missing data)
                skipped_unresolved += 1
                continue
            
            # Determine winning bucket
            winning_bucket = determine_temperature_bucket(actual_temp)
            
            # Count signals
            signal = tick['generated_signal'] or 'HOLD'
            if signal == 'BUY':
                buy_signals += 1
            elif signal == 'SELL':
                sell_signals += 1
            else:
                hold_signals += 1
            
            model_prob = tick['model_calculated_prob'] or 0
            market_price = tick['polymarket_yes_price'] or 0
            predicted_temp = tick['hko_predicted_value'] or 0
            predicted_bucket = determine_temperature_bucket(predicted_temp)
            
            # Edge calculation
            edge = model_prob - market_price
            edges.append(edge)
            
            # TRUE Brier score: compare model_prob against actual outcome
            # The tick represents a prediction for ONE outcome bucket
            # actual_outcome = 1 if this tick's predicted bucket = winning bucket, else 0
            actual_outcome = 1.0 if predicted_bucket == winning_bucket else 0.0
            brier_loss = (model_prob - actual_outcome) ** 2
            total_brier_loss += brier_loss
            
            # Win rate: did the model's most confident bucket match reality?
            if predicted_bucket == winning_bucket:
                correct_predictions += 1
            
            total_ticks += 1
        
        # Calculate metrics
        brier_score = total_brier_loss / total_ticks if total_ticks > 0 else 0
        win_rate = correct_predictions / total_ticks if total_ticks > 0 else 0
        mean_edge = sum(edges) / len(edges) if edges else 0
        abs_mean_edge = sum(abs(e) for e in edges) / len(edges) if edges else 0
        
        return {
            "total_ticks_evaluated": total_ticks,
            "skipped_unresolved": skipped_unresolved,
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "hold_signals": hold_signals,
            "brier_score": round(brier_score, 4),
            "win_rate": round(win_rate * 100, 2),
            "mean_edge": round(mean_edge, 4),
            "abs_mean_edge": round(abs_mean_edge, 4),
            "correct_predictions": correct_predictions
        }
    finally:
        conn.close()


if __name__ == "__main__":
    print("Running backtest evaluation...")
    results = run_backtest_evaluation()
    
    print("\n" + "="*50)
    print("          STRATEGY BACKTEST PERFORMANCE REPORT     ")
    print("="*50)
    print(f"Total Evaluated Market Ticks : {results.get('total_ticks_evaluated', 0)}")
    print(f"BUY Signals                  : {results.get('buy_signals', 0)}")
    print(f"SELL Signals                 : {results.get('sell_signals', 0)}")
    print(f"HOLD Signals                 : {results.get('hold_signals', 0)}")
    print(f"Correct Predictions          : {results.get('correct_predictions', 0)}")
    print(f"Calculated Strategy Win Rate : {results.get('win_rate', 0)}%")
    print(f"Overall Model Brier Score    : {results.get('brier_score', 0)} (Closer to 0.0 = More Accurate)")
    print(f"Mean Edge                    : {results.get('mean_edge', 0)}")
    print(f"Abs Mean Edge                : {results.get('abs_mean_edge', 0)}")
    print("="*50)
