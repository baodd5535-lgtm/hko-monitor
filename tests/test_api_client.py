import pytest
from datetime import datetime
from hko_weather_monitor.api_client import HKOApiClient


def test_parse_temperatures_correct_structure():
    client = HKOApiClient("", "", "", 10, 1, 1.0)
    sample = """Latest readings recorded at 00:50 Hong Kong Time 20 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,26.1,89,26.1,26.0,,,,1009.0,0.7,24.5,
HKA,116,17,23,26.9,82,27.1,26.9,,,44040,1008.5,0.5,25.3,
SHA,212,3,6,26.1,89,26.1,26.0,,,,1009.2,0.9,24.6,"""

    readings = client.parse_temperatures(sample)
    assert len(readings) == 3
    assert readings[0]["station_code"] == "hko"
    assert readings[0]["station_name"] == "HK Observatory"
    assert readings[0]["temperature"] == 26.1
    assert readings[0]["recorded_at"] == datetime(2026, 5, 20, 0, 50)


def test_parse_temperatures_missing_temperature():
    client = HKOApiClient("", "", "", 10, 1, 1.0)
    sample = """Latest readings recorded at 00:50 Hong Kong Time 20 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,M,89,26.1,26.0,,,,1009.0,0.7,24.5,"""

    readings = client.parse_temperatures(sample)
    assert len(readings) == 0


def test_parse_past_temperatures():
    client = HKOApiClient("", "", "", 10, 1, 1.0)
    sample = """202605190100,25.2,hko
202605190200,25.0,hko
202605190300,25.2,hko
202605190100,24.6,kp
202605190200,24.2,kp"""

    readings = client.parse_past_temperatures(sample)
    assert len(readings) == 5
    assert readings[0]["station_code"] == "hko"
    assert readings[0]["temperature"] == 25.2
    assert readings[0]["recorded_at"] == datetime(2026, 5, 19, 1, 0)


def test_station_whitelist_filter():
    client = HKOApiClient("", "", "", 10, 1, 1.0)
    sample = """Latest readings recorded at 00:50 Hong Kong Time 20 May 2026
STN,WINDDIRECTION,WINDSPEED,GUST,TEMP,RH,MAXTEMP,MINTEMP,GRASSTEMP,GRASSMINTEMP,VISIBILITY,PRESSURE,TEMPDIFFERENCE,HEATINDEX,
HKO,,,,26.1,89,26.1,26.0,,,,1009.0,0.7,24.5,
HKA,116,17,23,26.9,82,27.1,26.9,,,44040,1008.5,0.5,25.3,
SHA,212,3,6,26.1,89,26.1,26.0,,,,1009.2,0.9,24.6,"""

    readings = client.parse_temperatures(sample, station_whitelist=["HK Observatory"])
    assert len(readings) == 1
    assert readings[0]["station_name"] == "HK Observatory"
