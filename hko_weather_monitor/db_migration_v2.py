"""Phase 1 migration: Upgrade markets table + add market_outcomes table."""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hko_weather.db")


def migrate_markets_v2():
    """Upgrade markets schema for categorical outcomes + add market_outcomes table."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if already migrated (market_outcomes table exists)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='market_outcomes'")
    if cursor.fetchone():
        print("[SKIP] market_outcomes table already exists — migration v2 done.")
        conn.close()
        return

    print("[MIGRATE] Upgrading markets table schema...")

    # 1. Drop old markets table (empty anyway)
    cursor.execute("DROP TABLE IF EXISTS markets")

    # 2. Create new markets table (relational)
    cursor.execute("""
        CREATE TABLE markets (
            condition_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            slug TEXT NOT NULL,
            target_date TEXT NOT NULL,  -- YYYY-MM-DD
            resolution_source TEXT NOT NULL,  -- e.g., 'HKO'
            status TEXT DEFAULT 'ACTIVE',  -- ACTIVE, RESOLVED
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 3. Create market_outcomes table (relational, one row per categorical bucket)
    cursor.execute("""
        CREATE TABLE market_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT NOT NULL REFERENCES markets(condition_id),
            outcome_name TEXT NOT NULL,  -- e.g., "22°C", "23°C", "31+°C"
            temp_min REAL,               -- e.g., 22.0 (NULL for lower bound)
            temp_max REAL,               -- e.g., 22.9 (NULL for upper bound)
            yes_token_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(condition_id, outcome_name)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mo_condition ON market_outcomes(condition_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mo_token ON market_outcomes(yes_token_id)")

    conn.commit()
    conn.close()
    print("[MIGRATE] Done — markets + market_outcomes tables created.")


if __name__ == "__main__":
    migrate_markets_v2()
