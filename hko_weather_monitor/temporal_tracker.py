"""Tracks temporal changes in orderbook prices and HKO forecasts."""
import time
from collections import deque
from typing import Dict, List, Optional, Deque


class TemporalTracker:
    def __init__(self, orderbook_window_sec: int = 600, forecast_window_slots: int = 12):
        # Map: token_address -> deque((timestamp, mid_price, bid_ask_spread))
        self.orderbook_history: Dict[str, Deque] = {}
        self.orderbook_window_sec = orderbook_window_sec
        
        # Map: target_date_str -> deque((timestamp, forecast_max_temp, rain_prob))
        self.forecast_history: Dict[str, Deque] = {}
        self.forecast_window_slots = forecast_window_slots

    def record_orderbook(self, token_address: str, best_bid: float, best_ask: float) -> None:
        now = time.time()
        mid_price = (best_bid + best_ask) / 2.0
        if token_address not in self.orderbook_history:
            self.orderbook_history[token_address] = deque()
        
        self.orderbook_history[token_address].append((now, mid_price, best_ask - best_bid))
        
        # Evict old data outside tracking window
        while self.orderbook_history[token_address] and self.orderbook_history[token_address][0][0] < now - self.orderbook_window_sec:
            self.orderbook_history[token_address].popleft()

    def record_hko_forecast(self, target_date: str, max_temp: float, rain_probability: float) -> None:
        now = time.time()
        if target_date not in self.forecast_history:
            self.forecast_history[target_date] = deque()
            
        self.forecast_history[target_date].append((now, max_temp, rain_probability))
        
        if len(self.forecast_history[target_date]) > self.forecast_window_slots:
            self.forecast_history[target_date].popleft()

    def get_orderbook_momentum(self, token_address: str) -> float:
        """Returns the price delta over the tracking window. Positive means upward repricing."""
        history = self.orderbook_history.get(token_address)
        if not history or len(history) < 2:
            return 0.0
        return history[-1][1] - history[0][1]

    def get_forecast_delta(self, target_date: str) -> Dict[str, float]:
        """Calculates direction of HKO forecast updates."""
        history = self.forecast_history.get(target_date)
        if not history or len(history) < 2:
            return {"temp_delta": 0.0, "rain_prob_delta": 0.0}
        return {
            "temp_delta": history[-1][1] - history[0][1],
            "rain_prob_delta": history[-1][2] - history[0][2]
        }
