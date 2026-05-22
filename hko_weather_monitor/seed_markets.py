"""Seed markets + market_outcomes from live Polymarket Gamma API."""
import json
import re
import sqlite3
import os
import sys

# Add parent dir for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from hko_weather_monitor.polymarket import fetch_active_hk_polymarket, extract_temp_from_title

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hko_weather.db")


def parse_temp_bounds(outcome_name: str):
    """Parse outcome name into (temp_min, temp_max) for bucket matching.
    
    Examples:
      '22°C or below' -> (None, 22.0)
      '23°C'          -> (22.5, 23.5)
      '31+°C'         -> (30.5, None)
    """
    match = re.search(r'(\d+)', outcome_name)
    if not match:
        return (None, None)

    temp = int(match.group(1))

    if 'below' in outcome_name.lower():
        return (None, float(temp))
    elif '+' in outcome_name or 'above' in outcome_name.lower() or 'higher' in outcome_name.lower():
        return (float(temp) - 0.5, None)
    else:
        # Exact bucket: e.g., 23°C means 22.5-23.5
        return (float(temp) - 0.5, float(temp) + 0.5)


def seed_markets():
    """Fetch active HK markets from Polymarket and seed into database."""
    print("[SEED] Fetching active HK temperature markets from Polymarket...")
    markets_data = fetch_active_hk_polymarket()

    if not markets_data:
        print("[WARN] No active markets found.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    total_outcomes = 0

    for market in markets_data:
        title = market.get("title", "")
        slug = market.get("slug", "")
        target_date = market.get("date", "")
        outcomes = market.get("outcomes", [])
        token_ids = market.get("token_ids", [])

        if not outcomes:
            continue

        # Extract condition_id from market slug or title
        # Use a deterministic hash as condition_id since Gamma API doesn't provide one directly
        condition_id = f"hk_temp_{target_date}" if target_date else f"hk_temp_{slug}"

        print(f"\n[SEED] Market: {title}")
        print(f"  Condition ID: {condition_id}")
        print(f"  Outcomes: {len(outcomes)}, Token IDs: {len(token_ids)}")

        # 1. Insert market record
        cursor.execute("""
            INSERT OR REPLACE INTO markets (condition_id, title, slug, target_date, resolution_source, status)
            VALUES (?, ?, ?, ?, 'HKO', 'ACTIVE')
        """, (condition_id, title, slug, target_date))

        # 2. Insert each outcome with its token mapping
        # Token IDs are paired: [YES_22, NO_22, YES_23, NO_23, ...]
        # But for categorical markets, each token = ONE outcome bucket (YES side)
        # The "NO" tokens are synthetically derived
        # We map outcomes to the YES tokens (odd indices: 0, 2, 4, ...)

        for idx, outcome in enumerate(outcomes):
            outcome_name = outcome.get("label", outcome.get("temp", ""))
            temp_min, temp_max = parse_temp_bounds(outcome_name)

            # Get the YES token for this outcome (every other token starting from 0)
            yes_token_idx = idx * 2  # 0, 2, 4, 6, ...
            yes_token_id = token_ids[yes_token_idx] if yes_token_idx < len(token_ids) else None

            if not yes_token_id:
                print(f"  [SKIP] No token for {outcome_name}")
                continue

            cursor.execute("""
                INSERT OR REPLACE INTO market_outcomes 
                (condition_id, outcome_name, temp_min, temp_max, yes_token_id)
                VALUES (?, ?, ?, ?, ?)
            """, (condition_id, outcome_name, temp_min, temp_max, yes_token_id))

            total_outcomes += 1
            print(f"  [{idx}] {outcome_name}: {temp_min}-{temp_max} -> {yes_token_id[:20]}...")

    conn.commit()
    conn.close()

    print(f"\n[SEED] Complete! Seeded {len(markets_data)} markets, {total_outcomes} outcomes.")
    return len(markets_data), total_outcomes


if __name__ == "__main__":
    seed_markets()
