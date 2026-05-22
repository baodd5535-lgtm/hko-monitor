"""Slippage calculation and liquidity walking algorithms."""


def calculate_buy_slippage(sorted_asks, target_cash):
    """
    Simulates purchasing contracts using a fixed USD cash amount by walking up the asks.
    Returns: (filled_qty, avg_price, slippage, consumed_levels)
    """
    if not sorted_asks:
        return 0.0, 0.0, 0.0, []

    remaining_cash = target_cash
    total_qty = 0.0
    total_cost = 0.0
    consumed_levels = []
    
    initial_best_ask = sorted_asks[0][0]

    for price, size in sorted_asks:
        if remaining_cash <= 0:
            break
            
        # Maximum available cash cost at this specific level
        max_level_cost = price * size
        
        if remaining_cash >= max_level_cost:
            # Consume whole level
            total_qty += size
            total_cost += max_level_cost
            remaining_cash -= max_level_cost
            consumed_levels.append((price, size))
        else:
            # Consume partial level
            partial_qty = remaining_cash / price
            total_qty += partial_qty
            total_cost += remaining_cash
            remaining_cash = 0
            consumed_levels.append((price, partial_qty))
            break

    if total_qty == 0.0:
        return 0.0, 0.0, 0.0, []

    avg_price = total_cost / total_qty
    slippage = avg_price - initial_best_ask
    
    return total_qty, avg_price, slippage, consumed_levels


def calculate_sell_slippage(sorted_bids, target_qty):
    """
    Simulates selling contracts by walking down the bids.
    Returns: (filled_qty, avg_price, slippage, consumed_levels)
    """
    if not sorted_bids:
        return 0.0, 0.0, 0.0, []

    remaining_qty = target_qty
    total_qty = 0.0
    total_revenue = 0.0
    consumed_levels = []
    
    initial_best_bid = sorted_bids[0][0]

    for price, size in sorted_bids:
        if remaining_qty <= 0:
            break
            
        if remaining_qty >= size:
            # Sell whole level
            total_qty += size
            total_revenue += price * size
            remaining_qty -= size
            consumed_levels.append((price, size))
        else:
            # Sell partial level
            partial_qty = remaining_qty
            total_qty += partial_qty
            total_revenue += price * partial_qty
            remaining_qty = 0
            consumed_levels.append((price, partial_qty))
            break

    if total_qty == 0.0:
        return 0.0, 0.0, 0.0, []

    avg_price = total_revenue / total_qty
    slippage = initial_best_bid - avg_price  # Positive means slippage against us
    
    return total_qty, avg_price, slippage, consumed_levels
