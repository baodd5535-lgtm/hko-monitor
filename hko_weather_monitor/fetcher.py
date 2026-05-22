"""HKO weather data fetcher — per-minute temperature from wxinfo/awsgis/{station}.csv."""
import csv
import io
import logging
import requests
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger(__name__)

BASE_URL = "https://www.hko.gov.hk/wxinfo/awsgis/{station}.csv"

# All station codes from the portal (38 with per-minute data)
STATION_CODES = [
    "hko", "hka", "sha", "skg", "ty1", "lfs", "tkl", "cch", "kp", "wgl",
    "tms", "tc", "cwb", "wlp", "tu1", "hks", "se1", "jkb", "plc", "tyw",
    "ylp", "sek", "yct", "ngp", "vp1", "pen", "ssh", "tw", "hkp", "skw",
    "klt", "hpv", "ksc", "wts", "sty", "ktg", "ssp",
]

# Station name mapping
STATION_NAMES = {
    "hko": "HK Observatory", "hka": "Chek Lap Kok", "sha": "Sha Tin",
    "skg": "Shek Kong", "ty1": "Tai Mei Tuk", "lfs": "Lau Fau Shan",
    "tkl": "Ta Kwu Ling", "cch": "Cheung Chau", "kp": "King's Park",
    "wgl": "Waglan Island", "tms": "Tai Mo Shan", "tc": "Tate's Cairn",
    "cwb": "Clear Water Bay", "wlp": "Wetland Park", "tu1": "Tuen Mun",
    "hks": "HK Park", "se1": "Sheung Shui", "jkb": "Kai Tak Runway Park",
    "plc": "Pak Tam Chung", "tyw": "Tsuen Wan Shing Mun Valley",
    "ylp": "Yuen Long Park", "sek": "Shek Kong", "yct": "Yau Tsim Mong",
    "ngp": "Ngong Ping", "vp1": "Victoria Park", "pen": "Peng Chau",
    "ssh": "Sha Tin", "tw": "Tai Wo", "hkp": "HK Park", "skw": "Shau Kei Wan",
    "klt": "Kwun Tong", "hpv": "Happy Valley", "ksc": "Kau Sai Chau",
    "wts": "Wong Tai Sin", "sty": "Stanley", "ktg": "Kowloon Tong",
    "ssp": "Sham Shui Po",
}


def fetch_all_per_minute() -> List[Dict]:
    """Fetch per-minute temperature data for all stations.

    Returns list of dicts:
        [{"timestamp": "2026/05/20 10:40", "station": "HK Observatory",
          "temperature": 28.1, "humidity": 79}, ...]
    """
    all_rows = []
    for code in STATION_CODES:
        try:
            resp = requests.get(BASE_URL.format(station=code), timeout=10)
            if resp.status_code != 200:
                logger.warning("Station %s returned %d", code, resp.status_code)
                continue
            reader = csv.DictReader(io.StringIO(resp.text))
            for row in reader:
                temp = row.get("Temp", "").strip()
                rh = row.get("RH", "").strip()
                all_rows.append({
                    "timestamp": row["Date"].strip(),
                    "station": STATION_NAMES.get(code, code),
                    "temperature": float(temp) if temp not in ("M", "", "N/A") else None,
                    "humidity": int(rh) if rh not in ("M", "", "N/A") else None,
                })
        except Exception as e:
            logger.error("Failed to fetch station %s: %s", code, e)
    logger.info("Fetched %d per-minute readings across %d stations",
                len(all_rows), len(STATION_CODES))
    return all_rows


def parse_timestamp(ts: str) -> datetime:
    """Parse '2026/05/20 10:40' -> datetime."""
    return datetime.strptime(ts, "%Y/%m/%d %H:%M")


# ─── OCF Forecast Fetcher ───────────────────────────────────────

FORECAST_BASE_URL = "https://maps.weather.gov.hk/ocf/dat/{station}.xml"

# 16 OCF forecast stations (uppercase codes)
FORECAST_STATIONS = [
    "CCH", "HKA", "HKO", "HKS", "JKB", "LFS", "PEN", "SEK",
    "SHA", "SKG", "TKL", "TPO", "TUN", "TY1", "WGL", "SSH",
]

# OCF station display names
FORECAST_STATION_NAMES = {
    "CCH": "Cheung Chau", "HKA": "Chek Lap Kok", "HKO": "HK Observatory",
    "HKS": "HK Park", "JKB": "Kai Tak", "LFS": "Lau Fau Shan",
    "PEN": "Peng Chau", "SEK": "Sheung Shui", "SHA": "Sha Tin",
    "SKG": "Shek Kong", "TKL": "Ta Kwu Ling", "TPO": "Tseung Kwan O",
    "TUN": "Tuen Mun", "TY1": "Tai Mei Tuk", "WGL": "Waglan Island",
    "SSH": "Sha Tau Kok",
}

# Weather code to description mapping (from HKO icons)
WEATHER_CODES = {
    0: "Clear", 1: "Mostly Clear", 2: "Mostly Cloudy", 3: "Cloudy",
    50: "Sunny Intervals", 51: "Sunny", 52: "Sunny Intervals",
    53: "Sunny", 54: "Mostly Sunny", 60: "Cloudy", 61: "Mostly Cloudy",
    62: "Light Rain", 63: "Overcast Showers", 64: "Showers",
    71: "Moderate Rain", 72: "Heavy Rain", 73: "Thunderstorm",
    74: "Thunderstorm & Heavy Rain", 76: "Severe Thunderstorm",
    81: "Haze", 82: "Smog", 83: "Mist",
}


def fetch_all_forecasts() -> tuple:
    """Fetch OCF forecasts for all 16 stations.

    Returns (hourly_records, daily_records) tuples ready for DB insert.
    """
    hourly_records = []
    daily_records = []

    for code in FORECAST_STATIONS:
        try:
            resp = requests.get(FORECAST_BASE_URL.format(station=code), timeout=10)
            if resp.status_code != 200:
                logger.warning("Forecast station %s returned %d", code, resp.status_code)
                continue

            data = resp.json()
            model_time = str(data.get("ModelTime", ""))
            last_modified = str(data.get("LastModified", ""))

            # Hourly forecasts
            for hf in data.get("HourlyWeatherForecast", []):
                hourly_records.append((
                    code,
                    str(hf.get("ForecastHour", "")),
                    hf.get("ForecastTemperature"),
                    hf.get("ForecastRelativeHumidity"),
                    hf.get("ForecastWindSpeed"),
                    hf.get("ForecastWindDirection"),
                    model_time,
                    last_modified,
                ))

            # Daily forecasts
            for df in data.get("DailyForecast", []):
                daily_records.append((
                    code,
                    str(df.get("ForecastDate", "")),
                    df.get("ForecastMaximumTemperature"),
                    df.get("ForecastMinimumTemperature"),
                    df.get("ForecastChanceOfRain"),
                    df.get("ForecastDailyWeather"),
                    model_time,
                    last_modified,
                ))

        except Exception as e:
            logger.error("Failed to fetch forecast for %s: %s", code, e)

    logger.info("Fetched %d hourly, %d daily forecast records across %d stations",
                len(hourly_records), len(daily_records), len(FORECAST_STATIONS))
    return hourly_records, daily_records


def weather_code_description(code: int) -> str:
    """Convert HKO weather code to description."""
    return WEATHER_CODES.get(code, f"Unknown ({code})")


# ─── Adaptive sentinel polling (Last-Modified based) ─────────────

def check_last_modified(url: str) -> str | None:
    """HEAD request — return Last-Modified header or None on failure."""
    try:
        resp = requests.head(url, timeout=5, allow_redirects=True)
        return resp.headers.get("Last-Modified")
    except Exception as e:
        logger.debug("Last-Modified check failed for %s: %s", url, e)
        return None


def sentinel_changed(station: str = "hko") -> bool:
    """Quick HEAD on sentinel station — returns True if Last-Modified changed."""
    lm = check_last_modified(BASE_URL.format(station=station))
    if lm is None:
        return False
    old = getattr(sentinel_changed, "_lm", None)
    if lm != old:
        sentinel_changed._lm = lm  # cache for next comparison
        return True
    return False


def reset_sentinel():
    """Clear cached Last-Modified (call after full fetch to detect next change)."""
    if hasattr(sentinel_changed, "_lm"):
        delattr(sentinel_changed, "_lm")


# ─── 9-Day Forecast Scraper ───────────────────────────────────────

NINE_DAY_URL = "https://www.hko.gov.hk/tc/wxinfo/currwx/fnd.htm"


# ─── 9-Day Forecast Scraper ───────────────────────────────────────

NINE_DAY_URL = "https://www.hko.gov.hk/json/DYN_DAT_MINDS_FND.json"

def fetch_nine_day_forecast():
    """Fetch 9-day forecast from HKO JSON API."""
    try:
        resp = requests.get(NINE_DAY_URL, timeout=15)
        resp.encoding = 'utf-8'
        data = resp.json()['DYN_DAT_MINDS_FND']
        
        result = []
        for i in range(1, 10):
            date_key = f"Day{i}ForecastDate"
            if date_key not in data:
                break
            
            date = data[date_key]['Value_Eng']
            min_temp = data.get(f"Day{i}MinTemp", {}).get('Value_Eng', 0)
            max_temp = data.get(f"Day{i}MaxTemp", {}).get('Value_Eng', 0)
            min_rh = data.get(f"Day{i}MinRH", {}).get('Value_Eng', 0)
            max_rh = data.get(f"Day{i}MaxRH", {}).get('Value_Eng', 0)
            rain_prob = data.get(f"Day{i}PSR10", {}).get('Value_Eng', '')
            weather_desc = data.get(f"Day{i}WxDesc", {}).get('Value_Eng', '')
            wind_info = data.get(f"Day{i}WindInfo", {}).get('Value_Eng', '')
            wx_icon = data.get(f"Day{i}WxIcon", {}).get('Value_Eng', '')
            
            result.append({
                'date_str': f"{date[4:6]}/{date[6:8]}",
                'forecast_date': date,
                'min_temp': int(min_temp),
                'max_temp': int(max_temp),
                'rh_range': f"{min_rh}-{max_rh}",
                'rain_prob': rain_prob,
                'weather_desc': weather_desc,
                'wind_info': wind_info,
                'wx_icon': wx_icon,
            })
        
        logger.info("Fetched %d 9-day forecast entries", len(result))
        return result
    except Exception as e:
        logger.error("Failed to fetch 9-day forecast: %s", e)
        return []
