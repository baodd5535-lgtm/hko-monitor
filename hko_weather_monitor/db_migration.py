"""Database migration for paper trading system."""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hko_weather.db")


def migrate_database():
    """Execute database migration for paper trading tables."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Markets registry
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            condition_id TEXT PRIMARY KEY,
            market_title TEXT NOT NULL,
            target_date TEXT NOT NULL,
            resolution_metric TEXT NOT NULL,
            threshold_value REAL NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            winning_outcome TEXT
        )
    """)
    
    # Market ticks (5-15 min snapshots)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_ticks (
            tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            polymarket_yes_price REAL,
            polymarket_no_price REAL,
            hko_predicted_value REAL,
            hko_forecast_horizon_days INTEGER,
            model_calculated_prob REAL,
            generated_signal TEXT,
            FOREIGN KEY(condition_id) REFERENCES markets(condition_id)
        )
    """)
    
    # Accounts
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY,
            cash_balance REAL DEFAULT 10000.00,
            allocated_margin REAL DEFAULT 0.00
        )
    """)
    
    # Initialize default paper account if empty
    cursor.execute("INSERT OR IGNORE INTO accounts (account_id, cash_balance) VALUES ('paper_user', 10000.0)")

    # Orderbook state snapshots — token_id is TEXT (76-digit numbers exceed SQLite INTEGER)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orderbook_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT,
            token_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            side TEXT,
            price REAL,
            size REAL,
            best_bid REAL,
            best_ask REAL,
            updated_at TEXT
        )
    """)
    
    # Paper positions — token_id is TEXT (76-digit numbers)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            condition_id TEXT,
            token_id TEXT,
            side TEXT,
            qty REAL,
            avg_entry_price REAL,
            status TEXT DEFAULT 'OPEN',
            pnl REAL DEFAULT 0.00,
            opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            closed_at DATETIME,
            FOREIGN KEY(account_id) REFERENCES accounts(account_id),
            FOREIGN KEY(condition_id) REFERENCES markets(condition_id)
        )
    """)
    
    # Paper fills (execution ledger) — token_id is TEXT (76-digit numbers)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            condition_id TEXT,
            token_id TEXT,
            order_side TEXT,
            requested_value REAL,
            filled_qty REAL,
            avg_fill_price REAL,
            slippage_paid REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()
    print("Database migration complete.")


if __name__ == "__main__":
    migrate_database()
