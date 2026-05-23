#!/usr/bin/env python3
"""Automated UAT tests for HKO Weather Monitor frontend dashboard."""
import json
import sqlite3
import requests
import sys
from datetime import datetime

BASE = "http://localhost:8765"
DB = "/shared-hermes/hko-monitor/hko_weather_monitor/data/hko_weather.db"
PASS = 0
FAIL = 0

def ok(suite, name):
    global PASS; PASS += 1
    print(f"  [PASS] {suite}: {name}")

def fail(suite, name, reason):
    global FAIL; FAIL += 1
    print(f"  [FAIL] {suite}: {name} — {reason}")

def api(path):
    r = requests.get(f"{BASE}{path}", timeout=10)
    return r.status_code, r.json() if r.content else None

# ==========================================
print("=== SUITE 1: LOGIC CORRECTNESS (LOG) ===")
# ==========================================

for ep in ['/api/latest', '/api/polymarket', '/api/paper_trading', '/api/no_trading']:
    code, data = api(ep)
    if code == 200:
        ok("LOG", f"{ep} returns 200")
    else:
        fail("LOG", f"{ep}", f"status {code}")

try:
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT condition_id, model_calculated_prob, polymarket_yes_price, generated_signal FROM market_ticks ORDER BY tick_id DESC LIMIT 10").fetchall()
    for r in rows:
        cond, prob, price, signal = r
        if prob is not None and price is not None:
            edge = prob - price
            if edge > 0.10 and signal != 'BUY':
                fail("LOG", f"Signal {cond}", f"edge={edge:.4f} but signal={signal}, expected BUY")
            elif edge < -0.10 and signal != 'SELL':
                fail("LOG", f"Signal {cond}", f"edge={edge:.4f} but signal={signal}, expected SELL")
            elif -0.10 <= edge <= 0.10 and signal != 'HOLD':
                fail("LOG", f"Signal {cond}", f"edge={edge:.4f} but signal={signal}, expected HOLD")
            else:
                ok("LOG", f"Signal {cond}: edge={edge:.4f} -> {signal}")
    conn.close()
except Exception as e:
    fail("LOG", "Signal generation check", str(e))

code, data = api('/api/paper_trading')
if data and 'html' in data:
    ok("LOG", "Paper trading endpoint returns HTML")
else:
    fail("LOG", "Paper trading endpoint", "no html key")

# ==========================================
print("\n=== SUITE 2: NUMBERS ACCURACY (NUM) ===")
# ==========================================

code, latest = api('/api/latest')
if code == 200 and latest:
    ok("NUM", "/api/latest returns data, count=%d" % len(latest))
    if latest:
        keys = set(latest[0].keys())
        required = {'name', 'temperature'}
        if required.issubset(keys):
            ok("NUM", "Latest readings have required fields")
        else:
            fail("NUM", "Latest readings missing fields", f"got {keys}")
else:
    fail("NUM", "/api/latest", "no data")

try:
    conn = sqlite3.connect(DB)
    readings = conn.execute("SELECT station_id, temperature FROM readings ORDER BY id DESC LIMIT 5").fetchall()
    if readings:
        ok("NUM", f"DB has {len(readings)} recent readings")
        for r in readings:
            if r[1] is not None and (r[1] < -10 or r[1] > 50):
                fail("NUM", f"Temperature outlier: station {r[0]} = {r[1]}°C")
    conn.close()
except Exception as e:
    fail("NUM", "DB readings check", str(e))

try:
    conn = sqlite3.connect(DB)
    short_pos = conn.execute("SELECT id, condition_id, token_id, qty, status FROM paper_positions WHERE qty < 0 AND status != 'CLOSED'").fetchall()
    pos_count = len(short_pos)
    ok("NUM", f"Short positions queryable: {pos_count} found")
    conn.close()
except Exception as e:
    fail("NUM", "Short positions check", str(e))

# ==========================================
print("\n=== SUITE 3: NAMES/LABELS (NAM) ===")
# ==========================================

code, data = api('/api/forecast_codes')
if code == 200 and data:
    ok("NAM", f"Forecast codes: {data}")
else:
    fail("NAM", "Forecast codes", f"status {code}")

try:
    conn = sqlite3.connect(DB)
    outcomes = conn.execute("SELECT DISTINCT condition_id FROM market_outcomes LIMIT 5").fetchall()
    if outcomes:
        ok("NAM", f"Market outcomes present: {len(outcomes)} conditions")
    else:
        fail("NAM", "Market outcomes", "table empty")
    conn.close()
except Exception as e:
    fail("NAM", "Market outcomes check", str(e))

# ==========================================
print("\n=== SUITE 4: UI/UX (UI) ===")
# ==========================================

code, body = requests.get(BASE, timeout=10).status_code, requests.get(BASE, timeout=10).text
if code == 200:
    ok("UI", "Main page returns 200")
    checks = [
        ('HKO', 'HKO Regional Weather Monitor title'),
        ('Observations', 'Observations tab'),
        ('Forecasts', 'Forecasts tab'),
        ('Polymarket', 'Polymarket tab'),
        ('Paper Trading', 'Paper Trading tab'),
        ('NO Trading', 'NO Trading tab'),
        ('id="chart"', 'Chart canvas element'),
    ]
    for needle, label in checks:
        if needle in body:
            ok("UI", f"Page contains: {label}")
        else:
            fail("UI", f"Page missing: {label}", "")
else:
    fail("UI", "Main page", f"status {code}")

if 'obs-status' in body:
    fail("UI", "Bug 1 fix: obs-status still in JS", "should be 'status'")
else:
    ok("UI", "Bug 1 fix: obs-status removed from JS")

if 'renderChart(history, true)' in body:
    ok("UI", "Bug 3 fix: renderChart keeps station selection")
else:
    fail("UI", "Bug 3 fix: renderChart missing keepSelect", "")

if 'loadTable(true)' not in body or 'loadTable()' in body.split('currentSubTab === \'table\'')[1].split(';')[0]:
    ok("UI", "Bug 2 fix: loadTable no longer resets on refresh")
else:
    fail("UI", "Bug 2 fix: loadTable still resets", "")

# ==========================================
print("\n=== SUMMARY ===")
print(f"  PASSED: {PASS}")
print(f"  FAILED: {FAIL}")
print(f"  TOTAL:  {PASS + FAIL}")
sys.exit(0 if FAIL == 0 else 1)
