import logging
import time
import signal
import sys
from datetime import datetime
from typing import Optional

from .config import Config
from .database import WeatherDatabase
from .api_client import HKOApiClient

logger = logging.getLogger(__name__)


class WeatherPoller:
    def __init__(self, config: Config):
        self.config = config
        self.db = WeatherDatabase(config.DATABASE_PATH)
        self.api_client = HKOApiClient(
            base_url=config.HKO_API_URL,
            aws_datafile=config.AWS_DATAFILE,
            historical_temp_file=config.HISTORICAL_TEMP_FILE,
            timeout=config.REQUEST_TIMEOUT,
            max_retries=config.MAX_RETRIES,
            backoff_factor=config.RETRY_BACKOFF_FACTOR
        )
        self.running = True

    def poll_once(self) -> int:
        """Perform one poll cycle. Returns number of readings stored."""
        total = 0

        # Fetch and store current readings
        logger.info("Polling current temperatures...")
        raw_data = self.api_client.fetch_temperatures()
        if raw_data is not None:
            readings = self.api_client.parse_temperatures(raw_data, self.config.STATION_WHITELIST)
            if readings:
                self.db.insert_batch(readings)
                total += len(readings)

        # Fetch and store historical data
        logger.info("Polling historical temperatures...")
        past_data = self.api_client.fetch_past_temperatures()
        if past_data is not None:
            past_readings = self.api_client.parse_past_temperatures(past_data, self.config.STATION_WHITELIST)
            if past_readings:
                self.db.insert_batch(past_readings)
                total += len(past_readings)

        # Fetch and store forecasts for key stations
        logger.info("Polling forecasts...")
        forecast_stations = ["HKO", "KP", "VP1", "TMS", "SHA"]
        forecast_count = 0
        for station in forecast_stations:
            try:
                forecast_data = self.api_client.fetch_forecast(station, self.config.FORECAST_URL)
                if forecast_data is None:
                    continue

                daily = self.api_client.parse_daily_forecast(forecast_data)
                if daily:
                    self.db.insert_daily_forecasts_batch(daily)
                    forecast_count += len(daily)

                hourly = self.api_client.parse_hourly_forecast(forecast_data)
                if hourly:
                    self.db.insert_hourly_forecasts_batch(hourly)
                    forecast_count += len(hourly)
            except Exception as e:
                logger.error(f"Forecast error for {station}: {e}")

        total += forecast_count
        return total

    def run(self):
        """Main polling loop. Handles SIGINT/SIGTERM."""
        logger.info(f"Starting weather poller, interval={self.config.POLL_INTERVAL_SECONDS}s")
        while self.running:
            try:
                count = self.poll_once()
                logger.info(f"Poll complete. Stored {count} readings.")
                # Sleep, but break early if interrupted
                for _ in range(self.config.POLL_INTERVAL_SECONDS):
                    if not self.running:
                        break
                    time.sleep(1)
            except Exception as e:
                logger.exception(f"Unexpected error in poll cycle: {e}")
                time.sleep(10)

        logger.info("Poller stopped.")

    def stop(self):
        self.running = False
