"""
Empirical error distribution + multi-station ensemble for HKO temperature predictions.
Replaces hardcoded Gaussian σ table with data-driven forecast error modeling.
"""
import sqlite3
import numpy as np
from datetime import datetime, date
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hko_weather.db")


def load_forecast_errors(conn=None, station_codes=None, lookback_days=365):
    """
    Compute empirical forecast errors by matching historical forecasts against actual observations.
    
    Returns DataFrame with:
    - forecast_date, actual_date, horizon, station_code
    - hko_predicted, actual_max, error (actual - predicted)
    """
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    
    if station_codes is None:
        station_codes = ["CCH", "HKO", "SHA", "SKG", "TKL", "WGL", "HKA"]  # Key stations
    
    # Get daily forecasts
    forecasts = conn.execute("""
        SELECT station_code, forecast_date, max_temperature, fetched_at
        FROM forecast_daily
        WHERE station_code IN ({})
          AND fetched_at >= datetime('now', '-{} days')
    """.format(",".join("?" * len(station_codes)), lookback_days),
        station_codes
    ).fetchall()
    
    # Get actual daily max temps from per-minute readings
    # We need to compute daily max for each date
    actuals = conn.execute("""
        SELECT s.name, substr(r.recorded_at, 1, 10) as obs_date, 
               max(r.temperature) as actual_max
        FROM readings r
        JOIN stations s ON r.station_id = s.id
        WHERE r.recorded_at >= date('now', '-{} days')
          AND s.name IN ({})
        GROUP BY s.name, substr(r.recorded_at, 1, 10)
    """.format(lookback_days, ",".join(["'{}'".format(code) for code in station_codes])),
    ).fetchall()
    
    # Build lookup: (station, date) -> actual_max
    actual_lookup = {}
    for a in actuals:
        actual_lookup[(a['name'], a['obs_date'])] = a['actual_max']
    
    # Compute errors
    errors = []
    for f in forecasts:
        station = f['station_code']
        fc_date = f['forecast_date']  # YYYYMMDD
        predicted = f['max_temperature']
        
        if predicted is None:
            continue
            
        # Parse date
        fc_str = fc_date[:4] + '/' + fc_date[4:6] + '/' + fc_date[6:]  # YYYY/MM/DD
        lookup_key = (station, fc_str)
        
        actual = actual_lookup.get(lookup_key)
        if actual is None:
            continue
            
        # Compute horizon (days between fetch and forecast date)
        fetched = datetime.strptime(f['fetched_at'], '%Y-%m-%d %H:%M:%S')
        forecast_dt = datetime.strptime(fc_str, '%Y/%m/%d')
        horizon = (forecast_dt - fetched).days
        
        error = actual - predicted
        errors.append({
            'station_code': station,
            'forecast_date': fc_date,
            'horizon': max(0, horizon),
            'predicted': predicted,
            'actual': actual,
            'error': error,
            'day_of_year': forecast_dt.timetuple().tm_yday,
        })
    
    if conn is None:
        conn.close()
    
    return errors


def get_empirical_distribution(errors, horizon, target_day_of_year, season_window=30):
    """
    Get empirical error distribution for a specific horizon and season.
    
    Args:
        errors: list of error dicts from load_forecast_errors()
        horizon: forecast horizon in days
        target_day_of_year: day of year for the target date (1-366)
        season_window: ±days for seasonal filtering
    
    Returns:
        array of implied actual temperatures (empirical distribution)
    """
    # Filter by horizon
    horizon_errors = [e['error'] for e in errors if e['horizon'] == horizon]
    
    # Apply seasonal filtering
    seasonal_errors = [
        e['error'] for e in errors 
        if e['horizon'] == horizon and 
        abs(e['day_of_year'] - target_day_of_year) <= season_window or
        abs(e['day_of_year'] - target_day_of_year) >= (365 - season_window)  # wrap around
    ]
    
    # Use seasonal if enough data, otherwise fall back to all
    if len(seasonal_errors) >= 10:
        errors_to_use = seasonal_errors
    else:
        errors_to_use = horizon_errors
    
    if len(errors_to_use) < 5:
        logger.warning(f"Only {len(errors_to_use)} errors for horizon {horizon}, using default")
        # Fallback to simple normal
        return None
    
    return np.array(errors_to_use)


def calculate_bucket_probability_empirical(
    hko_max_temp, bucket_temp, horizon_days, target_date,
    errors=None, bucket_low=None, bucket_high=None
):
    """
    Calculate bucket probability using empirical error distribution.
    
    Args:
        hko_max_temp: HKO forecast max temperature
        bucket_temp: center temperature of the bucket
        horizon_days: forecast horizon in days
        target_date: target date as date object
        errors: pre-computed errors (from load_forecast_errors), loads from DB if None
        bucket_low, bucket_high: override bucket boundaries
    
    Returns:
        probability that actual max temp falls in the bucket
    """
    if errors is None:
        errors = load_forecast_errors()
    
    # Default bucket boundaries: ±0.5°C
    if bucket_low is None:
        bucket_low = bucket_temp - 0.5
    if bucket_high is None:
        bucket_high = bucket_temp + 0.5
    
    # Special handling for boundary buckets
    if bucket_temp <= 22:  # "X°C or below"
        bucket_low = -100.0
    elif bucket_temp >= 31:  # "X+°C"
        bucket_high = 100.0
    
    empirical = get_empirical_distribution(
        errors, horizon_days, target_date.timetuple().tm_yday
    )
    
    if empirical is None:
        # Fall back to original Gaussian
        from hko_weather_monitor.pipeline import calculate_bucket_probability
        return calculate_bucket_probability(hko_max_temp, bucket_temp, horizon_days)
    
    # Implied actual distribution = forecast + empirical errors
    implied_actuals = hko_max_temp + empirical
    
    # Probability = fraction of samples in bucket
    in_bucket = np.sum((implied_actuals >= bucket_low) & (implied_actuals < bucket_high))
    prob = in_bucket / len(implied_actuals)
    
    return max(0.001, min(0.999, prob))


def multi_station_ensemble(hko_forecasts, actual_station="CCH"):
    """
    Simple ensemble: average forecasts from multiple stations, weighted by historical accuracy.
    
    Args:
        hko_forecasts: dict of {station_code: max_temp}
        actual_station: the station Polymarket uses for settlement
    
    Returns:
        ensemble forecast temperature
    """
    # Historical accuracy weights (to be computed from load_forecast_errors)
    # For now, equal weight across all available stations
    if not hko_forecasts:
        return None
    
    temps = [t for t in hko_forecasts.values() if t is not None]
    return np.mean(temps) if temps else None


def dynamic_edge_threshold(horizon_days, market_liquidity=0):
    """
    Dynamic edge threshold based on horizon AND market liquidity.
    Higher threshold when liquidity is low (to avoid slippage traps).
    """
    # Base threshold by horizon
    base_thresholds = {
        0: 0.05, 1: 0.07, 2: 0.09, 3: 0.10,
        4: 0.12, 5: 0.15, 6: 0.20, 7: 0.25,
        8: 0.30, 9: 0.35,
    }
    base = base_thresholds.get(max(0, min(horizon_days, 9)), 0.10)
    
    # Liquidity adjustment: if market liquidity < $100, increase threshold
    if market_liquidity < 100:
        base *= 2.0  # Double threshold for illiquid markets
    elif market_liquidity < 500:
        base *= 1.5
    
    return base


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Load historical errors
    errors = load_forecast_errors()
    print(f"Loaded {len(errors)} forecast-actual pairs")
    
    if errors:
        # Summary statistics by horizon
        for h in range(5):
            horizon_errors = [e['error'] for e in errors if e['horizon'] == h]
            if horizon_errors:
                arr = np.array(horizon_errors)
                print(f"Horizon {h}d: n={len(arr)}, mean={arr.mean():.2f}, "
                      f"std={arr.std():.2f}, min={arr.min():.1f}, max={arr.max():.1f}")
        
        # Test empirical probability
        today = date.today()
        tomorrow = today.replace(day=min(today.day + 1, 28))
        
        # Get current forecast
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        forecast = conn.execute("""
            SELECT max_temperature FROM forecast_daily 
            WHERE station_code = 'CCH' AND forecast_date = ?
            ORDER BY fetched_at DESC LIMIT 1
        """, (tomorrow.strftime('%Y%m%d'),)).fetchone()
        conn.close()
        
        if forecast:
            hko_max = forecast['max_temperature']
            print(f"\nHKO forecast for {tomorrow}: {hko_max}°C")
            
            for bucket_temp in [28, 29, 30, 31]:
                prob = calculate_bucket_probability_empirical(
                    hko_max, bucket_temp, 1, tomorrow, errors
                )
                print(f"  P({bucket_temp}°C bucket) = {prob:.4f}")
