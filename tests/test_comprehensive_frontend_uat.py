import os
import sys
import json
import sqlite3
import requests
import unittest
from datetime import datetime

BASE_URL = "http://localhost:8765"
DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../hko_weather_monitor/data/hko_weather.db"))

class ComprehensiveDashboardUAT(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.cursor = self.conn.cursor()

    def tearDown(self):
        self.conn.close()

    # === SUITE 1: LOGIC CORRECTNESS ===
    def test_logic_api_endpoints_health(self):
        for endpoint in ['/api/latest', '/api/polymarket', '/api/paper_trading', '/api/no_trading']:
            response = requests.get(f"{BASE_URL}{endpoint}", timeout=5)
            self.assertEqual(response.status_code, 200, f"Endpoint {endpoint} failed health check.")

    def test_logic_trading_signal_generation(self):
        try:
            self.cursor.execute("""
                SELECT condition_id, model_calculated_prob, polymarket_yes_price, generated_signal 
                FROM market_ticks ORDER BY tick_id DESC LIMIT 20
            """)
            ticks = self.cursor.fetchall()
            for row in ticks:
                cond_id, prob, price, signal = row
                if prob is not None and price is not None:
                    edge = prob - price
                    if edge > 0.10:
                        self.assertEqual(signal, 'BUY', f"Edge {edge:.2f} expected BUY for {cond_id}")
                    elif edge < -0.10:
                        self.assertEqual(signal, 'SELL', f"Edge {edge:.2f} expected SELL for {cond_id}")
                    else:
                        self.assertEqual(signal, 'HOLD', f"Edge {edge:.2f} expected HOLD for {cond_id}")
        except sqlite3.OperationalError as e:
            self.skipTest(f"market_ticks table not populated yet: {str(e)}")

    # === SUITE 2: FIGURES & NUMBERS ACCURACY ===
    def test_numerical_outlier_bounds(self):
        self.cursor.execute("SELECT name, temperature FROM readings JOIN stations ON readings.station_id = stations.id ORDER BY readings.id DESC LIMIT 50")
        readings = self.cursor.fetchall()
        for name, temp in readings:
            if temp is not None:
                self.assertTrue(-10.0 <= temp <= 50.0, f"Abnormal temperature reading: {temp}°C at {name}")

    def test_numerical_short_position_integrity(self):
        try:
            self.cursor.execute("SELECT qty FROM paper_positions WHERE side = 'NO' AND status = 'OPEN'")
            short_positions = self.cursor.fetchall()
            for (qty,) in short_positions:
                self.assertTrue(qty != 0, "Encountered empty allocation inside active short position ledger.")
        except sqlite3.OperationalError:
            self.skipTest("paper_positions ledger table not initialized.")

    # === SUITE 3: NAMES & LABELS ===
    def test_labels_forecast_station_codes(self):
        response = requests.get(f"{BASE_URL}/api/forecast_codes", timeout=5)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("HKO", data, "Canonical station code identifier 'HKO' missing from forecast definitions.")

    def test_labels_market_outcome_buckets(self):
        try:
            self.cursor.execute("SELECT DISTINCT outcome_name FROM market_outcomes LIMIT 10")
            outcomes = [r[0] for r in self.cursor.fetchall()]
            if outcomes:
                for label in outcomes:
                    self.assertTrue(any(c in label for c in ['°', '+', '-', 'or']), f"Label formatting error: {label}")
        except sqlite3.OperationalError:
            self.skipTest("market_outcomes mapping layer not fully seeded.")

    # === SUITE 4: UI/UX LAYOUT VERIFICATION ===
    def test_ui_dom_elements_and_tabs(self):
        response = requests.get(BASE_URL, timeout=5)
        self.assertEqual(response.status_code, 200)
        html = response.text
        
        required_keywords = [
            "HKO Regional Weather Monitor",
            "Observations",
            "Forecasts",
            "Polymarket",
            "Paper Trading",
            "NO Trading",
            'id="chart"'
        ]
        for keyword in required_keywords:
            self.assertIn(keyword, html, f"DOM Verification Failed: Missing element/keyword: '{keyword}'")

if __name__ == "__main__":
    unittest.main()
