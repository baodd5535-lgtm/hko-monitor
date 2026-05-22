"""Backtest + scoring."""
import math, json, re, requests
from datetime import date, timedelta, datetime, timezone

BASE = "https://gamma-api.polymarket.com"

def gaussian_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))

def fetch_slug(d):
    slug = "highest-temperature-in-hong-kong-on-%s-%d-%d" % (d.strftime("%B").lower(), d.day, d.year)
    r = requests.get("%s/events/slug/%s" % (BASE, slug), timeout=10)
    return r.json() if r.status_code == 200 else None

def parse_outcomes(event):
    outcomes = []
    for m in event.get("markets", []):
        title = m.get("groupItemTitle", "") or m.get("question", "")
        tm = re.search(r"(\d+)", title)
        if not tm:
            continue
        t = int(tm.group(1))
        low = title.lower()
        if "below" in low:
            temp = "%d-" % t
        elif "higher" in low or "above" in low:
            temp = "%d+" % t
        else:
            temp = str(t)
        prices = m.get("outcomePrices", [])
        try:
            raw = prices if isinstance(prices, list) else json.loads(prices)
            yes = float(raw[0])
        except:
            yes = 0
        outcomes.append({"label": title, "temp": temp, "yes": yes})
    outcomes.sort(key=lambda x: int(re.search(r"(\d+)", x["temp"]).group(1)))
    return outcomes

def bucket_bounds(ts):
    t = int(re.search(r"(\d+)", ts).group(1))
    if ts.endswith("-"): return (-100.0, t + 0.5)
    if ts.endswith("+"): return (t - 0.5, 100.0)
    return (t - 0.5, t + 0.5)

def mprob(hko, sigma, ts):
    lo, hi = bucket_bounds(ts)
    return max(0, min(1, gaussian_cdf((hi-hko)/sigma) - gaussian_cdf((lo-hko)/sigma)))

today = date.today()
sigma_map = {0: 0.8, 1: 1.2, 2: 1.8, 3: 2.3, 4: 2.8, 5: 3.3}

events = {}
for d in range(4):
    ev = fetch_slug(today + timedelta(days=d))
    if ev: events[ev["slug"]] = ev

def score_market(ev, hko, sigma):
    outcomes = parse_outcomes(ev)
    print("\n%30s | %s" % ("BUCKET", "%-8s %-8s %-8s %-8s" % ("Mkt%", "Model%", "YES_E", "NO_E")))
    print("-" * 70)
    best_no = None; best_ne = 0
    for o in outcomes:
        mp = o["yes"] * 100
        mp2 = mprob(hko, sigma, o["temp"]) * 100
        ye = mp2 - mp
        ne = mp - mp2
        tag = " <<<" if ne > best_ne else ""
        if ne > best_ne: best_ne = ne; best_no = o
        print("%-30s | %7.1f%% %7.1f%% %7.1f%% %7.1f%%%s" % (o["label"], mp, mp2, ye, ne, tag))
    return best_no, best_ne, outcomes

print("=" * 70)
print("BACKTEST: May 22 (RESOLVED, actual ~31.5C)")
print("=" * 70)
may22 = [s for s in events if "may-22" in s]
if may22:
    bn, be, oc = score_market(events[may22[0]], hko=31.0, sigma=0.8)
    print("\n>> Best NO: %s (edge %.1f%%)" % (bn["label"], be))

print("\n" + "=" * 70)
print("ACTIVE MARKETS")
print("=" * 70)
for slug, ev in events.items():
    if "may-22" in slug: continue
    end = ev.get("endDate", "")
    dm = re.search(r"(\d{4}-\d{2}-\d{2})", end)
    ds = dm.group(1) if dm else ""
    horizon = max(0, (date.fromisoformat(ds) - today).days)
    res = datetime.fromisoformat(end.replace("Z","+00:00"))
    hrs = (res - datetime.now(timezone.utc)).total_seconds()/3600
    sigma = sigma_map.get(horizon, 3.0)
    print("\n%s (h=%dd, %.0fh left, sigma=%.1f)" % (ds, horizon, hrs, sigma))
    bn, be, oc = score_market(ev, hko=31.0, sigma=sigma)
    if bn and be > 5:
        print(">> TRADE: NO on '%s' (edge %.1f%%)" % (bn["label"], be))
    else:
        print(">> NO TRADE (best %.1f%%)" % be)
