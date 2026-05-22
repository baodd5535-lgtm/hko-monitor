"""Fetch Polymarket HK temperature market data from Gamma API.

Per official docs:
  GET /events/slug/{slug}  — fetch single event by slug
  GET /events              — list events with tag_slug, active, etc.
"""
import json
import re
import requests
import logging
from datetime import date, timedelta

BASE = "https://gamma-api.polymarket.com"
logger = logging.getLogger(__name__)


def _fetch_by_slug(slug):
    """GET /events/slug/{slug} — official endpoint."""
    try:
        resp = requests.get(f"{BASE}/events/slug/{slug}", timeout=10)
        if resp.status_code == 200:
            event = resp.json()
            return parse_polymarket_event(event)
    except Exception:
        pass
    return None


def _slug_for_date(d):
    """Build slug: highest-temperature-in-hong-kong-on-Month-DD-YYYY."""
    month_name = d.strftime("%B").lower()  # may
    return f"highest-temperature-in-hong-kong-on-{month_name}-{d.day}-{d.year}"


def fetch_hk_polymarket(date_str=None):
    """Fetch market for a single date (YYYY-MM-DD or None=today)."""
    if date_str:
        d = date.fromisoformat(date_str)
    else:
        d = date.today()
    return _fetch_by_slug(_slug_for_date(d))


def fetch_active_hk_polymarket():
    """
    Fetch all active HK temperature markets using robust discovery.
    
    Strategy:
    1. Tag-based search (primary) — query all active events, filter for HK temp
    2. Slug guessing (fallback) — try today + next 5 days with known slug pattern
    
    This handles slug format changes and discovers markets outside the 6-day window.
    """
    results = []
    
    # Method 1: Tag-based discovery (primary, most robust)
    try:
        resp = requests.get(
            f"{BASE}/events",
            params={"active": True, "limit": 100, "closed": False},
            timeout=15,
        )
        if resp.status_code == 200:
            events = resp.json()
            for event in events:
                title = event.get("title", "").lower()
                slug = event.get("slug", "").lower()
                
                # Match any HK temperature-related market
                if any(kw in title or kw in slug for kw in [
                    "hong kong", "hko", "highest-temperature-in-hong-kong",
                    "hk temperature", "hk temp",
                ]):
                    parsed = parse_polymarket_event(event)
                    if parsed:
                        results.append(parsed)
    except Exception:
        pass
    
    # Method 2: Slug guessing (fallback for any missed markets)
    if not results:
        logger.warning("Tag-based discovery returned no results, falling back to slug guessing")
        for delta in range(6):
            d = date.today() + timedelta(days=delta)
            event = _fetch_by_slug(_slug_for_date(d))
            if event and event not in results:
                results.append(event)
    
    # Deduplicate by slug
    seen = set()
    unique = []
    for r in results:
        slug = r.get("slug", "")
        if slug not in seen:
            seen.add(slug)
            unique.append(r)
    
    logger.info(f"Discovered {len(unique)} active HK temperature markets")
    return unique


# ─── Tag-based discovery (fallback) ──────────────────────────

def _fetch_by_tag(tag_slug):
    """GET /events?tag_slug=X — discover events by tag."""
    try:
        resp = requests.get(
            f"{BASE}/events",
            params={"tag_slug": tag_slug, "active": True, "limit": 20},
            timeout=10,
        )
        if resp.status_code == 200:
            events = resp.json()
            results = []
            for ev in events:
                if "hong kong" in ev.get("title", "").lower():
                    parsed = parse_polymarket_event(ev)
                    if parsed:
                        results.append(parsed)
            return results
    except Exception:
        pass
    return []


# ─── Parsing ──────────────────────────────────────────────────

def parse_polymarket_event(event):
    """Parse an event dict into our compact format."""
    markets = event.get("markets", [])
    if not markets:
        return None

    outcomes = []
    token_ids = []
    for m in markets:
        title = m.get("groupItemTitle", m.get("question", ""))
        prices = m.get("outcomePrices", [])
        volume = m.get("volumeNum") or m.get("volume", 0)
        liquidity = m.get("liquidityNum") or m.get("liquidity", 0)

        yes_price = 0.0
        try:
            raw = prices if isinstance(prices, list) else json.loads(prices)
            yes_price = float(raw[0])
        except (IndexError, ValueError, TypeError):
            pass

        temp_str = extract_temp_from_title(title)
        outcomes.append({
            "label": title,
            "temp": temp_str,
            "yes_price": round(yes_price * 100, 1),
            "volume": volume,
            "liquidity": liquidity,
        })

        # Extract CLOB token IDs for WebSocket subscription
        clob_tokens_raw = m.get("clobTokenIds", [])
        if clob_tokens_raw:
            if isinstance(clob_tokens_raw, str):
                # API returns JSON string, need to parse it
                try:
                    clob_tokens_raw = json.loads(clob_tokens_raw)
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(clob_tokens_raw, list):
                token_ids.extend([str(t) for t in clob_tokens_raw])

    outcomes.sort(key=lambda x: sort_temp_key(x.get("temp", "")))

    return {
        "title": event.get("title", ""),
        "slug": event.get("slug", ""),
        "date": event.get("endDate", ""),
        "outcomes": outcomes,
        "token_ids": token_ids,  # CLOB token IDs for WebSocket
        "total_volume": event.get("volume", 0),
        "liquidity": event.get("liquidity", 0),
        "url": f"https://polymarket.com/event/{event.get('slug', '')}",
    }


def extract_temp_from_title(title):
    """'20°C or below' → '20-', '25°C' → '25', '30°C or higher' → '30+'."""
    m = re.search(r"(\d+)", title)
    if not m:
        return title
    t = int(m.group(1))
    low = title.lower()
    if "below" in low:
        return f"{t}-"
    if "higher" in low or "above" in low:
        return f"{t}+"
    return str(t)


def sort_temp_key(temp_str):
    """Sort: below ranges → exact temps → above ranges."""
    if not temp_str:
        return (0, 0, "")
    m = re.search(r"(\d+)", temp_str)
    t = int(m.group(1)) if m else 0
    if temp_str.endswith("-"):
        return (0, t, "")
    if temp_str.endswith("+"):
        return (2, t, "")
    return (1, t, "")


def compute_expected_temp(outcomes):
    """Compute market-implied temperature estimate.

    Returns dict with:
      - lower_bound: weighted mean using bucket lower bounds (conservative)
      - mode: the most probable bucket
      - mode_pct: probability of the mode

    NOTE: Because Polymarket uses categorical buckets with open-ended
    ranges (e.g. '30+'), a true expected value is impossible without
    assuming a distribution shape. The lower_bound is the most reliable
    metric — it tells you the minimum the market prices in.
    """
    total_lower = 0.0
    weight = 0.0
    mode = None
    mode_pct = 0.0

    for o in outcomes:
        t_lower = _lower_bound(o.get("temp", ""))
        if t_lower is None:
            continue
        p = o.get("yes_price", 0)
        total_lower += t_lower * p
        weight += p
        if p > mode_pct:
            mode_pct = p
            mode = o.get("temp", "?")

    return {
        "lower_bound": round(total_lower / weight, 1) if weight > 0 else None,
        "mode": mode or "?",
        "mode_pct": round(mode_pct, 1),
    }


def _lower_bound(temp_str):
    """Return the lower bound temperature of a bucket.
    '25' → 25, '20-' → 19, '30+' → 30.
    """
    m = re.search(r"(\d+)", temp_str)
    if not m:
        return None
    t = int(m.group(1))
    if temp_str.endswith("-"):
        return t - 1
    return t  # exact or '+' → the threshold itself
