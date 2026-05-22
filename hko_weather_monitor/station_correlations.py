"""
Station-to-station correlation weighting.

Computes each station's bias, RMSE, and correlation vs reference station (CCH).
Weights = correlation / (1 + |bias| + RMSE), normalized.

Uses forecast_daily table — improves as we accumulate more data.
"""
import sqlite3
import numpy as np
import os
import logging
from datetime import date

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hko_weather.db")
REFERENCE = "HKO"  # HK Observatory — Polymarket settlement reference


def compute_station_correlations():
    """
    Compute per-station bias, RMSE, and correlation vs reference (CCH).

    Returns dict: station_code -> {bias, rmse, correlation, weight}
    """
    conn = sqlite3.connect(DB_PATH)

    # Get all forecast data, deduplicated to latest per station per date
    rows = conn.execute("""
        SELECT station_code, forecast_date, max_temperature
        FROM forecast_daily
        WHERE max_temperature IS NOT NULL
          AND rowid IN (
              SELECT MAX(f1.rowid)
              FROM forecast_daily f1
              WHERE f1.max_temperature IS NOT NULL
              GROUP BY f1.station_code, f1.forecast_date
          )
    """).fetchall()
    conn.close()

    # Build lookup: date -> {station: temp}
    daily = {}
    for code, fc_date, temp in rows:
        if fc_date not in daily:
            daily[fc_date] = {}
        daily[fc_date][code] = temp

    # Collect diffs vs reference
    diffs = {}  # station -> list of (ref_temp, station_temp, diff)
    for fc_date, stations in daily.items():
        if REFERENCE not in stations:
            continue
        ref_temp = stations[REFERENCE]
        for code, temp in stations.items():
            if code == REFERENCE:
                continue
            if code not in diffs:
                diffs[code] = []
            diffs[code].append((ref_temp, temp, temp - ref_temp))

    # Compute stats
    correlations = {}
    for code, pairs in diffs.items():
        if len(pairs) < 3:
            continue

        ref_arr = np.array([p[0] for p in pairs])
        stat_arr = np.array([p[1] for p in pairs])
        diff_arr = np.array([p[2] for p in pairs])

        mean_bias = float(np.mean(diff_arr))
        rmse = float(np.sqrt(np.mean(diff_arr ** 2)))
        correlation = float(np.corrcoef(ref_arr, stat_arr)[0, 1])

        # Weight: high correlation, low bias, low RMSE
        if np.isnan(correlation):
            correlation = 0.0
        weight = max(0.01, abs(correlation)) / (1.0 + abs(mean_bias) + rmse)

        correlations[code] = {
            'bias': mean_bias,
            'rmse': rmse,
            'correlation': correlation,
            'n': len(pairs),
            'weight': weight,
        }

    # Normalize weights
    total = sum(c['weight'] for c in correlations.values())
    if total > 0:
        for code in correlations:
            correlations[code]['weight'] /= total
    else:
        for code in correlations:
            correlations[code]['weight'] = 1.0 / max(len(correlations), 1)

    return correlations


def get_ensemble(target_date: str = None, correlations=None):
    """
    Compute weighted ensemble for a target date.

    If reference station (HKO) has data, return it directly.
    Otherwise, use correlation-weighted ensemble of remaining stations.
    """
    if target_date is None:
        target_date = date.today().strftime("%Y%m%d")

    conn = sqlite3.connect(DB_PATH)

    # Latest forecast per station
    rows = conn.execute("""
        SELECT station_code, max_temperature
        FROM forecast_daily
        WHERE forecast_date = ? AND max_temperature IS NOT NULL
          AND rowid IN (
              SELECT MAX(f1.rowid)
              FROM forecast_daily f1
              WHERE f1.max_temperature IS NOT NULL
                AND f1.forecast_date = ?
              GROUP BY f1.station_code
          )
    """, (target_date, target_date)).fetchall()
    conn.close()

    if not rows:
        return {}

    # Always use correlation-weighted ensemble (never just HKO alone)
    if correlations is None:
        correlations = compute_station_correlations()

    # Build weights: use correlation weights for non-reference stations
    # Give reference station (HKO) bonus weight since it's the settlement anchor
    raw_weights = {}
    for code, _ in rows:
        if code == REFERENCE:
            # HKO gets a fixed anchor weight (sum of all other weights * 0.5)
            raw_weights[code] = 1.0
        else:
            raw_weights[code] = correlations.get(code, {}).get('weight', 0.01)

    # Normalize
    total = sum(raw_weights.values())
    weights = {c: w / total for c, w in raw_weights.items()}

    # Weighted mean
    mean = sum(weights.get(code, 0) * temp for code, temp in rows)
    var = sum(weights.get(code, 0) * (temp - mean) ** 2 for code, temp in rows)

    return {
        'mean': float(mean),
        'std': float(np.sqrt(max(var, 0))),
        'weights': weights,
        'stations': len(rows),
    }


def print_correlations(correlations=None):
    """Pretty-print station correlations."""
    if correlations is None:
        correlations = compute_station_correlations()

    print(f"\n{'Station':<8} {'Bias vs {REFERENCE}':>10} {'RMSE':>6} {'Corr':>6} {'N':>4} {'Weight':>8}")
    print("-" * 52)

    for code, stats in sorted(correlations.items(), key=lambda x: x[1]['weight'], reverse=True):
        print(f"{code:<8} {stats['bias']:>+10.2f} {stats['rmse']:>6.2f} "
              f"{stats['correlation']:>6.3f} {stats['n']:>4} {stats['weight']:>8.4f}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    correlations = compute_station_correlations()
    print_correlations(correlations)

    print("\n=== Ensemble (today) ===")
    ens = get_ensemble()
    print(f"Mean: {ens['mean']:.2f}°C, Std: {ens['std']:.2f}°C, Stations: {ens['stations']}")
    print("\nWeights:")
    for code, w in sorted(ens['weights'].items(), key=lambda x: x[1], reverse=True):
        print(f"  {code}: {w:.4f}")
