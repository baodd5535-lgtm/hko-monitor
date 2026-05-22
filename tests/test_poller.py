import pytest
from datetime import datetime
from unittest.mock import Mock, patch
from hko_weather_monitor.config import Config
from hko_weather_monitor.poller import WeatherPoller
from hko_weather_monitor.api_client import HKOApiClient
from hko_weather_monitor.database import WeatherDatabase


@pytest.fixture
def mock_config(tmp_path):
    """Create a test config with temporary database."""
    return Config.__new__(Config)


def test_poll_once_success(mock_config, tmp_path):
    """Successful poll cycle stores readings."""
    mock_config.DATABASE_PATH = str(tmp_path / "test.db")
    mock_config.STATION_WHITELIST = []
    mock_config.HKO_API_URL = "https://test.hko.gov.hk"
    mock_config.AWS_DATAFILE = "latestReadings_AWS1_v2.txt"
    mock_config.HISTORICAL_TEMP_FILE = "animate_J1.csv"
    mock_config.REQUEST_TIMEOUT = 10
    mock_config.MAX_RETRIES = 3
    mock_config.RETRY_BACKOFF_FACTOR = 1.0
    
    poller = WeatherPoller(mock_config)
    
    # Mock the API client methods
    sample_current = """Latest readings recorded at 12:30 Hong Kong Time 20 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,26.1,89,26.1,26.0,,,,1009.0,0.7,24.5,
SHA,212,3,6,26.1,89,26.1,26.0,,,,1009.2,0.9,24.6,"""
    
    sample_past = """202605201100,25.5,hko
202605201200,26.0,hko"""
    
    with patch.object(poller.api_client, 'fetch_temperatures', return_value=sample_current), \
         patch.object(poller.api_client, 'fetch_past_temperatures', return_value=sample_past):
        
        total = poller.poll_once()
        
        # Should have processed both current and historical readings
        assert total >= 2  # At least the historical readings


def test_poll_once_api_failure(mock_config, tmp_path):
    """Poll cycle handles API failures gracefully."""
    mock_config.DATABASE_PATH = str(tmp_path / "test.db")
    mock_config.STATION_WHITELIST = []
    mock_config.HKO_API_URL = "https://test.hko.gov.hk"
    mock_config.AWS_DATAFILE = "latestReadings_AWS1_v2.txt"
    mock_config.HISTORICAL_TEMP_FILE = "animate_J1.csv"
    mock_config.REQUEST_TIMEOUT = 10
    mock_config.MAX_RETRIES = 3
    mock_config.RETRY_BACKOFF_FACTOR = 1.0
    
    poller = WeatherPoller(mock_config)
    
    # Mock all API failures
    with patch.object(poller.api_client, 'fetch_temperatures', return_value=None), \
         patch.object(poller.api_client, 'fetch_past_temperatures', return_value=None), \
         patch.object(poller.api_client, 'fetch_forecast', return_value=None):
        
        total = poller.poll_once()
        
        # Should return 0 when all APIs fail
        assert total == 0


def test_poller_stop(mock_config, tmp_path):
    """Poller stops correctly."""
    mock_config.DATABASE_PATH = str(tmp_path / "test.db")
    mock_config.STATION_WHITELIST = []
    mock_config.HKO_API_URL = "https://test.hko.gov.hk"
    mock_config.AWS_DATAFILE = "latestReadings_AWS1_v2.txt"
    mock_config.HISTORICAL_TEMP_FILE = "animate_J1.csv"
    mock_config.REQUEST_TIMEOUT = 10
    mock_config.MAX_RETRIES = 3
    mock_config.RETRY_BACKOFF_FACTOR = 1.0
    mock_config.POLL_INTERVAL_SECONDS = 1  # Short interval for testing
    
    poller = WeatherPoller(mock_config)
    assert poller.running is True
    
    poller.stop()
    assert poller.running is False
