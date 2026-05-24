"""Paper trade execution engine with slippage simulation."""
import sqlite3
from datetime import datetime
from hko_weather_monitor.orderbook_manager import PolymarketOrderbookManager
from hko_weather_monitor.slippage import calculate_buy_slippage, calculate_sell_slippage


class PaperExecutionEngine:
    """Executes paper trades with realistic slippage and liquidity constraints."""
    
    def __init__(self, db_path, book_manager=None):
        self.db_path = db_path
        self.book_manager = book_manager

    def execute_paper_buy(self, account_id, condition_id, token_id, cash_amount):
        """Executes a signal by pulling the active live orderbook snapshot and tracking depth."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            
            # Check sufficient capital balance
            cursor.execute("SELECT cash_balance FROM accounts WHERE account_id = ?", (account_id,))
            balance_res = cursor.fetchone()
            if not balance_res or balance_res[0] < cash_amount:
                return {"status": "REJECTED", "reason": "Insufficient paper balance"}

            # Get sorted book slice
            _, sorted_asks = self.book_manager.get_snapshot(token_id)
            if not sorted_asks:
                return {"status": "REJECTED", "reason": "No orderbook liquidity available"}

            # Calculate execution properties via walking the book
            filled_qty, avg_price, slippage, consumed_levels = calculate_buy_slippage(sorted_asks, cash_amount)
            
            if filled_qty == 0.0:
                return {"status": "REJECTED", "reason": "Liquidity exhaustion matching thresholds"}

            # Deduct local copy immediately to prevent simultaneous double liquidity usage
            for price, size in consumed_levels:
                self.book_manager.inject_consumed_liquidity(token_id, 'ask', price, size)

            # Persistence Steps
            # 1. Deduct capital balance
            cursor.execute("UPDATE accounts SET cash_balance = cash_balance - ? WHERE account_id = ?", (cash_amount, account_id))
            
            # 2. Record individual execution fill with tracking metrics
            cursor.execute("""
                INSERT INTO paper_fills (account_id, condition_id, token_id, order_side, requested_value, filled_qty, avg_fill_price, slippage_paid)
                VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?)
            """, (account_id, condition_id, token_id, cash_amount, filled_qty, avg_price, slippage))
            
            # 3. Snapshot exact orderbook state layers consumed for audit records
            for price, size in consumed_levels:
                cursor.execute("""
                    INSERT INTO orderbook_state (condition_id, token_id, side, price, size)
                    VALUES (?, ?, 'ask', ?, ?)
                """, (condition_id, token_id, price, size))

            # 4. Upsert aggregated position average entries
            cursor.execute("""
                SELECT qty, avg_entry_price FROM paper_positions 
                WHERE account_id = ? AND condition_id = ? AND token_id = ?
            """, (account_id, condition_id, token_id))
            
            pos_res = cursor.fetchone()
            if pos_res:
                current_qty, current_avg = pos_res
                new_qty = current_qty + filled_qty
                new_avg = ((current_qty * current_avg) + (filled_qty * avg_price)) / new_qty
                cursor.execute("""
                    UPDATE paper_positions SET qty = ?, avg_entry_price = ? 
                    WHERE account_id = ? AND condition_id = ? AND token_id = ?
                """, (new_qty, new_avg, account_id, condition_id, token_id))
            else:
                cursor.execute("""
                    INSERT INTO paper_positions (account_id, condition_id, token_id, side, qty, avg_entry_price)
                    VALUES (?, ?, ?, 'YES', ?, ?)
                """, (account_id, condition_id, token_id, filled_qty, avg_price))
                
            conn.commit()
            
            return {
                "status": "FILLED",
                "qty": filled_qty,
                "avg_price": avg_price,
                "slippage": slippage
            }
        finally:
            conn.close()

    def execute_paper_sell(self, account_id, condition_id, token_id, cash_amount, market_midpoint):
        """Execute a paper SELL (short YES / long NO).
        Selling YES means we receive cash but take on liability.
        Profit = market_price drops below our sell price.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # Get sorted bids (people buying YES)
            sorted_bids, _ = self.book_manager.get_snapshot(token_id)
            if not sorted_bids:
                return {"status": "REJECTED", "reason": "No bid-side liquidity available"}

            # Sell cash_amount / avg_price worth of shares
            # For SELL: we receive cash, take on negative position
            # Walk the bid side to find how many shares we can sell at
            filled_qty, avg_sell_price, slippage, consumed_levels = calculate_sell_slippage(
                sorted_bids, cash_amount / market_midpoint  # approximate qty
            )

            if filled_qty == 0.0:
                return {"status": "REJECTED", "reason": "Liquidity exhaustion"}

            # Consume bid-side liquidity
            for price, size in consumed_levels:
                self.book_manager.inject_consumed_liquidity(token_id, 'bid', price, size)

            # 1. Add cash from the sale
            proceeds = filled_qty * avg_sell_price
            cursor.execute("UPDATE accounts SET cash_balance = cash_balance + ? WHERE account_id = ?", (proceeds, account_id))

            # 2. Record fill
            cursor.execute("""
                INSERT INTO paper_fills (account_id, condition_id, token_id, order_side, requested_value, filled_qty, avg_fill_price, slippage_paid)
                VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?)
            """, (account_id, condition_id, token_id, cash_amount, filled_qty, avg_sell_price, slippage))

            # 3. Snapshot orderbook state
            for price, size in consumed_levels:
                cursor.execute("""
                    INSERT INTO orderbook_state (condition_id, token_id, side, price, size)
                    VALUES (?, ?, 'bid', ?, ?)
                """, (condition_id, token_id, price, size))

            # 4. Upsert position (negative qty = short position)
            cursor.execute("""
                SELECT qty, avg_entry_price FROM paper_positions
                WHERE account_id = ? AND condition_id = ? AND token_id = ?
            """, (account_id, condition_id, token_id))

            pos_res = cursor.fetchone()
            if pos_res:
                current_qty, current_avg = pos_res
                new_qty = current_qty - filled_qty
                if abs(new_qty) > 0.01:
                    # Recompute weighted avg (handles both long and short)
                    new_avg = ((current_qty * current_avg) - (filled_qty * avg_sell_price)) / new_qty
                    cursor.execute("""
                        UPDATE paper_positions SET qty = ?, avg_entry_price = ?
                        WHERE account_id = ? AND condition_id = ? AND token_id = ?
                    """, (new_qty, new_avg, account_id, condition_id, token_id))
                else:
                    # Position closed
                    cursor.execute("""
                        UPDATE paper_positions SET qty = 0, avg_entry_price = 0, status = 'CLOSED'
                        WHERE account_id = ? AND condition_id = ? AND token_id = ?
                    """, (account_id, condition_id, token_id))
            else:
                # New short position
                cursor.execute("""
                    INSERT INTO paper_positions (account_id, condition_id, token_id, side, qty, avg_entry_price)
                    VALUES (?, ?, ?, 'NO', ?, ?)
                """, (account_id, condition_id, token_id, -filled_qty, avg_sell_price))

            conn.commit()

            return {
                "status": "FILLED",
                "qty": filled_qty,
                "avg_price": avg_sell_price,
                "slippage": slippage
            }
        finally:
            conn.close()

    def _resolve_token_id(self, cursor, corrupted_id):
        """Match corrupted scientific-notation token_id back to outcome_name."""
        if not corrupted_id:
            return None
        # If already a full string, look it up directly
        cursor.execute("SELECT outcome_name FROM market_outcomes WHERE yes_token_id = ?", (corrupted_id,))
        row = cursor.fetchone()
        if row:
            return row[0]
        # Convert scientific notation to full integer prefix (12 chars)
        try:
            full = f'{float(corrupted_id):.0f}'[:12]
            cursor.execute("SELECT outcome_name FROM market_outcomes WHERE yes_token_id LIKE ?", (f'{full}%',))
            row = cursor.fetchone()
            return row[0] if row else None
        except (ValueError, TypeError):
            pass
        return None

    def get_trading_summary_html(self):
        """Generates an HTML component summary grid for dashboard insertion."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            
            # Balance details
            cursor.execute("SELECT cash_balance FROM accounts WHERE account_id = 'paper_user'")
            balance = cursor.fetchone()[0]
            
            # Active positions per token
            cursor.execute("""
                SELECT pp.condition_id, pp.token_id, pp.qty, pp.avg_entry_price
                FROM paper_positions pp
                WHERE pp.qty != 0 AND pp.status != 'CLOSED'
            """)
            positions = cursor.fetchall()
            
            # Latest fills
            cursor.execute("""
                SELECT condition_id, token_id, order_side, requested_value, filled_qty, 
                       avg_fill_price, slippage_paid, timestamp
                FROM paper_fills ORDER BY timestamp DESC
            """)
            fills = cursor.fetchall()
            
            # Build token_id -> outcome_name lookup
            outcome_map = {}
            for pos in positions:
                tid = pos[1]
                cond = pos[0]
                if tid not in outcome_map:
                    outcome_map[tid] = self._resolve_token_id(cursor, tid)
            
            # Fetch current prices from Polymarket CLOB API for PnL
            current_prices = {}
            cursor.execute("SELECT condition_id, yes_token_id, outcome_name FROM market_outcomes")
            all_outcomes = cursor.fetchall()
            
            # Build corrupted -> valid token_id mapping
            corr_to_valid = {}
            for pos in positions:
                corr_tid = pos[1]
                outcome_name = outcome_map.get(corr_tid)
                cond = pos[0]
                if outcome_name:
                    cursor.execute("SELECT yes_token_id FROM market_outcomes WHERE outcome_name = ? AND condition_id = ?", (outcome_name, cond))
                    match = cursor.fetchone()
                    if match:
                        corr_to_valid[corr_tid] = match[0]
            
            # Fetch current market prices from dashboard's Polymarket data for PnL
            current_prices = {}
            try:
                from hko_weather_monitor.polymarket import fetch_active_hk_polymarket
                pm_data = fetch_active_hk_polymarket() or []
                for market in pm_data:
                    token_ids = market.get('token_ids', [])
                    outcomes = market.get('outcomes', [])
                    for i, outcome in enumerate(outcomes):
                        # YES/NO pairs: YES at i*2, NO at i*2+1
                        yes_idx = i * 2
                        if yes_idx < len(token_ids):
                            tid = token_ids[yes_idx]
                            price = outcome.get('yes_price', 0) / 100.0  # Polymarket returns cents
                            current_prices[tid] = price
                # Map corrupted -> valid prices
                for corr_tid, valid_tid in corr_to_valid.items():
                    if valid_tid in current_prices:
                        current_prices[corr_tid] = current_prices[valid_tid]
            except Exception:
                pass
            
            html = f'<div style="padding:15px;background:#1a1a2e;border:1px solid #333;border-radius:8px;margin-bottom:15px"><div style="color:#888;font-size:14px">Available Balance</div><div style="font-size:28px;color:#4caf50">${balance:,.2f}</div></div>'
            
            if not positions:
                html += '<div style="color:#888;padding:20px;">No active positions.</div>'
            else:
                # === POSITIONS ===
                html += "<div style='font-weight:bold;color:#fff;margin-bottom:8px'>📊 Current Positions</div>"
                html += "<table border='1' style='border-collapse:collapse;width:100%;border-color:#333'>"
                html += "<tr style='background:#16213e'><th style='padding:6px;font-size:12px'>Market</th><th style='padding:6px;font-size:12px'>Outcome</th><th style='padding:6px;font-size:12px;text-align:right'>Shares</th><th style='padding:6px;font-size:12px;text-align:right'>Avg Entry</th><th style='padding:6px;font-size:12px;text-align:right'>Current</th><th style='padding:6px;font-size:12px;text-align:right'>PnL</th></tr>"
                
                total_pnl = 0
                for pos in positions:
                    condition_id = pos[0] or ''
                    tid = pos[1]
                    qty = pos[2]
                    avg_price = pos[3]
                    date_part = condition_id.split('T')[0].replace('hk_temp_', '') if 'T' in condition_id else ''
                    outcome = outcome_map.get(tid, '?') or '?'
                    
                    curr_price = current_prices.get(tid, avg_price)
                    pnl = (curr_price - avg_price) * qty
                    total_pnl += pnl
                    pnl_color = '#4caf50' if pnl >= 0 else '#e57373'
                    
                    html += f"<tr style='border-color:#333'><td style='padding:6px;color:#aaa;font-size:11px'>{date_part}</td><td style='padding:6px'>{outcome}</td><td style='padding:6px;text-align:right'>{qty:,.2f}</td><td style='padding:6px;text-align:right'>${avg_price:.4f}</td><td style='padding:6px;text-align:right'>${curr_price:.4f}</td><td style='padding:6px;text-align:right;color:{pnl_color}'><b>${pnl:.2f}</b></td></tr>"
                html += "</table>"
                
                # Total PnL
                total_color = '#4caf50' if total_pnl >= 0 else '#e57373'
                html += f"<div style='text-align:right;padding:8px;color:{total_color};font-weight:bold'>Total Unrealized PnL: ${total_pnl:.2f}</div>"
            
            # === TRANSACTION HISTORY ===
            if fills:
                html += "<div style='font-weight:bold;color:#fff;margin:12px 0 8px'>📜 Transaction History</div>"
                html += "<table border='1' style='border-collapse:collapse;width:100%;border-color:#333;font-size:12px'>"
                html += "<tr style='background:#16213e'><th style='padding:6px'>Time</th><th style='padding:6px'>Market</th><th style='padding:6px'>Outcome</th><th style='padding:6px'>Side</th><th style='padding:6px;text-align:right'>Amount</th><th style='padding:6px;text-align:right'>Shares</th><th style='padding:6px;text-align:right'>Price</th><th style='padding:6px;text-align:right'>Slippage</th></tr>"
                
                for fill in fills:
                    cond_id = fill[0] or ''
                    fill_tid = fill[1]
                    side = fill[2]
                    amount = fill[3]
                    f_qty = fill[4]
                    f_price = fill[5]
                    f_slip = fill[6]
                    f_time = fill[7] or ''
                    date_part = cond_id.split('T')[0].replace('hk_temp_', '') if 'T' in str(cond_id) else ''
                    fill_outcome = self._resolve_token_id(cursor, fill_tid) or '?'
                    side_color = '#4caf50' if side == 'BUY' else '#e57373'
                    
                    html += f"<tr style='border-color:#333'><td style='padding:6px;color:#888'>{f_time}</td><td style='padding:6px'>{date_part}</td><td style='padding:6px'>{fill_outcome}</td><td style='padding:6px;color:{side_color};font-weight:bold'>{side}</td><td style='padding:6px;text-align:right'>${amount:.2f}</td><td style='padding:6px;text-align:right'>{f_qty:,.2f}</td><td style='padding:6px;text-align:right'>${f_price:.4f}</td><td style='padding:6px;text-align:right'>{f_slip:.4f}</td></tr>"
                html += "</table>"
            
            return html
        finally:
            conn.close()
