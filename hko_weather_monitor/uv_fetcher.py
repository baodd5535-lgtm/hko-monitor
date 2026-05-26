"""UV Index data fetching from HKO."""
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List


UV_DATA_URLS = {
    'uv15min_daws': 'https://www.hko.gov.hk/wxinfo/uvinfo/record/uv15min_daws.txt',
    'uv15min': 'https://www.hko.gov.hk/wxinfo/uvinfo/record/uv15min.txt',
    'uvhourly': 'https://www.hko.gov.hk/wxinfo/uvinfo/record/uvhourly.txt',
}


def fetch_uv_data(data_type: str = 'uvhourly') -> Optional[Dict]:
    """Fetch UV data from HKO.
    
    Returns dict with:
        - date: YYYYMMDD string
        - data: list of {time, uv_index} dicts
    """
    url = UV_DATA_URLS.get(data_type)
    if not url:
        return None
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return parse_uv_data(response.text)
    except Exception as e:
        print(f"Failed to fetch UV data ({data_type}): {e}")
        return None


def parse_uv_data(text: str) -> Optional[Dict]:
    """Parse UV data from HKO text format."""
    lines = text.strip().split('\n')
    if not lines:
        return None
    
    date_str = lines[0].strip()
    data = []
    
    for line in lines[1:]:
        line = line.strip()
        if not line or line == '--':
            continue
        
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        
        try:
            time_val = float(parts[0])
            uv_val = parts[1].strip()
            if uv_val == '--' or not uv_val:
                uv_val = None
            else:
                uv_val = float(uv_val)
            
            data.append({
                'time': time_val,
                'uv_index': uv_val,
            })
        except (ValueError, IndexError):
            continue
    
    return {
        'date': date_str,
        'data': data,
    }


def _estimate_uv_from_cloud_and_season(date_str: str, cloud_cover_pct: float) -> float:
    """Estimate peak UV index for a future date based on cloud cover and season.

    HKO doesn't publish UV forecasts — only same-day observations.
    Use a physics-based estimate: latitude 22.3°N, seasonal solar zenith,
    attenuated by cloud cover (approximate).
    """
    from datetime import datetime
    try:
        dt = datetime.strptime(date_str, '%Y%m%d')
    except ValueError:
        return 5.0  # default moderate

    # Day of year (1=Jan 1, 365=Dec 31)
    doy = dt.timetuple().tm_yday

    # Approximate peak UV index for HK (22.3°N) by day of year
    # Max ~12-13 in mid-August, min ~4-5 in January
    # Formula: UV_peak_clear = 8.5 + 4.5 * sin(2π * (doy - 105) / 365)
    import math
    base_uv = 8.5 + 4.5 * math.sin(2 * math.pi * (doy - 105) / 365.0)

    # Cloud attenuation (approximate)
    # 0% cloud → 100% UV, 100% cloud → ~20% UV
    cloud_factor = 1.0 - 0.8 * (cloud_cover_pct / 100.0)
    estimated_uv = base_uv * cloud_factor

    return max(0.5, round(estimated_uv, 1))


def get_peak_uv_index(
    date_str: Optional[str] = None,
    cloud_cover_pct: Optional[float] = None,
) -> Optional[float]:
    """Get peak UV index for today or specified date.

    For today: fetches real observation from HKO uv15min_daws.txt.
    For future dates: estimates based on cloud cover and season (HKO has no UV forecast).

    Args:
        date_str: YYYYMMDD string. None = today.
        cloud_cover_pct: Cloud cover percentage (0-100) for estimation.
                         Only used when date is not today.
    """
    # Try real HKO data first (same-day only)
    uv_data = fetch_uv_data('uv15min_daws')
    if uv_data and (not date_str or uv_data['date'] == date_str):
        peak = 0.0
        for entry in uv_data['data']:
            if entry['uv_index'] is not None:
                peak = max(peak, entry['uv_index'])
        return peak if peak > 0 else None

    # Future date: estimate from cloud cover + season
    if date_str and cloud_cover_pct is not None:
        return _estimate_uv_from_cloud_and_season(date_str, cloud_cover_pct)

    # Fallback: default moderate UV for HK in May
    return None


def get_uv_index_at_time(time_val: float) -> Optional[float]:
    """Get UV index closest to specified time.
    
    time_val: decimal hour (e.g., 12.5 for 12:30)
    """
    uv_data = fetch_uv_data('uv15min')
    if not uv_data:
        return None
    
    closest = None
    min_diff = float('inf')
    
    for entry in uv_data['data']:
        diff = abs(entry['time'] - time_val)
        if diff < min_diff:
            min_diff = diff
            closest = entry
    
    return closest['uv_index'] if closest and min_diff < 0.5 else None


def get_uv_forecast_adjustment(peak_uv: float) -> float:
    """Calculate temperature adjustment based on UV index.
    
    Higher UV means more solar irradiance → higher peak temperatures.
    """
    if peak_uv is None:
        return 0.0
    
    # UV index correlates with solar irradiance and peak temperature potential
    # Low UV (0-2): -0.2°C (likely overcast)
    # Moderate UV (3-5): 0°C (baseline)
    # High UV (6-7): +0.3°C (clear sky heating)
    # Very High UV (8-10): +0.6°C (strong solar irradiance)
    # Extreme UV (11+): +0.8°C (maximum solar heating)
    
    if peak_uv <= 2:
        return -0.2
    elif peak_uv <= 5:
        return 0.0
    elif peak_uv <= 7:
        return 0.3
    elif peak_uv <= 10:
        return 0.6
    else:  # 11+
        return 0.8
