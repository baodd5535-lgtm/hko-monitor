import re
import requests
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Station configuration from HKO's stationConfigAWS
STATION_CONFIG = {
    "hko": "HK Observatory",
    "sha": "Sha Tin",
    "lfs": "Lau Fau Shan",
    "tkl": "Ta Kwu Ling",
    "tu1": "Tuen Mun",
    "hks": "Wong Chuk Hang",
    "wgl": "Waglan Island",
    "gi": "Green Island",
    "jkb": "Tseung Kwan O",
    "ccb": "Cheung Chau Beach",
    "kp": "King's Park",
    "plc": "Tai Mei Tuk",
    "slw": "Sha Lo Wan",
    "skg": "Sai Kung",
    "tme": "Tap Mun",
    "tyw": "Pak Tam Chung",
    "sek": "Shek Kong",
    "tms": "Tai Mo Shan",
    "hka": "Chek Lap Kok",
    "tc": "Tate's Cairn",
    "yct": "Tai Po",
    "ngp": "Ngong Ping",
    "vp1": "The Peak",
    "pen": "Peng Chau",
    "ssh": "Sheung Shui",
    "twn": "Tsuen Wan Ho Koon",
    "wlp": "Wetland Park",
    "hkp": "HK Park",
    "skw": "Shau Kei Wan",
    "klt": "Kowloon City",
    "cch": "Cheung Chau",
    "cwb": "Clear Water Bay",
    "brc": "Beas River",
    "cs1": "Cheung Sha",
    "elc": "Elegantia College in Sheung Shui",
    "gsi": "German Swiss International School",
    "hss": "Hong Kong Sea School",
    "ic1": "International Commerce Centre",
    "kfb": "Kadoorie Farm and Botanic Garden",
    "ks2": "Kau Sai Chau",
    "lam": "Lamma Island",
    "np": "North Point",
    "sc": "Sha Chau",
    "se": "Kai Tak",
    "sf": "Star Ferry",
    "swh": "Sai Wan Ho",
    "tlc": "Tai Lam Chung",
    "tpk": "Tai Po Kau",
    "tw": "Tsuen Wan Shing Mun Valley",
    "vpa": "Victoria Peak",
    "zcp": "Kowloon Bay",
}


class HKOApiClient:
    def __init__(self, base_url: str, aws_datafile: str, historical_temp_file: str,
                 timeout: int, max_retries: int, backoff_factor: float):
        self.base_url = base_url.rstrip("/")
        self.aws_datafile = aws_datafile
        self.historical_temp_file = historical_temp_file
        self.timeout = timeout
        self.session = self._create_session(max_retries, backoff_factor)

    def _create_session(self, max_retries: int, backoff_factor: float) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def fetch_temperatures(self) -> Optional[str]:
        """Fetch raw text from HKO AWS data file."""
        url = f"{self.base_url}/{self.aws_datafile}?t={datetime.now().timestamp()}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            logger.debug("API response received successfully")
            return response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            return None

    def fetch_past_temperatures(self) -> Optional[str]:
        """Fetch historical temperature data CSV."""
        url = f"{self.base_url}/{self.historical_temp_file}?t={datetime.now().timestamp()}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            logger.debug("Past temperature data received")
            return response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"Past temperature fetch failed: {e}")
            return None

    def parse_temperatures(self, raw_text: str, station_whitelist: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Parse the latest readings text file."""
        try:
            lines = raw_text.strip().split('\n')
            if len(lines) < 2:
                return []

            # Parse header timestamp
            header = lines[0]
            match = re.search(r'(\d{2}):(\d{2}).*?(\d{1,2})\s+(\w+)\s+(\d{4})', header)
            if not match:
                logger.warning("Could not parse timestamp from header")
                return []

            hour, minute, day, month_str, year = match.groups()
            month_map = {
                'January': 1, 'February': 2, 'March': 3, 'April': 4,
                'May': 5, 'June': 6, 'July': 7, 'August': 8,
                'September': 9, 'October': 10, 'November': 11, 'December': 12
            }
            month = month_map.get(month_str)
            if not month:
                logger.error(f"Unknown month: {month_str}")
                return []

            recorded_at = datetime(int(year), month, int(day), int(hour), int(minute))

            readings = []
            # Skip header and column header
            for line in lines[2:]:
                parts = line.split(',')
                if len(parts) < 5:
                    continue

                station_code = parts[0].strip().lower()
                temp_str = parts[4].strip()
                if not temp_str or temp_str == 'M':
                    continue

                try:
                    temperature = float(temp_str)
                except ValueError:
                    continue

                station_name = STATION_CONFIG.get(station_code, station_code.upper())
                if station_whitelist and station_name not in station_whitelist:
                    continue

                readings.append({
                    "station_code": station_code,
                    "station_name": station_name,
                    "recorded_at": recorded_at,
                    "temperature": temperature,
                })

            logger.info(f"Parsed {len(readings)} temperature readings at {recorded_at}")
            return readings

        except Exception as e:
            logger.exception(f"Failed to parse temperatures: {e}")
            return []

    def parse_past_temperatures(self, raw_text: str, station_whitelist: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Parse the historical temperature CSV."""
        try:
            readings = []
            for line in raw_text.strip().split('\n'):
                if not line.strip():
                    continue

                parts = line.split(',')
                if len(parts) < 3:
                    continue

                timestamp_str = parts[0].strip()
                temp_str = parts[1].strip()
                station_code = parts[2].strip().lower()

                if not temp_str or temp_str == 'M':
                    continue

                try:
                    temperature = float(temp_str)
                except ValueError:
                    continue

                # Parse timestamp YYYYMMDDHHMM
                try:
                    recorded_at = datetime.strptime(timestamp_str, "%Y%m%d%H%M")
                except ValueError:
                    logger.warning(f"Invalid timestamp: {timestamp_str}")
                    continue

                station_name = STATION_CONFIG.get(station_code, station_code.upper())
                if station_whitelist and station_name not in station_whitelist:
                    continue

                readings.append({
                    "station_code": station_code,
                    "station_name": station_name,
                    "recorded_at": recorded_at,
                    "temperature": temperature,
                })

            logger.info(f"Parsed {len(readings)} historical temperature readings")
            return readings

        except Exception as e:
            logger.exception(f"Failed to parse past temperatures: {e}")
            return []

    def fetch_forecast(self, station_code: str, forecast_url_template: str) -> Optional[Dict[str, Any]]:
        """Fetch forecast JSON for a station (case-sensitive uppercase code)."""
        url = forecast_url_template.format(STATION=station_code.upper())
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            logger.debug(f"Forecast received for {station_code}")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Forecast request failed for {station_code}: {e}")
            return None
        except ValueError as e:
            logger.error(f"Invalid JSON in forecast for {station_code}: {e}")
            return None

    def parse_daily_forecast(self, raw_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse daily forecast from forecast JSON."""
        try:
            forecasts = []
            for day in raw_data.get("DailyForecast", []):
                forecast_date_str = day.get("ForecastDate")
                if not forecast_date_str:
                    continue
                try:
                    forecast_date = datetime.strptime(forecast_date_str, "%Y%m%d").date()
                except ValueError:
                    logger.warning(f"Invalid forecast date: {forecast_date_str}")
                    continue

                forecasts.append({
                    "station_code": raw_data.get("StationCode", "").lower(),
                    "station_name": STATION_CONFIG.get(raw_data.get("StationCode", "").lower(), raw_data.get("StationCode", "")),
                    "forecast_date": forecast_date,
                    "max_temperature": float(day["ForecastMaximumTemperature"]) if day.get("ForecastMaximumTemperature") else None,
                    "min_temperature": float(day["ForecastMinimumTemperature"]) if day.get("ForecastMinimumTemperature") else None,
                    "chance_of_rain": day.get("ForecastChanceOfRain"),
                    "weather_code": day.get("ForecastDailyWeather"),
                })

            logger.info(f"Parsed {len(forecasts)} daily forecasts for {raw_data.get('StationCode')}")
            return forecasts

        except Exception as e:
            logger.exception(f"Failed to parse daily forecast: {e}")
            return []

    def parse_hourly_forecast(self, raw_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse hourly forecast from forecast JSON.

        Each item has ForecastHour (YYYYMMDDHH), ForecastTemperature,
        ForecastWindSpeed, ForecastWindDirection, ForecastRelativeHumidity.
        """
        try:
            forecasts = []
            for item in raw_data.get("HourlyWeatherForecast", []):
                if not isinstance(item, dict):
                    continue

                temp_val = item.get("ForecastTemperature")
                if temp_val is None:
                    continue
                try:
                    temperature = float(temp_val)
                except (ValueError, TypeError):
                    continue

                # Parse ForecastHour YYYYMMDDHH
                hour_str = str(item.get("ForecastHour", ""))
                forecast_time = None
                if len(hour_str) >= 10:
                    try:
                        forecast_time = datetime.strptime(hour_str[:10], "%Y%m%d%H")
                    except ValueError:
                        pass

                forecasts.append({
                    "station_code": raw_data.get("StationCode", "").lower(),
                    "station_name": STATION_CONFIG.get(raw_data.get("StationCode", "").lower(), raw_data.get("StationCode", "")),
                    "forecast_time": forecast_time,
                    "temperature": temperature,
                    "wind_speed": item.get("ForecastWindSpeed"),
                    "wind_direction": item.get("ForecastWindDirection"),
                    "humidity": item.get("ForecastRelativeHumidity"),
                })

            logger.info(f"Parsed {len(forecasts)} hourly forecasts for {raw_data.get('StationCode')}")
            return forecasts

        except Exception as e:
            logger.exception(f"Failed to parse hourly forecast: {e}")
            return []
