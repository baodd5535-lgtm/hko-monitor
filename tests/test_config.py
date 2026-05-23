import os
import importlib
from hko_weather_monitor import config


def test_config_loads_defaults(monkeypatch):
    """Test that config falls back to secure defaults when no .env is present."""
    # Strip all relevant env vars
    for key in ["HKO_API_URL", "REQUEST_TIMEOUT", "MAX_RETRIES",
                 "RETRY_BACKOFF_FACTOR", "POLL_INTERVAL_SECONDS", "DATABASE_PATH",
                 "LOG_LEVEL", "LOG_FILE", "STATION_WHITELIST", "AWS_DATAFILE",
                 "HISTORICAL_TEMP_FILE", "FORECAST_URL"]:
        monkeypatch.delenv(key, raising=False)

    # Reload module to pick up clean env
    importlib.reload(config)
    c = config.Config

    assert c.HKO_API_URL == "https://www.hko.gov.hk/wxinfo/awsgis"
    assert c.REQUEST_TIMEOUT == 15
    assert c.MAX_RETRIES == 3
    assert c.RETRY_BACKOFF_FACTOR == 1.0
    assert c.POLL_INTERVAL_SECONDS == 300
    assert c.DATABASE_PATH == os.path.join(os.path.dirname(config.__file__), "data", "hko_weather.db")
    assert c.LOG_LEVEL == "INFO"
    assert c.LOG_FILE == "hko_monitor.log"
    assert c.STATION_WHITELIST == []


def test_config_type_coercion(monkeypatch):
    """Test that string env vars are correctly cast to int/float."""
    monkeypatch.setenv("REQUEST_TIMEOUT", "30")
    monkeypatch.setenv("MAX_RETRIES", "5")
    monkeypatch.setenv("RETRY_BACKOFF_FACTOR", "2.5")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "60")

    importlib.reload(config)
    c = config.Config

    assert c.REQUEST_TIMEOUT == 30
    assert isinstance(c.REQUEST_TIMEOUT, int)
    assert c.MAX_RETRIES == 5
    assert isinstance(c.MAX_RETRIES, int)
    assert c.RETRY_BACKOFF_FACTOR == 2.5
    assert isinstance(c.RETRY_BACKOFF_FACTOR, float)
    assert c.POLL_INTERVAL_SECONDS == 60
    assert isinstance(c.POLL_INTERVAL_SECONDS, int)


def test_config_station_whitelist(monkeypatch):
    """Test station whitelist parsing from comma-separated string."""
    monkeypatch.setenv("STATION_WHITELIST", "A1, B2 , C3")

    importlib.reload(config)
    c = config.Config

    assert c.STATION_WHITELIST == ["A1", "B2", "C3"]


def test_config_empty_station_whitelist(monkeypatch):
    """Test empty whitelist results in empty list."""
    monkeypatch.setenv("STATION_WHITELIST", "")

    importlib.reload(config)
    c = config.Config

    assert c.STATION_WHITELIST == []
