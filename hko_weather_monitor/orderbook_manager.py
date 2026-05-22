"""Polymarket CLOB orderbook manager for real-time book tracking."""
import json
import sqlite3
import asyncio
import websockets
from collections import defaultdict
from datetime import datetime
import logging
import os

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "hko_weather.db")


class PolymarketOrderbookManager:
    """Maintains real-time local copy of Polymarket CLOB."""
    
    def __init__(self):
        # Format: { token_id: { 'bids': {price: size}, 'asks': {price: size} } }
        self._books = defaultdict(lambda: {'bids': {}, 'asks': {}})
        self._outcome_map = {}  # token_id -> outcome_name
        self.lock = asyncio.Lock()
        self.ws = None
        self.running = False
        self.condition_id = None
        self.on_price_update = None  # Optional callback: (token_id, best_bid, best_ask) -> None
        self.on_price_update = None  # Optional callback: (token_id, best_bid, best_ask) -> None
        
    def _get_tokens_for_condition(self, condition_id):
        """Query market_outcomes table for all token IDs for a condition."""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT yes_token_id, outcome_name FROM market_outcomes WHERE condition_id = ?",
                (condition_id,)
            )
            return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"DB query error: {e}")
            return []
        finally:
            conn.close()
    
    async def connect(self, condition_id):
        """Connect to Polymarket CLOB WebSocket, subscribing to ALL outcomes for a condition."""
        outcomes = self._get_tokens_for_condition(condition_id)
        if not outcomes:
            logger.warning(f"No tokens found for condition_id: {condition_id}")
            return
        
        self.condition_id = condition_id
        token_ids = [row[0] for row in outcomes]
        
        # Map token IDs to outcome names
        for token_id, outcome_name in outcomes:
            self._outcome_map[token_id] = outcome_name
        
        logger.info(f"Subscribing to {len(token_ids)} tokens for {condition_id}")
        
        self.ws = await websockets.connect(
            "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        )
        
        # Subscribe to ALL tokens - OFFICIAL Polymarket format
        await self.ws.send(json.dumps({
            "type": "market",
            "assets_ids": [str(t) for t in token_ids],
            "initial_dump": True
        }))
        
        self.running = True
        asyncio.create_task(self._listen())
        asyncio.create_task(self._ping_loop())

    async def _listen(self):
        """Listen for WebSocket messages, process them, and notify engine callback."""
        try:
            async for message in self.ws:
                if not self.running:
                    break
                await self.handle_websocket_message(message)
        except websockets.exceptions.ConnectionClosed:
            self.running = False
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            self.running = False
            
    async def handle_websocket_message(self, message_text):
        """Processes real-time delta frames from Polymarket CLOB WS."""
        data = json.loads(message_text)
        
        # Handle ping/pong
        if isinstance(data, str) and data == "PONG":
            return
        
        # Initial snapshot is a LIST of book objects
        if isinstance(data, list):
            async with self.lock:
                for book in data:
                    token_id = str(book.get('asset_id', ''))
                    if not token_id:
                        continue
                    
                    for level in book.get('bids', []):
                        price = float(level['price'])
                        size = float(level['size'])
                        if size == 0.0:
                            self._books[token_id]['bids'].pop(price, None)
                        else:
                            self._books[token_id]['bids'][price] = size
                    
                    for level in book.get('asks', []):
                        price = float(level['price'])
                        size = float(level['size'])
                        if size == 0.0:
                            self._books[token_id]['asks'].pop(price, None)
                        else:
                            self._books[token_id]['asks'][price] = size
                    
                    # Persist to DB
                    self._persist_to_db(token_id)
                    
                    # Notify engine of price update
                    if self.on_price_update:
                        best_bid = max(self._books[token_id]['bids'].keys()) if self._books[token_id]['bids'] else None
                        best_ask = min(self._books[token_id]['asks'].keys()) if self._books[token_id]['asks'] else None
                        self.on_price_update(token_id, best_bid, best_ask)
            logger.info(f"Received initial snapshot for {len(data)} assets")
            return
        
        # Price change updates
        if isinstance(data, dict):
            if 'bids' in data or 'asks' in data:
                async with self.lock:
                    asset_id = data.get('asset_id', '')
                    updated_tokens = set()
                    for side in ['bids', 'asks']:
                        if side in data:
                            for level in data[side]:
                                token_id = str(level.get('asset_id', asset_id))
                                if not token_id:
                                    continue
                                price = float(level['price'])
                                size = float(level['size'])
                                if size == 0.0:
                                    self._books[token_id][side].pop(price, None)
                                else:
                                    self._books[token_id][side][price] = size
                                updated_tokens.add(token_id)
                    
                    for tid in updated_tokens:
                        self._persist_to_db(tid)
                        if self.on_price_update:
                            best_bid = max(self._books[tid]['bids'].keys()) if self._books[tid]['bids'] else None
                            best_ask = min(self._books[tid]['asks'].keys()) if self._books[tid]['asks'] else None
                            self.on_price_update(tid, best_bid, best_ask)
            elif data.get('event_type') == 'TRADE' or 'price_changes' in data:
                pass

    def get_snapshot(self, token_id):
        """Returns sorted representations of bids (desc) and asks (asc)."""
        book = self._books[token_id]
        sorted_bids = sorted([(p, s) for p, s in book['bids'].items()], key=lambda x: -x[0])
        sorted_asks = sorted([(p, s) for p, s in book['asks'].items()], key=lambda x: x[0])
        return sorted_bids, sorted_asks
    
    def _persist_to_db(self, token_id):
        """Persist orderbook snapshot to orderbook_state table for dashboard."""
        book = self._books.get(token_id, {})
        bids = book.get('bids', {})
        asks = book.get('asks', {})
        
        if not bids and not asks:
            return
        
        best_bid = max(bids.keys()) if bids else None
        best_ask = min(asks.keys()) if asks else None
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO orderbook_state 
                (token_id, best_bid, best_ask, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """, (token_id, best_bid, best_ask))
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Failed to persist orderbook: {e}")

    def inject_consumed_liquidity(self, token_id, side, price, size_consumed):
        """Deducts liquidity from the orderbook state immediately upon fill simulation."""
        target_side = 'bids' if side.lower() == 'bid' else 'asks'
        if price in self._books[token_id][target_side]:
            self._books[token_id][target_side][price] = max(0.0, self._books[token_id][target_side][price] - size_consumed)
            if self._books[token_id][target_side][price] == 0.0:
                self._books[token_id][target_side].pop(price, None)

    def disconnect(self):
        """Disconnect WebSocket."""
        self.running = False
        if self.ws:
            asyncio.create_task(self.ws.close())

    async def _ping_loop(self):
        """Send periodic pings to keep the WebSocket alive."""
        while self.running and self.ws:
            try:
                await asyncio.sleep(30)
                await self.ws.ping()
            except Exception:
                break
