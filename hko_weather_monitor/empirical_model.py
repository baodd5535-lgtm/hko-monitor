"""Empirical error distribution and multi-station ensemble model.

Replaces the hardcoded Gaussian σ table with:
1. Actual HKO forecast errors computed from historical data in our DB
2. Seasonal filtering (errors within ±30 days of target date)
3. Multi-station ensemble (all available forecast stations)
4. Fat-tail handling via direct empirical bootstrap
"""
import sqlite3
import numpy as np
import os
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hko_weather.db")


def load_and_clean_data():
    """
    Load historical forecasts and actual observations, compute errors by horizon.

    Returns list of dicts with:
        forecast_date (str), horizon (int), hko_pred (float), true_max (float), error (float)
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get daily forecasts from CCH (Cheung Chau)
    forecasts = conn.execute("""
        SELECT forecast_date, max_temperature as hko_pred, fetched_at
        FROM forecast_daily
        WHERE station_code = 'CCH' AND max_temperature IS NOT NULL
    """).fetchall()

    # Get actual daily max temps from per-minute readings for Cheung Chau
    actuals = conn.execute("""
        SELECT substr(r.recorded_at, 1, 10) as obs_date,
               MAX(r.temperature) as true_max
        FROM readings r
        JOIN stations s ON s.id = r.station_id
        WHERE s.name = 'Cheung Chau'
        GROUP BY substr(r.recorded_at, 1, 10)
    """).fetchall()

    # Build lookup: date_str -> actual_max
    actual_lookup = {dict(a)['obs_date']: dict(a)['true_max'] for a in actuals}

    # Compute errors
    errors = []
    for f in forecasts:
        f = dict(f)
        fc_date = f['forecast_date']  # YYYYMMDD
        predicted = f['hko_pred']
        fetched = f['fetched_at']

        if predicted is None:
            continue

        # Convert forecast_date (YYYYMMDD) to date string (YYYY/MM/DD) for lookup
        fc_str = fc_date[:4] + '/' + fc_date[4:6] + '/' + fc_date[6:]

        actual = actual_lookup.get(fc_str)
        if actual is None:
            continue

        # Compute horizon
        try:
            fetched_dt = datetime.strptime(fetched, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue

        fc_dt = datetime.strptime(fc_str, '%Y/%m/%d')
        horizon = max(0, (fc_dt - fetched_dt).days)

        errors.append({
            'forecast_date': fc_str,
            'horizon': horizon,
            'hko_pred': predicted,
            'true_max': actual,
            'error': actual - predicted,
        })

    conn.close()
    return errors


def calculate_empirical_probability(
    all_errors, target_date: str, hko_forecast: float, horizon: int,
    bucket_low: float, bucket_high: float,
    seasonal_window_days: int = 30,
) -> float:
    """
    Calculate empirical probability that actual temp falls in [bucket_low, bucket_high).

    Uses historical errors from the same horizon AND same season (±30 days).
    """
    # Filter by horizon
    horizon_errors = [e for e in all_errors if e['horizon'] == horizon]

    if not horizon_errors:
        return 0.5

    # Parse target date for seasonal filtering
    try:
        target_dt = datetime.strptime(target_date, '%Y-%m-%d')
        target_doy = target_dt.timetuple().tm_yday
    except ValueError:
        target_dt = datetime.strptime(target_date, '%Y/%m/%d')
        target_doy = target_dt.timetuple().tm_yday

    # Seasonal filter
    seasonal_errors = []
    for e in horizon_errors:
        try:
            fc_dt = datetime.strptime(e['forecast_date'], '%Y/%m/%d')
            doy = fc_dt.timetuple().tm_yday
            day_diff = abs(doy - target_doy)
            if day_diff <= seasonal_window_days or day_diff >= (365 - seasonal_window_days):
                seasonal_errors.append(e['error'])
        except (ValueError, KeyError):
            continue

    # Use seasonal if enough data, otherwise all horizon errors
    if len(seasonal_errors) >= 10:
        error_vals = np.array(seasonal_errors)
    else:
        error_vals = np.array([e['error'] for e in horizon_errors])

    if len(error_vals) < 5:
        logger.warning(f"Not enough errors for horizon={horizon} (n={len(error_vals)})")
        return 0.5

    # Reconstruct implied actual distribution
    implied_actuals = hko_forecast + error_vals

    # Empirical probability
    in_bucket = np.sum((implied_actuals >= bucket_low) & (implied_actuals < bucket_high))
    prob = in_bucket / len(implied_actuals)

    return max(0.001, min(0.999, float(prob)))


# Station geography: coastal vs inland (affects microclimate)
# Polymarket typically uses HKO Head Office (Tsim Sha Tsui) or Cheung Chau for settlement
STATION_PROXIMITY = {
    # Coastal / urban — closest to settlement station (HKO TS / CCH)
    "HKO": 1.0,   # HK Observatory (Tsim Sha Tsui) — settlement reference
    "CCH": 1.0,   # Cheung Chau — common settlement
    "HKA": 0.9,   # Chek Lap Kok — coastal airport
    "HKS": 0.85,  # HK Park — urban coastal
    "JKB": 0.85,  # Kai Tak — urban coastal
    "WGL": 0.7,   # Waglan Island — coastal but far
    "PEN": 0.7,   # Peng Chau — coastal but far
    "CWB": 0.8,   # Clear Water Bay — coastal
    "YLP": 0.8,   # Yau Tsim Mong — coastal urban
    # Sub-urban / semi-coastal
    "SHA": 0.6,   # Sha Tin — sub-urban
    "SEK": 0.55,  # Sheung Shui — sub-urban
    "TPO": 0.6,   # Tseung Kwan O — coastal but sheltered
    "SSH": 0.55,  # Sha Tau Kok — sub-urban
    "TY1": 0.65,  # Tai Mei Tuk — sub-urban
    "YLP": 0.6,   # Yuen Long Park — sub-urban
    # Inland — discounted (urban heat island, less sea breeze)
    "SKG": 0.3,   # Shek Kong — inland
    "TKL": 0.25,  # Ta Kwu Ling — inland hill
    "TUN": 0.3,   # Tuen Mun — inland-ish
    "LFS": 0.2,   # Lau Fau Shan — far inland
}


def compute_station_weights(all_errors):
    """
    Compute station weights from historical forecast accuracy.

    Strategy:
    1. Per-station RMSE from historical errors
    2. Inverse-RMSE weighting (more accurate = more weight)
    3. Multiply by proximity score (coastal > inland)
    4. Normalize to sum to 1.0
    """
    # Per-station RMSE
    station_rmse = {}
    for station in set(e.get('station_code') for e in all_errors):
        errors = [e['error'] for e in all_errors if e.get('station_code') == station]
        if errors:
            station_rmse[station] = float(np.sqrt(np.mean(np.array(errors) ** 2)))
        else:
            station_rmse[station] = 3.0  # default high RMSE for unknown

    # Combine inverse-RMSE with proximity
    raw_weights = {}
    for station, rmse in station_rmse.items():
        inv_rmse = 1.0 / max(rmse, 0.1)  # avoid division by zero
        proximity = STATION_PROXIMITY.get(station, 0.3)  # default low for unknown
        raw_weights[station] = inv_rmse * proximity

    # Normalize
    total = sum(raw_weights.values())
    if total > 0:
        weights = {s: w / total for s, w in raw_weights.items()}
    else:
        weights = {}

    return weights, station_rmse


_weight_cache = None  # (weights_dict, rmse_dict)


def multi_station_ensemble(all_errors=None, target_date: str = None) -> dict:
    """
    Multi-station weighted ensemble.

    Weights = (inverse_RMSE * coastal_proximity), normalized to sum to 1.0.
    Inland stations (TKL, LFS, SKG) are heavily discounted.
    """
    global _weight_cache

    conn = sqlite3.connect(DB_PATH)

    if target_date is None:
        target_date = date.today().strftime("%Y%m%d")

    # Deduplicate: take latest fetch per station
    rows = conn.execute("""
        SELECT station_code, max_temperature
        FROM forecast_daily
        WHERE forecast_date = ? AND max_temperature IS NOT NULL
          AND (station_code, max_temperature, fetched_at) IN (
              SELECT station_code, max_temperature, MAX(fetched_at)
              FROM forecast_daily
              WHERE forecast_date = ? AND max_temperature IS NOT NULL
              GROUP BY station_code
          )
    """, (target_date, target_date)).fetchall()
    conn.close()

    if not rows:
        return {}

    # Compute weights if not cached
    if _weight_cache is None and all_errors:
        _weight_cache = compute_station_weights(all_errors)

    weights, rmse_map = _weight_cache if _weight_cache else ({}, {})

    # If no weights computed, use proximity-only (first run)
    if not weights:
        raw = {}
        for r in rows:
            code = r[0]
            raw[code] = STATION_PROXIMITY.get(code, 0.3)
        total = sum(raw.values())
        if total > 0:
            weights = {s: w / total for s, w in raw.items()}
        else:
            weights = {r[0]: 1.0 / len(rows) for r in rows}

    # Weighted ensemble
    weighted_sum = 0.0
    for r in rows:
        code, temp = r[0], r[1]
        w = weights.get(code, 0.0)
        weighted_sum += w * temp

    n = len(rows)
    result = {
        "mean": weighted_sum,
        "stations": n,
        "weighted": True,
    }

    # Compute weighted std
    if n > 1:
        var_sum = sum(weights.get(r[0], 0) * (r[1] - weighted_sum) ** 2 for r in rows)
        result["std"] = float(np.sqrt(max(var_sum, 0)))
    else:
        result["std"] = 0.5

    return result


def market_awareness_check() -> dict:
    """
    Check for extreme current conditions the market might know about.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT r.temperature, r.recorded_at
        FROM readings r
        JOIN stations s ON s.id = r.station_id
        WHERE s.name = 'HK Observatory'
        ORDER BY r.recorded_at DESC LIMIT 1
    """).fetchone()
    conn.close()

    if row:
        temp = row[0]
        return {"current_temp": temp, "is_extreme": temp is not None and (temp < 22 or temp > 33)}
    return {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    errors = load_and_clean_data()
    print(f"Loaded {len(errors)} forecast-actual pairs")
    if errors:
        horizons = set(e['horizon'] for e in errors)
        for h in sorted(horizons):
            subset = [e['error'] for e in errors if e['horizon'] == h]
            arr = np.array(subset)
            print(f"  h={h}: n={len(arr)}, mean={arr.mean():.2f}, "
                  f"std={arr.std():.2f}, min={arr.min():.1f}, max={arr.max():.1f}")

        # Test
        ens = multi_station_ensemble()
        print(f"\nEnsemble: {ens}")
        print(f"Awareness: {market_awareness_check()}")
