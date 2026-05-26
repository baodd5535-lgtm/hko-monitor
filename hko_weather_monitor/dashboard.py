"""HTTP API and dashboard for HKO weather data + forecasts."""
import json
import sqlite3
import http.server
import socketserver
import threading
from urllib.parse import urlparse, parse_qs
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hko_weather_monitor.db import (
    init_db, get_latest_readings, get_all_history, get_all_stations,
    get_history_table,
    get_latest_forecasts_hourly, get_latest_forecasts_daily,
    get_forecast_station_codes,
    DB_PATH,
)
from hko_weather_monitor.main import poll_once, poll_forecasts
from hko_weather_monitor.fetcher import (
    FORECAST_STATION_NAMES, WEATHER_CODES,
)
from hko_weather_monitor.polymarket import (
    fetch_hk_polymarket, fetch_active_hk_polymarket, compute_expected_temp,
)

PORT = 8765

# Forecast station selector mapping
FORECAST_STN_MAP = {
    "HK Observatory": "HKO", "Chek Lap Kok": "HKA", "Sha Tin": "SHA",
    "Shek Kong": "SKG", "Lau Fau Shan": "LFS", "Ta Kwu Ling": "TKL",
    "Cheung Chau": "CCH", "Peng Chau": "PEN", "Waglan Island": "WGL",
    "HK Park": "HKS", "Kai Tak": "JKB", "Sheung Shui": "SEK",
    "Tseung Kwan O": "TPO", "Tuen Mun": "TUN", "Tai Mei Tuk": "TY1",
    "Sha Tau Kok": "SSH",
}

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HKO Weather Monitor</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0f;
            color: #e0e0e0;
            padding: 20px;
        }
        h1 {
            text-align: center;
            margin-bottom: 20px;
            color: #00d4ff;
            font-size: 24px;
        }
        .status {
            text-align: center;
            color: #888;
            margin-bottom: 20px;
            font-size: 14px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 12px;
            margin-bottom: 30px;
        }
        .card {
            background: #1a1a2e;
            border-radius: 8px;
            padding: 15px;
            border: 1px solid #2a2a3e;
            transition: border-color 0.3s;
        }
        .card:hover { border-color: #00d4ff; }
        .card .station {
            font-size: 14px;
            color: #aaa;
            margin-bottom: 8px;
        }
        .card .temp {
            font-size: 32px;
            font-weight: bold;
        }
        .card .details {
            font-size: 12px;
            color: #888;
            margin-top: 4px;
        }
        .card .details span { margin-right: 10px; }
        .card .time {
            font-size: 12px;
            color: #666;
            margin-top: 5px;
        }
        .temp-low { color: #4fc3f7; }
        .temp-mid { color: #ffb74d; }
        .temp-high { color: #ff7043; }
        canvas {
            width: 100%;
            height: 400px;
            background: #0d0d1a;
            border-radius: 8px;
            border: 1px solid #2a2a3e;
        }
        .controls {
            text-align: center;
            margin: 20px 0;
        }
        select, button {
            background: #1a1a2e;
            color: #e0e0e0;
            border: 1px solid #2a2a3e;
            padding: 8px 16px;
            border-radius: 4px;
            margin: 0 5px;
            cursor: pointer;
        }
        button:hover { border-color: #00d4ff; }
        .tabs {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .tab {
            background: #1a1a2e;
            color: #888;
            border: 1px solid #2a2a3e;
            padding: 8px 24px;
            border-radius: 4px;
            cursor: pointer;
        }
        .tab.active { color: #00d4ff; border-color: #00d4ff; }
        .table-wrap {
            max-height: 500px;
            overflow-y: auto;
            border-radius: 8px;
            border: 1px solid #2a2a3e;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        thead {
            position: sticky;
            top: 0;
            z-index: 1;
        }
        th {
            background: #1a1a2e;
            color: #00d4ff;
            padding: 10px 15px;
            text-align: left;
            border-bottom: 2px solid #2a2a3e;
        }
        td {
            padding: 6px 15px;
            border-bottom: 1px solid #1a1a2e;
        }
        tr:hover { background: #1a1a2e; }
        .rh-cell { color: #4fc3f7; }
        .wind-cell { color: #a5d6a7; }
        .loading-row td {
            text-align: center;
            color: #666;
            padding: 20px;
        }
        .forecast-section {
            background: #0d0d1a;
            border-radius: 8px;
            border: 1px solid #2a2a3e;
            padding: 15px;
            margin-bottom: 20px;
        }
        .forecast-section h3 {
            color: #00d4ff;
            margin-bottom: 10px;
            font-size: 16px;
        }
        .hourly-scroll {
            display: flex;
            overflow-x: auto;
            gap: 8px;
            padding: 10px 0;
        }
        .hourly-card {
            flex: 0 0 80px;
            background: #1a1a2e;
            border-radius: 6px;
            padding: 10px;
            text-align: center;
            border: 1px solid #2a2a3e;
        }
        .hourly-card .hour { font-size: 11px; color: #888; }
        .hourly-card .wx-icon { font-size: 20px; margin: 4px 0; }
        .hourly-card .h-temp { font-size: 14px; font-weight: bold; }
        .hourly-card .h-details { font-size: 10px; color: #888; margin-top: 2px; }
        .daily-table { width: 100%; }
        .daily-table td, .daily-table th { padding: 8px 10px; text-align: center; }
        .daily-table .d-day { text-align: left; }
        .wx-sunny { color: #ffb74d; }
        .wx-cloudy { color: #90a4ae; }
        .wx-rain { color: #4fc3f7; }
        .wx-thunder { color: #ce93d8; }
        .wx-haze { color: #bcaaa4; }
        .model-time { font-size: 11px; color: #555; margin-left: 10px; }

        /* Polymarket styles */
        .pm-section {
            background: #0d0d1a;
            border-radius: 8px;
            border: 1px solid #2a2a3e;
            padding: 15px;
            margin-bottom: 20px;
        }
        .pm-section h3 { color: #00d4ff; margin-bottom: 10px; font-size: 16px; }
        .pm-section h4 { color: #aaa; margin-bottom: 8px; font-size: 14px; }
        .pm-bar-container { display: flex; align-items: center; margin: 4px 0; gap: 8px; }
        .pm-bar-label { width: 70px; text-align: right; font-size: 13px; color: #ccc; flex-shrink: 0; }
        .pm-bar-track { flex: 1; height: 24px; background: #1a1a2e; border-radius: 4px; overflow: hidden; position: relative; }
        .pm-bar-fill { height: 100%; border-radius: 4px; transition: width 0.3s; min-width: 2px; }
        .pm-bar-pct { width: 55px; text-align: left; font-size: 13px; color: #fff; flex-shrink: 0; font-weight: bold; }
        .pm-bar-vol { width: 80px; text-align: left; font-size: 11px; color: #888; flex-shrink: 0; }
        .pm-summary { display: flex; gap: 20px; flex-wrap: wrap; margin: 10px 0; }
        .pm-stat { background: #1a1a2e; padding: 10px 16px; border-radius: 6px; border: 1px solid #2a2a3e; }
        .pm-stat-label { font-size: 11px; color: #888; margin-bottom: 4px; }
        .pm-stat-value { font-size: 20px; font-weight: bold; }
        .pm-stat-value.market-temp { color: #ff7043; }
        .pm-stat-value.hko-temp { color: #4fc3f7; }
        .pm-stat-value.forecast-temp { color: #ffb74d; }
        .pm-link { display: inline-block; margin-top: 8px; color: #00d4ff; text-decoration: none; font-size: 13px; }
        .pm-link:hover { text-decoration: underline; }
        .pm-multi-event { margin-bottom: 20px; }
        .pm-event-title { color: #ffb74d; font-size: 14px; margin-bottom: 5px; font-weight: bold; }
    </style>
</head>
<body>
    <h1>HKO Regional Weather Monitor</h1>
    <div class="status" id="status">Loading...</div>

    <!-- Main tabs -->
    <div class="tabs">
        <div class="tab active" id="tab-obs" onclick="switchMainTab('obs')">Observations</div>
        <div class="tab" id="tab-forecast" onclick="switchMainTab('forecast')">Forecasts</div>
        <div class="tab" id="tab-polymarket" onclick="switchMainTab('polymarket')">Polymarket</div>
        <div class="tab" id="tab-trading" onclick="switchMainTab('trading')">Paper Trading</div>
        <div class="tab" id="tab-no-trading" onclick="switchMainTab('no-trading')">NO Trading</div>
    </div>

    <!-- Observations view -->
    <div id="view-obs">
        <div class="grid" id="grid"></div>
        <div class="controls">
            <select id="station-select"><option>Loading...</option></select>
            <button onclick="refresh()">Refresh</button>
            <button onclick="poll()">Fetch Now</button>
            <button onclick="pollForecasts()">Fetch Forecasts</button>
        </div>
        <div class="tabs">
            <div class="tab active" id="tab-chart" onclick="switchSubTab('chart')">Chart</div>
            <div class="tab" id="tab-table" onclick="switchSubTab('table')">Table</div>
        </div>
        <div id="view-chart"><canvas id="chart"></canvas></div>
        <div id="view-table" style="display:none">
            <div class="table-wrap" id="table-wrap">
                <table>
                    <thead><tr><th>Time</th><th>Temperature °C</th><th>Humidity %</th></tr></thead>
                    <tbody id="table-body"></tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Forecast view -->
    <div id="view-forecast" style="display:none">
        <div class="controls">
            <select id="forecast-station-select">
                <option value="HKO">HK Observatory</option>
            </select>
        </div>
        <div class="forecast-section">
            <h3>Hourly Forecast <span class="model-time" id="model-time-hourly"></span></h3>
            <div class="hourly-scroll" id="hourly-scroll"></div>
        </div>
        <div class="forecast-section">
            <h3>10-Day Forecast <span class="model-time" id="model-time-daily"></span></h3>
            <table class="daily-table" id="daily-table">
                <thead>
                    <tr>
                        <th class="d-day">Day</th><th>Weather</th>
                        <th>Max °C</th><th>Min °C</th><th>Rain %</th>
                    </tr>
                </thead>
                <tbody id="daily-body"></tbody>
            </table>
        </div>
    </div>

    <!-- Polymarket view -->
    <div id="view-polymarket" style="display:none">
        <div class="pm-section">
            <h3>HK Temperature Markets <a class="pm-link" id="pm-link" href="#" target="_blank">View on Polymarket ↗</a></h3>
            <div id="pm-loading" style="color:#888;padding:20px;text-align:center;">Loading markets...</div>
            <div id="pm-content" style="display:none">
                <!-- Summary stats -->
                <div class="pm-summary" id="pm-summary"></div>
                <!-- Multiple events -->
                <div id="pm-events"></div>
                <!-- 9-day forecast from HKO -->
                <div id="pm-nine-day" style="margin-top:24px;padding-top:16px;border-top:1px solid #333">
                    <h4 style="color:#ccc;margin-bottom:12px">HKO 9-Day Forecast</h4>
                    <div id="pm-nine-day-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- Paper Trading view -->
    <div id="view-trading" style="display:none">
        <div class="pm-section">
            <h3>Paper Trading Account</h3>
            <div id="trading-content">
                <div id="trading-balance" style="margin-bottom:20px;padding:15px;background:#1a1a2e;border:1px solid #333;border-radius:8px">
                    <div style="color:#888;font-size:14px">Available Balance</div>
                    <div style="font-size:28px;color:#4caf50" id="trading-balance-amount">$10,000.00</div>
                </div>
                
                <div id="trading-positions" style="margin-bottom:20px">
                    <h4 style="color:#ccc;margin-bottom:12px">Current Positions</h4>
                    <table style="width:100%;border-collapse:collapse">
                        <thead>
                            <tr style="border-bottom:1px solid #333">
                                <th style="text-align:left;padding:8px;color:#888">Market</th>
                                <th style="text-align:left;padding:8px;color:#888">Side</th>
                                <th style="text-align:right;padding:8px;color:#888">Qty</th>
                                <th style="text-align:right;padding:8px;color:#888">Avg Price</th>
                                <th style="text-align:right;padding:8px;color:#888">P&L</th>
                            </tr>
                        </thead>
                        <tbody id="positions-body">
                        </tbody>
                    </table>
                </div>
                
                <div id="trading-fills" style="margin-bottom:20px">
                    <h4 style="color:#ccc;margin-bottom:12px">Recent Fills</h4>
                    <table style="width:100%;border-collapse:collapse">
                        <thead>
                            <tr style="border-bottom:1px solid #333">
                                <th style="text-align:left;padding:8px;color:#888">Time</th>
                                <th style="text-align:left;padding:8px;color:#888">Market</th>
                                <th style="text-align:left;padding:8px;color:#888">Side</th>
                                <th style="text-align:right;padding:8px;color:#888">Qty</th>
                                <th style="text-align:right;padding:8px;color:#888">Price</th>
                                <th style="text-align:right;padding:8px;color:#888">Slippage</th>
                            </tr>
                        </thead>
                        <tbody id="fills-body">
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <!-- NO Trading Engine view -->
    <div id="view-no-trading" style="display:none">
        <div class="pm-section">
            <h3>NO Trading Engine <span id="engine-status-badge" style="font-size:12px;padding:3px 10px;border-radius:12px;background:#333;color:#888;">Initializing...</span></h3>
            
            <!-- Engine Status Cards -->
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:20px" id="engine-cards">
            </div>
            
            <!-- Active Triggers -->
            <div style="margin-bottom:20px">
                <h4 style="color:#ccc;margin-bottom:12px">
                    🔥 Trigger Log
                    <span style="font-size:11px;color:#666;font-weight:normal">(last 50 events)</span>
                </h4>
                <div id="trigger-log" style="max-height:300px;overflow-y:auto;background:#12121f;border:1px solid #2a2a3e;border-radius:8px;padding:10px">
                    <div style="color:#666;text-align:center;padding:20px">Waiting for triggers...</div>
                </div>
            </div>
            
            <!-- Multi-Factor Adjustments -->
            <div style="margin-bottom:20px">
                <h4 style="color:#ccc;margin-bottom:12px">🌡️ Multi-Factor Temperature Adjustments</h4>
                <table style="width:100%;border-collapse:collapse" id="no-factor-table">
                    <thead>
                        <tr style="border-bottom:1px solid #333">
                            <th style="text-align:left;padding:8px;color:#888">Date</th>
                            <th style="text-align:right;padding:8px;color:#888">Raw HKO</th>
                            <th style="text-align:right;padding:8px;color:#888">Cloud</th>
                            <th style="text-align:right;padding:8px;color:#888">Wind</th>
                            <th style="text-align:right;padding:8px;color:#888">Humidity</th>
                            <th style="text-align:right;padding:8px;color:#888">Adjusted</th>
                            <th style="text-align:left;padding:8px;color:#888">Factors</th>
                        </tr>
                    </thead>
                    <tbody id="no-factor-body"></tbody>
                </table>
            </div>

            <!-- Live Scoring Decisions -->
            <div style="margin-bottom:20px">
                <h4 style="color:#ccc;margin-bottom:12px">🎯 Live Scoring Decisions Log</h4>
                <table style="width:100%;border-collapse:collapse" id="scoring-log-table">
                    <thead>
                        <tr style="border-bottom:1px solid #333">
                            <th style="text-align:left;padding:8px;color:#888">Market ID</th>
                            <th style="text-align:left;padding:8px;color:#888">Bucket</th>
                            <th style="text-align:right;padding:8px;color:#888">Model Prob</th>
                            <th style="text-align:right;padding:8px;color:#888">Market Price</th>
                            <th style="text-align:right;padding:8px;color:#888">Edge</th>
                            <th style="text-align:left;padding:8px;color:#888">Decision</th>
                            <th style="text-align:left;padding:8px;color:#888">Rationale</th>
                        </tr>
                    </thead>
                    <tbody id="scoring-log-body"></tbody>
                </table>
            </div>

            <!-- Active Market Maker Orders -->
            <div style="margin-bottom:20px">
                <h4 style="color:#ccc;margin-bottom:12px">🤖 Active Market Maker Limit Orders</h4>
                <table style="width:100%;border-collapse:collapse" id="maker-orders-table">
                    <thead>
                        <tr style="border-bottom:1px solid #333">
                            <th style="text-align:left;padding:8px;color:#888">Market ID</th>
                            <th style="text-align:left;padding:8px;color:#888">Bucket</th>
                            <th style="text-align:left;padding:8px;color:#888">Side</th>
                            <th style="text-align:right;padding:8px;color:#888">Quote Price</th>
                            <th style="text-align:right;padding:8px;color:#888">Size</th>
                            <th style="text-align:right;padding:8px;color:#888">Fair Value</th>
                            <th style="text-align:left;padding:8px;color:#888">Status</th>
                        </tr>
                    </thead>
                    <tbody id="maker-orders-body"></tbody>
                </table>
            </div>

            <!-- NO Positions -->
            <div style="margin-bottom:20px">
                <h4 style="color:#ccc;margin-bottom:12px">📉 NO Positions (Short YES) — All</h4>
                <table style="width:100%;border-collapse:collapse" id="no-positions-table">
                    <thead>
                        <tr style="border-bottom:1px solid #333">
                            <th style="text-align:left;padding:8px;color:#888">Market</th>
                            <th style="text-align:left;padding:8px;color:#888">Bucket</th>
                            <th style="text-align:right;padding:8px;color:#888">Qty</th>
                            <th style="text-align:right;padding:8px;color:#888">Entry Price</th>
                            <th style="text-align:right;padding:8px;color:#888">Current Price</th>
                            <th style="text-align:right;padding:8px;color:#888">P&L</th>
                            <th style="text-align:left;padding:8px;color:#888">Status</th>
                            <th style="text-align:left;padding:8px;color:#888">Trigger</th>
                        </tr>
                    </thead>
                    <tbody id="no-positions-body"></tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('chart').getContext('2d');
        let currentMainTab = 'obs';
        let currentSubTab = 'chart';
        let tableOffset = 0;
        let tableLoading = false;
        let tableExhausted = false;
        const TABLE_LIMIT = 50;
        let forecastStationCodes = [];

        // Weather emoji mapping
        const WX_ICONS = {
            0: '☀️', 1: '🌤️', 2: '⛅', 3: '☁️',
            50: '🌤️', 51: '☀️', 52: '🌤️', 53: '☀️', 54: '🌤️',
            60: '☁️', 61: '☁️', 62: '🌦️', 63: '🌧️', 64: '🌧️',
            71: '🌧️', 72: '⛈️', 73: '⛈️', 74: '⛈️', 76: '⛈️',
            81: '🌫️', 82: '🌫️', 83: '🌫️',
        };

        const WX_CLASSES = {
            0: 'wx-sunny', 1: 'wx-sunny', 2: 'wx-cloudy', 3: 'wx-cloudy',
            50: 'wx-sunny', 51: 'wx-sunny', 52: 'wx-sunny', 53: 'wx-sunny', 54: 'wx-sunny',
            60: 'wx-cloudy', 61: 'wx-cloudy', 62: 'wx-rain', 63: 'wx-rain', 64: 'wx-rain',
            71: 'wx-rain', 72: 'wx-thunder', 73: 'wx-thunder', 74: 'wx-thunder', 76: 'wx-thunder',
            81: 'wx-haze', 82: 'wx-haze', 83: 'wx-haze',
        };

        function tempColor(t) {
            if (t == null) return '#666';
            if (t <= 23) return '#4fc3f7';
            if (t <= 28) return '#ffb74d';
            return '#ff7043';
        }
        function switchMainTab(tab) {
            currentMainTab = tab;
            document.getElementById('tab-obs').classList.toggle('active', tab === 'obs');
            document.getElementById('tab-forecast').classList.toggle('active', tab === 'forecast');
            document.getElementById('tab-polymarket').classList.toggle('active', tab === 'polymarket');
            document.getElementById('tab-trading').classList.toggle('active', tab === 'trading');
            document.getElementById('tab-no-trading').classList.toggle('active', tab === 'no-trading');

            document.getElementById('view-obs').style.display = tab === 'obs' ? '' : 'none';
            document.getElementById('view-forecast').style.display = tab === 'forecast' ? '' : 'none';
            document.getElementById('view-polymarket').style.display = tab === 'polymarket' ? '' : 'none';
            document.getElementById('view-trading').style.display = tab === 'trading' ? '' : 'none';
            document.getElementById('view-no-trading').style.display = tab === 'no-trading' ? '' : 'none';

            if (tab === 'forecast') loadForecasts();
            if (tab === 'polymarket') loadPolymarket();
            if (tab === 'trading') loadPaperTrading();
            if (tab === 'no-trading') loadNoTrading();
        }

        // Convert UTC datetime string to HKT (UTC+8)
        // Input: '2026-05-20 10:51:13' (UTC) or ISO format
        // Output: '2026-05-20 18:51' (HKT)
        function utcToHkt(ts) {
            if (!ts) return '';
            try {
                // Parse UTC datetime
                const d = new Date(ts.replace(' ', 'T') + 'Z');
                if (isNaN(d.getTime())) return ts; // not a valid datetime, return as-is
                // Convert to HKT
                const hkt = new Date(d.getTime() + 8 * 3600 * 1000);
                return hkt.getFullYear() + '/' +
                    String(hkt.getMonth() + 1).padStart(2, '0') + '/' +
                    String(hkt.getDate()).padStart(2, '0') + ' ' +
                    String(hkt.getHours()).padStart(2, '0') + ':' +
                    String(hkt.getMinutes()).padStart(2, '0');
            } catch {
                return ts;
            }
        }

        function switchSubTab(tab) {
            currentSubTab = tab;
            document.getElementById('tab-chart').classList.toggle('active', tab === 'chart');
            document.getElementById('tab-table').classList.toggle('active', tab === 'table');
            document.getElementById('view-chart').style.display = tab === 'chart' ? '' : 'none';
            document.getElementById('view-table').style.display = tab === 'table' ? '' : 'none';
            if (tab === 'table' && tableOffset === 0) loadTable();
        }

        function renderCards(readings) {
            const grid = document.getElementById('grid');
            grid.innerHTML = readings.map(r => `
                <div class="card">
                    <div class="station">${r.name}</div>
                    <div class="temp" style="color:${tempColor(r.temperature)}">${r.temperature != null ? r.temperature.toFixed(1) : 'N/A'}°C</div>
                    <div class="details">
                        <span>💧 ${r.humidity != null ? r.humidity + '%' : 'N/A'}</span>
                    </div>
                    <div class="time">${r.recorded_at} HKT</div>
                </div>
            `).join('');
        }

        function renderChart(history, keepSelect) {
            if (!history.length) return;

            if (!keepSelect) {
                const stations = [...new Set(history.map(r => r.name))];
                const select = document.getElementById('station-select');
                select.innerHTML = stations.map(s => `<option>${s}</option>`).join('');
                const sel = stations.find(s => s === 'HK Observatory') || stations[0];
                select.value = sel;
            }

            const sel = document.getElementById('station-select').value;
            const data = history.filter(r => r.name === sel);
            if (data.length < 2) return;

            const canvas = document.getElementById('chart');
            if (canvas.width !== canvas.offsetWidth || canvas.height !== 400) {
                canvas.width = canvas.offsetWidth;
                canvas.height = 400;
            }
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            const temps = data.map(r => r.temperature).filter(t => t != null);
            if (!temps.length) return;
            const minT = Math.floor(Math.min(...temps) - 1);
            const maxT = Math.ceil(Math.max(...temps) + 1);
            const range = maxT - minT || 1;

            const w = canvas.width;
            const h = canvas.height;
            const pad = { top: 30, bottom: 40, left: 50, right: 20 };
            const cw = w - pad.left - pad.right;
            const ch = h - pad.top - pad.bottom;

            ctx.strokeStyle = '#2a2a3e';
            ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i++) {
                const y = pad.top + (ch * i / 4);
                ctx.beginPath();
                ctx.moveTo(pad.left, y);
                ctx.lineTo(w - pad.right, y);
                ctx.stroke();
                ctx.fillStyle = '#666';
                ctx.font = '12px sans-serif';
                ctx.fillText((maxT - (range * i / 4)).toFixed(1) + '°', 10, y + 4);
            }

            ctx.strokeStyle = tempColor(data[data.length-1].temperature);
            ctx.lineWidth = 2;
            ctx.beginPath();
            let started = false;
            data.forEach((r, i) => {
                if (r.temperature == null) return;
                const x = pad.left + (i / (data.length - 1)) * cw;
                const y = pad.top + ch - ((r.temperature - minT) / range) * ch;
                if (!started) { ctx.moveTo(x, y); started = true; }
                else ctx.lineTo(x, y);
            });
            ctx.stroke();

            ctx.fillStyle = tempColor(data[data.length-1].temperature);
            data.forEach((r, i) => {
                if (r.temperature == null) return;
                const x = pad.left + (i / (data.length - 1)) * cw;
                const y = pad.top + ch - ((r.temperature - minT) / range) * ch;
                ctx.beginPath();
                ctx.arc(x, y, 3, 0, Math.PI * 2);
                ctx.fill();
            });

            ctx.fillStyle = '#666';
            ctx.font = '10px sans-serif';
            const step = Math.max(1, Math.floor(data.length / 6));
            data.forEach((r, i) => {
                if (i % step === 0) {
                    const x = pad.left + (i / (data.length - 1)) * cw;
                    ctx.fillText(r.recorded_at.slice(11, 16) + ' HKT', x, h - 10);
                }
            });

            ctx.fillStyle = '#e0e0e0';
            ctx.font = '14px sans-serif';
            ctx.fillText(sel + ' - Temperature History', pad.left, 20);
        }

        // ─── Forecast rendering ──────────────────────────

        function renderHourlyForecast(data, modelTime) {
            const container = document.getElementById('hourly-scroll');
            if (!data.length) {
                container.innerHTML = '<div style="color:#666;padding:20px;">No forecast data</div>';
                return;
            }

            const modelTimeStr = formatModelTime(modelTime);
            document.getElementById('model-time-hourly').textContent =
                modelTimeStr ? `Base: ${modelTimeStr}` : '';

            // Show next 24 hours
            const hours = data.slice(0, 24);
            container.innerHTML = hours.map(h => {
                const hourStr = formatHour(h.forecast_hour);
                // Derive weather icon from humidity and temperature (no weather_code in hourly)
                let icon = '🌤️';  // default: partly cloudy
                if (h.humidity && h.humidity > 80) {
                    icon = h.temperature > 25 ? '🌧️' : '🌦️';  // humid + warm = rain
                } else if (h.humidity && h.humidity > 65) {
                    icon = '☁️';  // cloudy
                } else if (h.temperature > 31) {
                    icon = '☀️';  // hot/sunny
                } else if (h.temperature < 20) {
                    icon = '🌥️';  // cool
                }
                const wxClass = h.wind_speed > 5 ? 'wind-cell' : '';
                return `
                    <div class="hourly-card">
                        <div class="hour">${hourStr} HKT</div>
                        <div class="wx-icon">${icon}</div>
                        <div class="h-temp" style="color:${tempColor(h.temperature)}">${h.temperature != null ? h.temperature.toFixed(1) : '?'}</div>
                        <div class="h-details">
                            💧${h.humidity != null ? Math.round(h.humidity) : '?'}%
                            ${h.wind_speed != null ? '<br>🌊' + h.wind_speed.toFixed(1) + 'm/s' : ''}
                        </div>
                    </div>
                `;
            }).join('');
        }

        function renderDailyForecast(data, modelTime) {
            const tbody = document.getElementById('daily-body');
            if (!data.length) {
                tbody.innerHTML = '<tr><td colspan="5">No forecast data</td></tr>';
                return;
            }

            const modelTimeStr = formatModelTime(modelTime);
            document.getElementById('model-time-daily').textContent =
                modelTimeStr ? `Base: ${modelTimeStr}` : '';

            const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

            tbody.innerHTML = data.map(d => {
                const date = new Date(parseInt(d.forecast_date.substring(0, 4)),
                    parseInt(d.forecast_date.substring(4, 6)) - 1,
                    parseInt(d.forecast_date.substring(6, 8)));
                const dayName = dayNames[date.getDay()];
                const dateStr = `${date.getMonth()+1}/${date.getDate()}`;
                const icon = WX_ICONS[d.weather_code] || '🌡️';
                const wxClass = WX_CLASSES[d.weather_code] || 'wx-cloudy';

                return `
                    <tr>
                        <td class="d-day">${dateStr} ${dayName}</td>
                        <td class="${wxClass}">${icon}</td>
                        <td style="color:#ff7043">${d.max_temperature != null ? d.max_temperature.toFixed(1) : '?'}</td>
                        <td style="color:#4fc3f7">${d.min_temperature != null ? d.min_temperature.toFixed(1) : '?'}</td>
                        <td>${d.chance_of_rain || '?'}</td>
                    </tr>
                `;
            }).join('');
        }

        function formatHour(hourStr) {
            if (!hourStr || hourStr.length < 10) return '?';
            const yyyy = hourStr.substring(0, 4);
            const mm = hourStr.substring(4, 6);
            const dd = hourStr.substring(6, 8);
            const hh = hourStr.substring(8, 10);
            return `${mm}/${dd} ${hh}:00`;
        }

        function formatModelTime(mt) {
            if (!mt || mt.length < 8) return '';
            const yyyy = mt.substring(0, 4);
            const mm = mt.substring(4, 6);
            const dd = mt.substring(6, 8);
            const hh = mt.substring(8, 10) || '00';
            return `${yyyy}/${mm}/${dd} ${hh}:00 HKT`;
        }

        async function loadForecasts(stationCode) {
            const code = stationCode || document.getElementById('forecast-station-select').value;
            try {
                const [hourly, daily] = await Promise.all([
                    fetch(`/api/forecast_hourly?station=${code}`).then(r => r.json()),
                    fetch(`/api/forecast_daily?station=${code}`).then(r => r.json()),
                ]);
                const modelTime = (hourly[0] && hourly[0].model_time) ||
                                   (daily[0] && daily[0].model_time) || '';
                renderHourlyForecast(hourly, modelTime);
                renderDailyForecast(daily, modelTime);
            } catch (e) {
                console.error('Forecast load failed:', e);
            }
        }

        async function loadTable(reset = false) {
            if (tableLoading || (tableExhausted && !reset)) return;
            if (reset) { tableOffset = 0; tableExhausted = false; }
            tableLoading = true;

            const station = document.getElementById('station-select').value;
            try {
                const data = await fetch(
                    `/api/table?station=${encodeURIComponent(station)}&offset=${tableOffset}&limit=${TABLE_LIMIT}`
                ).then(r => r.json());

                const tbody = document.getElementById('table-body');
                if (reset) tbody.innerHTML = '';

                if (data.length === 0) {
                    tableExhausted = true;
                    if (reset) {
                        tbody.innerHTML = '<tr class="loading-row"><td colspan="3">No data available</td></tr>';
                    }
                } else {
                    data.forEach(r => {
                        const tr = document.createElement('tr');
                        tr.innerHTML = `
                            <td>${r.recorded_at} HKT</td>
                            <td style="color:${tempColor(r.temperature)}">${r.temperature != null ? r.temperature.toFixed(1) : 'N/A'}</td>
                            <td class="rh-cell">${r.humidity != null ? r.humidity : 'N/A'}</td>
                        `;
                        tbody.appendChild(tr);
                    });
                    tableOffset += data.length;
                    if (data.length < TABLE_LIMIT) tableExhausted = true;
                }
            } catch (e) {
                console.error('Table load failed:', e);
            }
            tableLoading = false;
        }

        document.getElementById('table-wrap').addEventListener('scroll', function() {
            if (this.scrollTop + this.clientHeight >= this.scrollHeight - 50) {
                loadTable();
            }
        });

        // ─── Polymarket ────────────────────────────────

        function renderPolymarketBar(outcome) {
            const pct = outcome.yes_price || 0;
            const temp = outcome.temp || '?';
            const vol = outcome.volume || 0;
            const color = tempColor(parseFloat(temp.replace(/[^\d]/g, '')) || 25);
            return `
                <div class="pm-bar-container">
                    <div class="pm-bar-label">${temp}°C</div>
                    <div class="pm-bar-track">
                        <div class="pm-bar-fill" style="width:${pct}%;background:${color}"></div>
                    </div>
                    <div class="pm-bar-pct">${pct.toFixed(1)}%</div>
                    <div class="pm-bar-vol">$${vol >= 1000 ? (vol/1000).toFixed(1)+'K' : vol.toFixed(0)}</div>
                </div>
            `;
        }

        function renderPolymarketEvent(event, hkoTemp, forecastData, marketDate) {
            const outcomes = event.outcomes || [];
            const est = event.expected_temp || { lower_bound: null, mode: '?', mode_pct: 0 };
            const displayDate = marketDate ? marketDate.slice(0,4)+'-'+marketDate.slice(4,6)+'-'+marketDate.slice(6,8) : '';

            // NOTE: Categorical markets with open-ended buckets (e.g. '30+')
            // cannot yield a true expected value without distribution assumptions.
            // We show: the MODE (most likely bucket) + lower-bound estimate.
            let marketLabel;
            if (est.mode_pct > 80) {
                // Dominant outcome — show it directly
                marketLabel = `${est.mode}°C <span style="font-size:11px;color:#888">(${est.mode_pct}% prob)</span>`;
            } else {
                // Distributed — show lower bound with mode
                marketLabel = `≥${est.lower_bound}°C <span style="font-size:11px;color:#888">mode: ${est.mode}°C ${est.mode_pct}%</span>`;
            }

            let summaryHtml = `
                <div class="pm-stat">
                    <div class="pm-stat-label">Market Implied</div>
                    <div class="pm-stat-value market-temp">${marketLabel}</div>
                </div>
            `;
            if (hkoTemp != null) {
                summaryHtml += `
                    <div class="pm-stat">
                        <div class="pm-stat-label">HKO Current</div>
                        <div class="pm-stat-value hko-temp">${hkoTemp.toFixed(1)}°C</div>
                    </div>
                `;
            }
            if (forecastData) {
                summaryHtml += `
                    <div class="pm-stat">
                        <div class="pm-stat-label">HKO Forecast Max (${displayDate})</div>
                        <div class="pm-stat-value forecast-temp">${(forecastData.max_temp || 0).toFixed(1)}°C</div>
                    </div>
                    <div class="pm-stat">
                        <div class="pm-stat-label">Wind (avg/max)</div>
                        <div class="pm-stat-value" style="color:#a5d6a7;font-size:16px">
                            ${forecastData.wind_dir || '?'} ${forecastData.wind_avg != null ? forecastData.wind_avg.toFixed(1)+' / ' : ''}${forecastData.wind_max != null ? forecastData.wind_max.toFixed(1) : ''} m/s
                        </div>
                    </div>
                    ${forecastData.rain_chance ? `
                    <div class="pm-stat">
                        <div class="pm-stat-label">Rain</div>
                        <div class="pm-stat-value" style="color:#4fc3f7;font-size:16px">${forecastData.rain_chance}</div>
                    </div>
                    ` : ''}
                `;
            } else if (marketDate) {
                summaryHtml += `
                    <div class="pm-stat">
                        <div class="pm-stat-label">HKO Forecast (${displayDate})</div>
                        <div class="pm-stat-value forecast-temp" style="color:#666">N/A</div>
                    </div>
                `;
            }
            summaryHtml += `
                <div class="pm-stat">
                    <div class="pm-stat-label">Total Volume</div>
                    <div class="pm-stat-value" style="color:#aaa">$${(event.total_volume || 0) >= 1000 ? ((event.total_volume||0)/1000).toFixed(1)+'K' : (event.total_volume||0).toFixed(0)}</div>
                </div>
            `;

            let barsHtml = outcomes.map(o => renderPolymarketBar(o)).join('');

            return `
                <div class="pm-multi-event">
                    <div class="pm-event-title">${event.title} ${displayDate ? '('+displayDate+')' : ''}</div>
                    <div class="pm-summary">${summaryHtml}</div>
                    ${barsHtml}
                    <a class="pm-link" href="${event.url}" target="_blank">Trade this market ↗</a>
                </div>
            `;
        }

        let polymarketCache = null;

        async function loadPolymarket() {
            const loading = document.getElementById('pm-loading');
            const content = document.getElementById('pm-content');

            // Don't cache - always fetch fresh data (live orderbook updates)
            // polymarketCache = null;

            try {
                loading.style.display = '';
                content.style.display = 'none';

                // Fetch polymarket data + enriched per-day HKO reference + LIVE orderbook in parallel
                const [pmData, pmHko, liveBook] = await Promise.all([
                    fetch('/api/polymarket').then(r => r.json()),
                    fetch('/api/polymarket_hko_daily').then(r => r.json()),
                    fetch('/api/live_orderbook').then(r => r.json()),
                ]);

                // Merge live orderbook data into polymarket data
                const liveBookMap = {};
                liveBook.forEach(entry => {
                    liveBookMap[entry.token_id] = {
                        bid: entry.best_bid,
                        ask: entry.best_ask,
                        updated: entry.updated_at,
                    };
                });

                polymarketCache = { 
                    pmData: mergeOrderbook(pmData, liveBookMap), 
                    hkoTemp: pmHko.hkoTemp, 
                    daily: pmHko.daily, 
                    nine_day: pmHko.nine_day || [] 
                };
                renderPolymarket(polymarketCache);
            } catch (e) {
                loading.textContent = 'Error loading Polymarket data: ' + e.message;
            }
        }

        function mergeOrderbook(pmData, liveBookMap) {
            return pmData.map(event => {
                return {
                    ...event,
                    outcomes: event.outcomes.map((outcome, idx) => {
                        // Try to find matching token_id from condition
                        const tokenId = String(outcome.token_id || '');
                        const live = liveBookMap[tokenId];
                        return live ? {
                            ...outcome,
                            live_bid: live.bid,
                            live_ask: live.ask,
                            live_spread: live.ask && live.bid ? (live.ask - live.bid) : null,
                            live_mid: live.ask && live.bid ? (live.ask + live.bid) / 2 : null,
                            live_updated: live.updated,
                        } : outcome;
                    }),
                };
            });
        }

        function renderPolymarket(data) {
            const loading = document.getElementById('pm-loading');
            const content = document.getElementById('pm-content');
            const summary = document.getElementById('pm-summary');
            const events = document.getElementById('pm-events');
            const link = document.getElementById('pm-link');

            loading.style.display = 'none';
            content.style.display = '';

            const { pmData, hkoTemp, daily } = data;

            if (!pmData || !pmData.length) {
                events.innerHTML = '<div style="color:#666;padding:20px;">No active HK temperature markets found</div>';
                summary.innerHTML = '';
                return;
            }

            // Set main link to first event
            link.href = pmData[0].url;

            // Helper: convert UTC date string to HKT (UTC+8) YYYYMMDD
            function utcToHktDate(d) {
                if (!d) return '';
                const utc = new Date(d + 'Z');
                const hkt = new Date(utc.getTime() + 8 * 3600 * 1000);
                return hkt.getFullYear()
                    + String(hkt.getMonth()+1).padStart(2,'0')
                    + String(hkt.getDate()).padStart(2,'0');
            }

            let eventsHtml = pmData.map(event => {
                const mDate = utcToHktDate(event.date);
                const fcast = (daily && daily[mDate]) || null;
                return renderPolymarketEvent(event, hkoTemp, fcast, mDate);
            }).join('');

            events.innerHTML = eventsHtml;

            // Render 9-day forecast
            const nineDayGrid = document.getElementById('pm-nine-day-grid');
            const { nine_day } = data;
            if (nine_day && nine_day.length) {
                nineDayGrid.innerHTML = nine_day.map(d => {
                    const maxT = d.max_temp != null ? d.max_temp.toFixed(1) : '?';
                    const minT = d.min_temp != null ? d.min_temp.toFixed(1) : '?';
                    const rain = d.rain_prob || '';
                    const wind = d.wind_info || '';
                    return `\
                    <div style="background:#1a1a2e;border:1px solid #333;border-radius:8px;padding:12px;text-align:center">
                        <div style="color:#aaa;font-size:12px;margin-bottom:6px">${d.date_str || d.forecast_date || ''}</div>
                        <div style="font-size:20px;margin-bottom:4px">
                            <span style="color:#ff7043">${maxT}°</span>
                            <span style="color:#666">/</span>
                            <span style="color:#4fc3f7">${minT}°</span>
                        </div>
                        <div style="font-size:11px;color:#888">${d.weather_desc || ''}</div>
                        ${rain ? `<div style="font-size:11px;color:#4fc3f7;margin-top:4px">Rain ${rain}</div>` : ''}
                        ${wind ? `<div style="font-size:11px;color:#a5d6a7;margin-top:2px">${wind}</div>` : ''}
                    </div>`;
                }).join('');
                document.getElementById('pm-nine-day').style.display = '';
            } else {
                nineDayGrid.innerHTML = '<div style="color:#666;padding:10px;font-size:12px">9-day forecast unavailable</div>';
            }
        }

        async function refresh() {
            try {
                const [readings, history, codes] = await Promise.all([
                    fetch('/api/latest').then(r => r.json()),
                    fetch('/api/history?hours=24').then(r => r.json()),
                    fetch('/api/forecast_codes').then(r => r.json()),
                ]);
                document.getElementById('status').textContent =
                    `Observations: ${readings[0]?.recorded_at} HKT | ${readings.length} stations | ${history.length} records`;
                renderCards(readings);
                if (currentSubTab === 'chart') renderChart(history, true);
                if (currentSubTab === 'table' && tableOffset === 0) loadTable(true);
                forecastStationCodes = codes;

                // Update forecast station selector — use codes as values, names as labels
                const fsel = document.getElementById('forecast-station-select');
                const currentVal = fsel.value;
                const nameToCode = {};
                const nameToCodeFallback = {"HK Observatory": "HKO", "Chek Lap Kok": "HKA", "Sha Tin": "SHA", "Shek Kong": "SKG", "Lau Fau Shan": "LFS", "Ta Kwu Ling": "TKL", "Cheung Chau": "CCH", "Peng Chau": "PEN", "Waglan Island": "WGL", "HK Park": "HKS", "Kai Tak": "JKB", "Sheung Shui": "SEK", "Tseung Kwan O": "TPO", "Tuen Mun": "TUN", "Tai Mei Tuk": "TY1", "Sha Tau Kok": "SSH"};
                // Reverse the mapping: name -> code
                for (const [name, code] of Object.entries(nameToCodeFallback)) {
                    nameToCode[name] = code;
                }
                // Build options from forecast station codes, matched to observation names
                fsel.innerHTML = codes.map(code => {
                    const displayName = nameToCodeFallback[code] || code;
                    return `<option value="${code}">${displayName}</option>`;
                }).join('');
                fsel.value = currentVal || 'HKO';
            } catch (e) {
                document.getElementById('status').textContent = 'Error: ' + e.message;
            }
        }

        async function poll() {
            try {
                const r = await fetch('/api/poll', { method: 'POST' });
                const data = await r.json();
                document.getElementById('status').textContent = `Fetched: ${data.count} readings`;
                setTimeout(refresh, 500);
            } catch (e) {
                alert('Poll failed: ' + e.message);
            }
        }

        async function pollForecasts() {
            try {
                const r = await fetch('/api/poll_forecast', { method: 'POST' });
                const data = await r.json();
                document.getElementById('status').textContent = `Forecasts: ${data.count} records fetched`;
                setTimeout(() => {
                    if (currentMainTab === 'forecast') loadForecasts();
                }, 500);
            } catch (e) {
                alert('Forecast poll failed: ' + e.message);
            }
        }

        document.getElementById('station-select').addEventListener('change', () => {
            const station = document.getElementById('station-select').value;
            if (currentSubTab === 'chart') {
                fetch('/api/history?hours=24&station=' + encodeURIComponent(station))
                    .then(r => r.json())
                    .then(renderChart);
            } else {
                loadTable(true);
            }
        });

        document.getElementById('forecast-station-select').addEventListener('change', () => {
            loadForecasts();
        });

        async function loadPaperTrading() {
            const container = document.getElementById('trading-content');
            try {
                const data = await fetch('/api/paper_trading').then(r => r.json());
                container.innerHTML = data.html;
            } catch (e) {
                container.innerHTML = '<div style="color:#e57373;padding:20px;">Error: ' + e.message + '</div>';
            }
        }

        async function loadNoTrading() {
            try {
                const data = await fetch('/api/no_trading').then(r => r.json());
                renderNoTrading(data);
            } catch (e) {
                console.error('Failed to load NO trading data:', e);
            }
        }

        function renderNoTrading(data) {
            // Engine status badge
            const badge = document.getElementById('engine-status-badge');
            if (badge) {
                if (data.engine_running) {
                    badge.textContent = '● Running';
                    badge.style.background = '#1b5e20';
                    badge.style.color = '#4caf50';
                } else {
                    badge.textContent = '○ Stopped';
                    badge.style.background = '#333';
                    badge.style.color = '#888';
                }
            }

            // Status cards
            const cardsEl = document.getElementById('engine-cards');
            if (cardsEl) {
                const cards = [
                    { label: 'Active Conditions', value: data.active_conditions || 0, icon: '📊' },
                    { label: 'Tracked Tokens', value: data.orderbook_tokens || 0, icon: '🔗' },
                    { label: 'Forecast Dates', value: data.tracked_dates || 0, icon: '📅' },
                    { label: 'Last HKO Sync', value: data.last_hko_sync ? timeSince(data.last_hko_sync) : 'Never', icon: '🌡️' },
                    { label: 'Last Heartbeat', value: data.last_heartbeat ? timeSince(data.last_heartbeat) : 'Never', icon: '💓' },
                    { label: 'Last Re-score', value: data.last_rescore ? timeSince(data.last_rescore) : 'Never', icon: '🔄' },
                ];
                cardsEl.innerHTML = cards.map(c => `
                    <div class="card">
                        <div style="font-size:20px;margin-bottom:4px">${c.icon}</div>
                        <div style="font-size:14px;color:#aaa">${c.label}</div>
                        <div style="font-size:20px;font-weight:bold;margin-top:4px">${c.value}</div>
                    </div>
                `).join('');
            }

            // Trigger log
            const logEl = document.getElementById('trigger-log');
            if (logEl && data.triggers && data.triggers.length > 0) {
                logEl.innerHTML = data.triggers.map(t => {
                    let color = '#888';
                    let icon = '•';
                    if (t.type === 'heartbeat') { color = '#ff9800'; icon = '💓'; }
                    else if (t.type === 'momentum') { color = '#f44336'; icon = '📈'; }
                    else if (t.type === 'hko_changed') { color = '#4caf50'; icon = '🌡️'; }
                    else if (t.type === 'hko_sync') { color = '#2196f3'; icon = '☁️'; }
                    else if (t.type === 'trade_executed') { color = '#e040fb'; icon = '💰'; }
                    else if (t.type === 'trade_rejected') { color = '#666'; icon = '❌'; }
                    return `<div style="padding:6px 0;border-bottom:1px solid #1a1a2e;font-size:12px">
                        <span style="color:${color}">${icon} ${t.type}</span>
                        <span style="color:#666;margin-left:8px">${t.time}</span>
                        <span style="color:#ccc;margin-left:8px">${t.message}</span>
                    </div>`;
                }).join('');
            }

            // Multi-factor adjustments
            const factorBody = document.getElementById('no-factor-body');
            if (factorBody && data.factors && data.factors.length > 0) {
                factorBody.innerHTML = data.factors.map(f => `
                    <tr style="border-bottom:1px solid #1a1a2e">
                        <td style="padding:8px;color:#ccc">${f.date}</td>
                        <td style="padding:8px;text-align:right;color:#fff">${f.raw_temp.toFixed(1)}°</td>
                        <td style="padding:8px;text-align:right;color:${f.cloud_adj > 0 ? '#4caf50' : '#f44336'}">${f.cloud_adj > 0 ? '+' : ''}${f.cloud_adj.toFixed(1)}</td>
                        <td style="padding:8px;text-align:right;color:${f.wind_adj > 0 ? '#4caf50' : '#f44336'}">${f.wind_adj > 0 ? '+' : ''}${f.wind_adj.toFixed(1)}</td>
                        <td style="padding:8px;text-align:right;color:${f.humidity_adj > 0 ? '#4caf50' : '#f44336'}">${f.humidity_adj > 0 ? '+' : ''}${f.humidity_adj.toFixed(1)}</td>
                        <td style="padding:8px;text-align:right;color:#00d4ff;font-weight:bold">${f.adjusted_temp.toFixed(1)}°</td>
                        <td style="padding:8px;color:#888;font-size:11px">${f.details}</td>
                    </tr>
                `).join('');
            }

            // NO positions
            const posBody = document.getElementById('no-positions-body');
            if (posBody && data.no_positions && data.no_positions.length > 0) {
                posBody.innerHTML = data.no_positions.map(p => {
                    const pnl = p.pnl;
                    const pnlColor = pnl > 0 ? '#4caf50' : pnl < 0 ? '#f44336' : '#888';
                    const statusColor = p.status === 'OPEN' ? '#4caf50' : '#666';
                    return `<tr style="border-bottom:1px solid #1a1a2e; ${p.status !== 'OPEN' ? 'opacity:0.6' : ''}">
                        <td style="padding:8px;color:#ccc">${p.market}</td>
                        <td style="padding:8px;color:#fff;font-weight:bold">${p.bucket}</td>
                        <td style="padding:8px;text-align:right;color:#e040fb">${p.qty.toFixed(2)}</td>
                        <td style="padding:8px;text-align:right;color:#aaa">$${p.entry_price.toFixed(4)}</td>
                        <td style="padding:8px;text-align:right;color:#ccc">$${p.current_price ? p.current_price.toFixed(4) : 'N/A'}</td>
                        <td style="padding:8px;text-align:right;color:${pnlColor};font-weight:bold">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</td>
                        <td style="padding:8px;color:${statusColor};font-weight:bold;font-size:11px">${p.status}</td>
                        <td style="padding:8px;color:#666;font-size:11px">${p.trigger || 'N/A'}</td>
                    </tr>`;
                }).join('');
                    } else if (posBody) {
                posBody.innerHTML = '<tr><td colspan="8" style="padding:20px;text-align:center;color:#666">No NO positions yet</td></tr>';
            }

            // Scoring decisions log
            const scoringBody = document.getElementById('scoring-log-body');
            if (scoringBody && data.scoring && data.scoring.length > 0) {
                scoringBody.innerHTML = data.scoring.map(s => {
                    const decColor = s.decision === 'TRADE_CANDIDATE' ? '#4caf50' :
                                     s.decision === 'SKIP' ? '#888' : '#ff9800';
                    return `<tr style="border-bottom:1px solid #1a1a2e">
                        <td style="padding:8px;color:#888;font-size:11px">${s.condition_id || ''}</td>
                        <td style="padding:8px;color:#ccc">${s.bucket || ''}</td>
                        <td style="padding:8px;text-align:right;color:#00d4ff">${s.model_prob != null ? s.model_prob.toFixed(3) : '-'}</td>
                        <td style="padding:8px;text-align:right;color:#ccc">${s.market_yes != null ? s.market_yes.toFixed(3) : '-'}</td>
                        <td style="padding:8px;text-align:right;color:${(s.edge||0) > 0 ? '#4caf50' : '#f44336'}">${s.edge != null ? s.edge.toFixed(3) : '-'}</td>
                        <td style="padding:8px;text-align:left;color:${decColor};font-weight:bold;font-size:11px">${s.decision || '-'}</td>
                        <td style="padding:8px;color:#888;font-size:11px">${s.rationale || ''}</td>
                    </tr>`;
                }).join('');
            } else if (scoringBody) {
                scoringBody.innerHTML = '<tr><td colspan="7" style="padding:20px;text-align:center;color:#666">No scoring decisions yet</td></tr>';
            }

            // Maker orders
            const makerBody = document.getElementById('maker-orders-body');
            if (makerBody && data.maker_orders && data.maker_orders.length > 0) {
                makerBody.innerHTML = data.maker_orders.map(m => {
                    const sideColor = m.side === 'BUY_YES' ? '#4caf50' : '#f44336';
                    const statusColor = m.status === 'OPEN' ? '#4caf50' : '#888';
                    return `<tr style="border-bottom:1px solid #1a1a2e">
                        <td style="padding:8px;color:#888;font-size:11px">${m.condition_id || ''}</td>
                        <td style="padding:8px;color:#ccc">${m.bucket || ''}</td>
                        <td style="padding:8px;text-align:left;color:${sideColor};font-weight:bold;font-size:11px">${m.side || '-'}</td>
                        <td style="padding:8px;text-align:right;color:#fff">${m.price != null ? m.price.toFixed(3) : '-'}</td>
                        <td style="padding:8px;text-align:right;color:#ccc">${m.size != null ? m.size.toFixed(0) : '-'}</td>
                        <td style="padding:8px;text-align:right;color:#00d4ff">${m.fair_value != null ? m.fair_value.toFixed(3) : '-'}</td>
                        <td style="padding:8px;text-align:left;color:${statusColor};font-size:11px">${m.status || '-'}</td>
                    </tr>`;
                }).join('');
            } else if (makerBody) {
                makerBody.innerHTML = '<tr><td colspan="7" style="padding:20px;text-align:center;color:#666">No maker orders yet</td></tr>';
            }
        }

        function timeSince(timestamp) {
            if (!timestamp || timestamp === 0) return 'Never';
            const seconds = Math.floor((Date.now() / 1000) - timestamp);
            if (seconds < 0) return 'Never';
            if (seconds < 60) return seconds + 's ago';
            if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
            return Math.floor(seconds / 3600) + 'h ago';
        }

        // Auto-refresh: update all active tabs in real-time
        let refreshInterval = null;
        
        function startAutoRefresh() {
            if (refreshInterval) clearInterval(refreshInterval);
            refreshInterval = setInterval(() => {
                // Update observations silently
                refresh();
                
                // Update Polymarket if active
                if (currentMainTab === 'polymarket') {
                    loadPolymarket();
                }
                
                // Update Paper Trading if active
                if (currentMainTab === 'trading') {
                    loadPaperTrading();
                }
                
                // Update NO Trading if active
                if (currentMainTab === 'no-trading') {
                    loadNoTrading();
                }
                
                // Update Forecasts if active
                if (currentMainTab === 'forecast') {
                    loadForecasts();
                }
                
                // Update last-refresh timestamp
                const statusEl = document.getElementById('status');
                if (statusEl) {
                    const now = new Date();
                    const hktTime = new Date(now.getTime() + 8 * 3600 * 1000);
                    const timeStr = hktTime.getFullYear() + '/' +
                        String(hktTime.getMonth()+1).padStart(2,'0') + '/' +
                        String(hktTime.getDate()).padStart(2,'0') + ' ' +
                        String(hktTime.getHours()).padStart(2,'0') + ':' +
                        String(hktTime.getMinutes()).padStart(2,'0') + ':' +
                        String(hktTime.getSeconds()).padStart(2,'0') + ' HKT';
                    
                    // Update the last refresh time display
                    const refreshIndicator = document.getElementById('last-refresh');
                    if (!refreshIndicator) {
                        // Add a refresh indicator if it doesn't exist
                        const indicator = document.createElement('div');
                        indicator.id = 'last-refresh';
                        indicator.style.cssText = 'color:#555;font-size:11px;margin-left:10px;';
                        statusEl.parentNode.appendChild(indicator);
                    }
                    document.getElementById('last-refresh').textContent = '↻ ' + timeStr;
                }
            }, 15000); // 15 seconds for near real-time
        }
        
        startAutoRefresh();
    </script>
</body>
"""


def _mean_direction(degrees):
    """Circular mean of wind direction in degrees."""
    import math
    s = sum(math.sin(math.radians(d)) for d in degrees)
    c = sum(math.cos(math.radians(d)) for d in degrees)
    return math.degrees(math.atan2(s, c)) % 360


def _cardinal(deg):
    """Convert degrees to cardinal direction."""
    dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    idx = round(deg / 22.5) % 16
    return dirs[idx]


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif parsed.path == '/api/latest':
            data = get_latest_readings()
            self._json_response(data)
        elif parsed.path == '/api/history':
            params = parse_qs(parsed.query)
            hours = int(params.get('hours', [24])[0])
            station = params.get('station', [None])[0]
            if station:
                from hko_weather_monitor.db import get_temperature_history
                data = get_temperature_history(station, hours)
            else:
                data = get_all_history(hours)
            self._json_response(data)
        elif parsed.path == '/api/stations':
            data = get_all_stations()
            self._json_response(data)
        elif parsed.path == '/api/table':
            params = parse_qs(parsed.query)
            station = params.get('station', ['HK Observatory'])[0]
            offset = int(params.get('offset', [0])[0])
            limit = int(params.get('limit', [50])[0])
            data = get_history_table(station, offset, limit)
            self._json_response(data)
        elif parsed.path == '/api/forecast_hourly':
            params = parse_qs(parsed.query)
            station = params.get('station', ['HKO'])[0]
            data = get_latest_forecasts_hourly(station)
            self._json_response(data)
        elif parsed.path == '/api/forecast_daily':
            params = parse_qs(parsed.query)
            station = params.get('station', ['HKO'])[0]
            data = get_latest_forecasts_daily(station)
            self._json_response(data)
        elif parsed.path == '/api/forecast_codes':
            data = get_forecast_station_codes()
            self._json_response(data)
        elif parsed.path == '/api/polymarket':
            data = fetch_active_hk_polymarket()
            if data:
                for event in data:
                    event['expected_temp'] = compute_expected_temp(event.get('outcomes', []))
            self._json_response(data or [])
        elif parsed.path == '/api/paper_trading':
            # Paper trading summary
            from hko_weather_monitor.execution_engine import PaperExecutionEngine
            engine = PaperExecutionEngine(DB_PATH)
            data = engine.get_trading_summary_html()
            self._json_response({"html": data})
        elif parsed.path == '/api/live_orderbook':
            # Live orderbook data for all active market outcomes
            conn = sqlite3.connect(DB_PATH)
            try:
                cursor = conn.cursor()
                
                # Fetch all outcomes in one query
                cursor.execute("""
                    SELECT mo.condition_id, mo.outcome_name, mo.yes_token_id,
                           m.target_date, m.title
                    FROM market_outcomes mo
                    JOIN markets m ON mo.condition_id = m.condition_id
                    WHERE m.status = 'ACTIVE'
                    ORDER BY m.target_date, mo.id
                """)
                outcomes = cursor.fetchall()
                
                # Batch-fetch latest orderbook for all tokens at once
                token_ids = [row[2] for row in outcomes if row[2]]
                latest_book = {}
                if token_ids:
                    placeholders = ','.join(['?' for _ in token_ids])
                    cursor.execute(f"""
                        SELECT token_id, best_bid, best_ask, updated_at
                        FROM orderbook_state
                        WHERE token_id IN ({placeholders})
                    """, token_ids)
                    # Keep only the latest entry per token
                    for row in cursor.fetchall():
                        tid = row[0]
                        if tid not in latest_book or (row[3] or '') > (latest_book[tid][3] or ''):
                            latest_book[tid] = row
                
                live_book = []
                for row in outcomes:
                    token_id = row[2]
                    book = latest_book.get(token_id)
                    live_book.append({
                        'condition_id': row[0],
                        'outcome_name': row[1],
                        'token_id': token_id,
                        'best_bid': book[1] if book else None,
                        'best_ask': book[2] if book else None,
                        'updated_at': book[3] if book else None,
                    })
                
                self._json_response(live_book)
            finally:
                conn.close()
        elif parsed.path == '/api/polymarket_hko_daily':
            # Per-day aggregates: daily forecast + hourly wind stats + 9-day forecast
            from datetime import date as _date
            daily = get_latest_forecasts_daily('HKO')
            hourly = get_latest_forecasts_hourly('HKO')
            readings = get_latest_readings()
            hko = next((r for r in readings if r['name'] == 'HK Observatory'), None)

            # Aggregate hourly wind by day (YYYYMMDD)
            hourly_by_day = {}
            for h in hourly:
                fh = h.get('forecast_hour', '')
                day = fh[:8] if len(fh) >= 8 else ''
                if not day:
                    continue
                if day not in hourly_by_day:
                    hourly_by_day[day] = {'temps': [], 'ws': [], 'wd': [], 'rh': []}
                bucket = hourly_by_day[day]
                if h.get('temperature') is not None:
                    bucket['temps'].append(h['temperature'])
                if h.get('wind_speed') is not None:
                    bucket['ws'].append(h['wind_speed'])
                if h.get('wind_direction') is not None:
                    bucket['wd'].append(h['wind_direction'])
                if h.get('humidity') is not None:
                    bucket['rh'].append(h['humidity'])

            # Merge with daily forecasts
            result = {}
            for d in daily:
                day = d['forecast_date']
                agg = hourly_by_day.get(day, {})
                ws_list = agg.get('ws', [])
                wd_list = agg.get('wd', [])
                result[day] = {
                    'max_temp': d.get('max_temperature'),
                    'min_temp': d.get('min_temperature'),
                    'weather_code': d.get('weather_code'),
                    'rain_chance': d.get('chance_of_rain'),
                    'wind_avg': round(sum(ws_list) / len(ws_list), 1) if ws_list else None,
                    'wind_max': round(max(ws_list), 1) if ws_list else None,
                    'wind_dir': _cardinal(_mean_direction(wd_list)) if wd_list else None,
                }

            # 9-day forecast from HKO (scraped from Highcharts data)
            # 9-day forecast from HKO JSON API
            from hko_weather_monitor.fetcher import fetch_nine_day_forecast
            from hko_weather_monitor.db import bulk_insert_nine_day_forecast, get_nine_day_forecast
            nine_day = fetch_nine_day_forecast()
            if nine_day:
                bulk_insert_nine_day_forecast(nine_day)
                nine_day = get_nine_day_forecast()

            self._json_response({
                'hkoTemp': hko['temperature'] if hko else None,
                'daily': result,
                'nine_day': nine_day or [],
            })
        elif parsed.path == '/api/no_trading':
            # NO Trading Engine dashboard data
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.row_factory = sqlite3.Row

                # 1. Trigger log (last 50)
                triggers_out = []
                try:
                    triggers = conn.execute(
                        "SELECT timestamp, type, message FROM trigger_log ORDER BY id DESC LIMIT 50"
                    ).fetchall()
                    for t in triggers:
                        ts = t['timestamp']
                        from datetime import datetime as _dt
                        dt = _dt.fromtimestamp(ts).strftime('%H:%M:%S')
                        triggers_out.append({
                            'time': dt,
                            'type': t['type'],
                            'message': t['message'],
                        })
                except Exception:
                    # If no trigger_log table yet, seed from fills
                    pass

                if not triggers_out:
                    # Seed from recent SELL fills
                    try:
                        sells = conn.execute("""
                            SELECT pf.timestamp, mo.outcome_name, pf.avg_fill_price,
                                   mo.condition_id
                            FROM paper_fills pf
                            JOIN market_outcomes mo ON pf.condition_id = mo.condition_id
                            WHERE pf.order_side = 'SELL'
                            ORDER BY pf.id DESC LIMIT 20
                        """).fetchall()
                        import time as _time
                        for s in sells:
                            triggers_out.append({
                                'time': s['timestamp'][-8:-3] if s['timestamp'] else '00:00:00',
                                'type': 'trade_executed',
                                'message': f"SELL YES (BUY NO): {s['outcome_name']} @ {s['avg_fill_price']:.4f}",
                            })
                            # Insert into trigger_log for future
                            try:
                                conn.execute(
                                    "INSERT INTO trigger_log (timestamp, type, message) VALUES (?, ?, ?)",
                                    (_time.time(), 'trade_executed',
                                     f"SELL YES (BUY NO): {s['outcome_name']} @ {s['avg_fill_price']:.4f}")
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass

                # 2. NO positions (side='NO')
                no_positions = []
                try:
                    rows = conn.execute("""
                        SELECT pp.condition_id, pp.token_id, pp.qty,
                               pp.avg_entry_price, pp.opened_at, pp.status,
                               mo.outcome_name, m.target_date, m.title
                        FROM paper_positions pp
                        JOIN market_outcomes mo ON pp.token_id = mo.yes_token_id
                        LEFT JOIN markets m ON pp.condition_id = m.condition_id
                        WHERE pp.side = 'NO'
                        ORDER BY pp.id DESC LIMIT 50
                    """).fetchall()

                    # Get current prices from orderbook_state
                    token_ids = [r['token_id'] for r in rows if r['token_id']]
                    current_prices = {}
                    if token_ids:
                        placeholders = ','.join(['?' for _ in token_ids])
                        book_rows = conn.execute(f"""
                            SELECT token_id, best_bid, best_ask
                            FROM orderbook_state
                            WHERE token_id IN ({placeholders})
                              AND (best_bid IS NOT NULL OR best_ask IS NOT NULL)
                        """, token_ids).fetchall()
                        for br in book_rows:
                            bid = br[1]
                            ask = br[2]
                            if bid is not None or ask is not None:
                                mid = ((bid or 0) + (ask or 0)) / 2.0
                                current_prices[br[0]] = mid

                    for row in rows:
                        cp = current_prices.get(row['token_id'])
                        entry = row['avg_entry_price'] or 0
                        # For SHORT: profit if price goes DOWN
                        qty = abs(row['qty'] or 0)
                        pnl = (entry - (cp if cp else entry)) * qty
                        no_positions.append({
                            'market': (row['target_date'] or row['condition_id']).split('T')[0],
                            'bucket': row['outcome_name'] or 'N/A',
                            'qty': qty,
                            'entry_price': entry,
                            'current_price': cp,
                            'pnl': pnl,
                            'status': row['status'] if row['status'] else 'OPEN',
                            'trigger': row['opened_at'][-8:-3] if row['opened_at'] else '',
                        })
                except Exception as e:
                    pass

                # 3. Multi-factor adjustments per date
                factors_out = []
                try:
                    # Get active condition IDs
                    conditions = conn.execute(
                        "SELECT DISTINCT condition_id FROM market_outcomes"
                    ).fetchall()
                    for (cond,) in conditions:
                        import re as _re
                        m = _re.search(r'(\d{4}-\d{2}-\d{2})', cond)
                        if not m:
                            continue
                        date_iso = m.group(1)
                        date_hko = date_iso.replace('-', '')

                        # Get daily forecast from forecast_daily table
                        daily = conn.execute("""
                            SELECT max_temperature, min_temperature, weather_code,
                                   chance_of_rain
                            FROM forecast_daily
                            WHERE station_code = 'HKO'
                              AND forecast_date = ?
                            ORDER BY fetched_at DESC LIMIT 1
                        """, (date_hko,)).fetchone()

                        if not daily or daily['max_temperature'] is None:
                            continue

                        raw_temp = float(daily['max_temperature'])

                        # Weather code → cloud coverage
                        wc = daily['weather_code'] or 0
                        cloud_map = {
                            0: 0.0, 1: 15.0, 2: 65.0, 3: 85.0,
                            50: 30.0, 51: 10.0, 52: 30.0, 53: 10.0, 54: 25.0,
                            60: 75.0, 61: 65.0, 62: 90.0, 63: 95.0, 64: 90.0,
                            71: 95.0, 72: 100.0, 73: 100.0, 74: 100.0, 76: 100.0,
                        }
                        cloud_cov = cloud_map.get(wc, 50.0)

                        # Cloud adjustment
                        cloud_adj = 0.0
                        if cloud_cov > 75.0:
                            cloud_adj = -0.8
                        elif cloud_cov < 20.0:
                            cloud_adj = 0.4

                        # Get hourly wind/humidity from forecast_hourly
                        hourly = conn.execute("""
                            SELECT humidity, wind_speed, wind_direction
                            FROM forecast_hourly
                            WHERE station_code = 'HKO'
                              AND forecast_hour LIKE ?
                            ORDER BY forecast_hour ASC
                            LIMIT 12
                        """, (date_hko + '%',)).fetchall()

                        humidity_avg = 0.0
                        wind_speed_avg = 0.0
                        wind_dirs = []
                        for h in hourly:
                            if h[0] is not None:
                                humidity_avg += h[0]
                            if h[1] is not None:
                                wind_speed_avg += h[1]
                            if h[2] is not None:
                                # Convert numeric direction to cardinal
                                deg = int(h[2])
                                dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
                                        'S','SSW','SW','WSW','W','WNW','NW','NNW']
                                wind_dirs.append(dirs[round(deg / 22.5) % 16])

                        wind_speed_kmh = 0.0
                        if hourly:
                            humidity_avg /= len(hourly)
                            wind_speed_avg /= len(hourly)
                            # Convert m/s to km/h for threshold comparison
                            wind_speed_kmh = wind_speed_avg * 3.6

                        # Humidity adjustment
                        humidity_adj = 0.0
                        if humidity_avg > 85.0:
                            humidity_adj = -0.3

                        # Wind adjustment
                        wind_adj = 0.0
                        dominant_dir = max(set(wind_dirs), key=wind_dirs.count) if wind_dirs else 'E'
                        if dominant_dir in ['E', 'SE'] and wind_speed_kmh > 15.0:
                            wind_adj = -0.5
                        elif dominant_dir in ['N', 'NW'] and wind_speed_kmh < 10.0:
                            wind_adj = 0.6

                        # UV adjustment
                        import hko_weather_monitor.uv_fetcher as _uv
                        uv_peak = _uv.get_peak_uv_index(date_hko, cloud_cov)
                        uv_adj = _uv.get_uv_forecast_adjustment(uv_peak)
                        # None safety: ensure all adjustments are numeric before math
                        uv_adj = uv_adj if uv_adj is not None else 0.0
                        cloud_adj = cloud_adj if cloud_adj is not None else 0.0
                        wind_adj = wind_adj if wind_adj is not None else 0.0
                        humidity_adj = humidity_adj if humidity_adj is not None else 0.0
                        if uv_peak is not None and uv_peak <= 2:
                            uv_level = 'low'
                        elif uv_peak is not None and uv_peak <= 5:
                            uv_level = 'moderate'
                        elif uv_peak is not None and uv_peak <= 7:
                            uv_level = 'high'
                        elif uv_peak is not None and uv_peak <= 10:
                            uv_level = 'very_high'
                        elif uv_peak is not None:
                            uv_level = 'extreme'
                        else:
                            uv_level = 'N/A'

                        adjusted_temp = raw_temp + cloud_adj + wind_adj + humidity_adj + uv_adj

                        factors_out.append({
                            'date': date_iso,
                            'raw_temp': raw_temp,
                            'cloud_adj': cloud_adj,
                            'wind_adj': wind_adj,
                            'humidity_adj': humidity_adj,
                            'uv_adj': uv_adj,
                            'peak_uv': uv_peak,
                            'uv_level': uv_level,
                            'adjusted_temp': adjusted_temp,
                            'details': f'Cloud:{cloud_cov:.0f}% Wind:{dominant_dir} {wind_speed_avg:.1f}m/s RH:{humidity_avg:.0f}% Rain:{daily["chance_of_rain"] or 0}% UV:{uv_peak} ({uv_level})',
                        })
                except Exception as _e:
                    import logging as _log
                    _log.getLogger('dashboard').error(f'factors loop error for {cond}: {_e}', exc_info=True)

                conn.commit()

            finally:
                conn.close()

            # 4. Engine status (PID file check + timestamps from DB)
            import os as _os
            engine_pid_file = '/tmp/hko_engine.pid'
            engine_running = False
            if _os.path.exists(engine_pid_file):
                try:
                    with open(engine_pid_file) as f:
                        pid = int(f.read().strip())
                    os.kill(pid, 0)
                    engine_running = True
                except (ValueError, FileNotFoundError, ProcessLookupError, PermissionError):
                    engine_running = False

            # Read timestamps from engine_status table
            last_hko_sync = 0.0
            last_heartbeat = 0.0
            last_rescore = 0.0
            try:
                conn2 = sqlite3.connect(DB_PATH)
                try:
                    for row in conn2.execute('SELECT key, value FROM engine_status').fetchall():
                        if row[0] == 'last_hko_sync':
                            last_hko_sync = row[1]
                        elif row[0] == 'last_heartbeat':
                            last_heartbeat = row[1]
                        elif row[0] == 'last_rescore':
                            last_rescore = row[1]
                finally:
                    conn2.close()
            except Exception:
                pass

            # Counts from DB
            try:
                conn3 = sqlite3.connect(DB_PATH)
                try:
                    tracked_tokens = conn3.execute(
                        "SELECT COUNT(DISTINCT token_id) FROM orderbook_state"
                    ).fetchone()[0]
                    tracked_dates = conn3.execute(
                        "SELECT COUNT(DISTINCT condition_id) FROM market_outcomes"
                    ).fetchone()[0]
                finally:
                    conn3.close()
            except Exception:
                tracked_tokens = 0
                tracked_dates = 0

            # Scoring log (latest per bucket) — independent connection (conn was closed earlier)
            scoring_out = []
            try:
                scoring_conn = sqlite3.connect(DB_PATH)
                for row in scoring_conn.execute("""
                    SELECT timestamp, condition_id, bucket, hko_forecast,
                           model_prob, market_yes, edge, no_score, conviction,
                           kelly_frac, position_size, decision, rationale
                    FROM scoring_log
                    WHERE timestamp = (SELECT MAX(timestamp) FROM scoring_log sl2
                                       WHERE sl2.condition_id = scoring_log.condition_id
                                         AND sl2.bucket = scoring_log.bucket)
                    ORDER BY condition_id, bucket
                """).fetchall():
                    scoring_out.append({
                        'ts': row[0], 'condition': row[1][:25] if row[1] else 'N/A', 'bucket': row[2],
                        'hko': row[3], 'model_prob': row[4], 'market_yes': row[5],
                        'edge': row[6], 'no_score': row[7], 'conviction': row[8],
                        'kelly': row[9], 'pos_size': row[10], 'decision': row[11],
                        'rationale': row[12],
                    })
                scoring_conn.close()
            except Exception:
                import traceback
                traceback.print_exc()

            # Maker orders (latest per bucket) — independent connection (conn was closed earlier)
            maker_out = []
            try:
                maker_conn = sqlite3.connect(DB_PATH)
                for row in maker_conn.execute("""
                    SELECT timestamp, condition_id, bucket, side, price, size,
                           fair_value, spread_offset, rationale, status
                    FROM maker_orders
                    WHERE timestamp = (SELECT MAX(timestamp) FROM maker_orders mo2
                                       WHERE mo2.condition_id = maker_orders.condition_id
                                         AND mo2.bucket = maker_orders.bucket)
                    ORDER BY condition_id, bucket, side
                """).fetchall():
                    maker_out.append({
                        'ts': row[0], 'condition': row[1][:25] if row[1] else 'N/A', 'bucket': row[2],
                        'side': row[3], 'price': row[4], 'size': row[5],
                        'fair_value': row[6], 'spread': row[7],
                        'rationale': row[8], 'status': row[9],
                    })
                maker_conn.close()
            except Exception:
                import traceback
                traceback.print_exc()

            self._json_response({
                'engine_running': engine_running,
                'active_conditions': tracked_dates,
                'orderbook_tokens': tracked_tokens,
                'tracked_dates': tracked_dates,
                'last_hko_sync': last_hko_sync,
                'last_heartbeat': last_heartbeat,
                'last_rescore': last_rescore,
                'triggers': triggers_out,
                'factors': factors_out,
                'no_positions': no_positions,
                'scoring': scoring_out,
                'maker_orders': maker_out,
            })

        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/api/poll':
            count = poll_once()
            self._json_response({"count": count, "status": "ok"})
        elif self.path == '/api/poll_forecast':
            count = poll_forecasts()
            self._json_response({"count": count, "status": "ok"})
        else:
            self.send_error(404)

    def _json_response(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass  # Suppress access logs


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True  # Avoid "Address already in use"


class ThreadedHTTPServer(socketserver.ThreadingMixIn, ReusableTCPServer):
    daemon_threads = True  # Kill threads on exit


if __name__ == "__main__":
    init_db()

    print(f"HKO Weather Dashboard started")
    print(f"Dashboard: http://0.0.0.0:{PORT}")
    with ThreadedHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()
