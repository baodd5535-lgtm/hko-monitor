"""Signal generator for paper trading based on HKO forecasts and Polymarket odds."""
import sqlite3
from datetime import datetime
from hko_weather_monitor.orderbook_manager import PolymarketOrderbookManager


class SignalGenerator:
    """Generates trading signals by comparing HKO forecasts with Polymarket odds."""
    
    def __init__(self, db_path):
        self.db_path = db_path
        
    def generate_signal(self, account_id, condition_id, token_id, hko_forecast, market_data):
        """Generate BUY/SELL/HOLD signal based on HKO forecast vs Polymarket odds."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get best ask price from orderbook
        if not hasattr(self, 'book_manager') or not self.book_manager:
            return {"status": "NO_ORDERBOOK", "reason": "Orderbook not connected"}
        
        _, sorted_asks = self.book_manager.get_snapshot(token_id)
        if not sorted_asks:
            return {"status": "NO_LIQUIDITY", "reason": "No orderbook liquidity available"}
        
        best_ask = sorted_asks[0][0]
        model_prob = market_data.get('model_prob', 0.5)
        
        # Calculate edge
        edge = model_prob - best_ask
        
        # Determine signal based on edge threshold
        if edge > 0.10:  # 10% threshold
            signal = "BUY"
        elif edge < -0.10:  # -10% threshold
            signal = "SELL"
        else:
            signal = "HOLD"
        
        # Record signal in market_ticks for audit
        cursor.execute("""
            INSERT INTO market_ticks (
                condition_id, 
                polymarket_yes_price, 
                hko_predicted_value, 
                model_calculated_prob, 
                generated_signal
            ) VALUES (?, ?, ?, ?, ?)
        """, (condition_id, best_ask, hko_forecast, model_prob, signal))
        
        conn.commit()
        conn.close()
        
        return {
            "status": "SIGNAL",
            "signal": signal,
            "edge": edge,
            "model_prob": model_prob,
            "market_price": best_ask,
            "hko_forecast": hko_forecast
        }
