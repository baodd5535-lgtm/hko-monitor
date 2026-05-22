import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # API settings
    HKO_API_URL = os.getenv("HKO_API_URL", "https://www.hko.gov.hk/wxinfo/awsgis")
    AWS_DATAFILE = os.getenv("AWS_DATAFILE", "latestReadings_AWS1_v2.txt")
    HISTORICAL_TEMP_FILE = os.getenv("HISTORICAL_TEMP_FILE", "animate_J1.csv")
    FORECAST_URL = os.getenv("FORECAST_URL", "https://www.hko.gov.hk/wxinfo/awsgis/forecast/{STATION}.xml")
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
    RETRY_BACKOFF_FACTOR = float(os.getenv("RETRY_BACKOFF_FACTOR", "1.0"))

    # Polling interval (seconds)
    POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 minutes

    # Database path (absolute or relative)
    DATABASE_PATH = os.getenv("DATABASE_PATH", "weather_data.db")

    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = os.getenv("LOG_FILE", "hko_monitor.log")

    # Optional: filter specific stations (comma-separated names, empty = all)
    STATION_WHITELIST = [s.strip() for s in os.getenv("STATION_WHITELIST", "").split(",") if s.strip()]
