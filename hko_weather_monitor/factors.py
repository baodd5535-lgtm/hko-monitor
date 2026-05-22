"""Multi-factor weather scoring for adjusting HKO temperature forecasts."""
from typing import Dict, Any, Optional
from hko_weather_monitor import uv_fetcher


class WeatherMultiFactorScorer:
    def __init__(self):
        self._last_uv_data = None
        self._last_uv_fetch = None
    
    def _uv_spline_peak(self, current_peak: Optional[float]) -> Dict[str, Any]:
        """Predict UV peak using spline extrapolation.
        
        Uses 15-min UV data to predict the day's maximum UV via cubic spline.
        Falls back to current peak if insufficient data or scipy unavailable.
        """
        import numpy as np
        uv_data = uv_fetcher.fetch_uv_data('uv15min')
        if not uv_data or len(uv_data['data']) < 5:
            return {
                'uv_adjustment': uv_fetcher.get_uv_forecast_adjustment(current_peak),
                'peak_uv': current_peak,
                'uv_level': self._get_uv_level(current_peak),
                'method': 'fallback',
            }
        
        # Extract UV series
        uv_series = [entry['uv_index'] for entry in uv_data['data'] if entry['uv_index'] is not None]
        times = list(range(len(uv_series)))
        
        if len(times) < 5:
            return {
                'uv_adjustment': uv_fetcher.get_uv_forecast_adjustment(current_peak),
                'peak_uv': current_peak,
                'uv_level': self._get_uv_level(current_peak),
                'method': 'fallback',
            }
        
        try:
            # Try scipy spline first
            from scipy.interpolate import CubicSpline
            cs = CubicSpline(times, uv_series, bc_type='natural')
            # Extrapolate peak (typically occurs 12:00-14:00)
            future_times = np.linspace(times[-1], times[-1] + 8, 32)
            peak_spline = float(max(cs(future_times)))
            # Also check existing data
            peak_existing = max(uv_series) if uv_series else 0.0
            peak_uv = max(peak_spline, peak_existing)
            return {
                'uv_adjustment': uv_fetcher.get_uv_forecast_adjustment(peak_uv),
                'peak_uv': peak_uv,
                'uv_level': self._get_uv_level(peak_uv),
                'method': 'scipy_spline',
            }
        except (ImportError, ValueError) as e:
            # Fallback to max aggregation
            return {
                'uv_adjustment': uv_fetcher.get_uv_forecast_adjustment(current_peak),
                'peak_uv': current_peak,
                'uv_level': self._get_uv_level(current_peak),
                'method': 'fallback',
            }
    
    def _get_uv_level(self, peak_uv: Optional[float]) -> str:
        """Determine UV level category from peak UV index."""
        if peak_uv is None or peak_uv <= 2:
            return 'low'
        elif peak_uv <= 5:
            return 'moderate'
        elif peak_uv <= 7:
            return 'high'
        elif peak_uv <= 10:
            return 'very_high'
        else:
            return 'extreme'
    
    def get_uv_adjustment(self, date_iso: Optional[str] = None) -> Dict[str, Any]:
        """Get UV-based temperature adjustment.
        
        Returns dict with adjustment value and metadata.
        """
        # Fetch UV data (cached for 1 hour)
        from datetime import datetime, timedelta
        now = datetime.now()
        
        if not self._last_uv_data or (now - self._last_uv_fetch) > timedelta(hours=1):
            self._last_uv_data = uv_fetcher.fetch_uv_data('uv15min_daws')
            self._last_uv_fetch = now
        
        if not self._last_uv_data:
            return {'uv_adjustment': 0.0, 'peak_uv': None, 'uv_level': 'unknown'}
        
        peak_uv = uv_fetcher.get_peak_uv_index(self._last_uv_data['date'])
        uv_adjustment = uv_fetcher.get_uv_forecast_adjustment(peak_uv)
        
        return {
            'uv_adjustment': uv_adjustment,
            'peak_uv': peak_uv,
            'uv_level': self._get_uv_level(peak_uv),
        }
    
    def calculate_adjusted_temperature_probability(self, base_hko_temp: float, details: Dict[str, Any]) -> float:
        """
        Adjusts raw forecast metrics into an implied maximum temperature distribution offset.
        Expected details format:
        {
            "humidity": float,       # RH percentage (e.g. 85.0)
            "cloud_coverage": float, # Percentage (e.g. 90.0)
            "wind_speed": float,     # km/h
            "wind_direction": str    # "E", "SW", "N"
            "uv_index": float,       # Optional peak UV index
        }
        """
        adjustment = 0.0
        factor_breakdown = {}
        
        # Cloud cover dampens solar irradiance, suppressing daytime high peaks
        cloud_coverage = details.get("cloud_coverage", 0.0)
        if cloud_coverage > 75.0:
            adjustment -= 0.8
            factor_breakdown['cloud'] = -0.8
        elif cloud_coverage < 20.0:
            adjustment += 0.4
            factor_breakdown['cloud'] = 0.4
        else:
            factor_breakdown['cloud'] = 0.0

        # Maritime easterlies act as heat sink; Northerlies/Westerlies amplify land mass heat trap
        wind_dir = details.get("wind_direction", "E")
        wind_speed = details.get("wind_speed", 0.0)
        
        if wind_dir in ["E", "SE"] and wind_speed > 15.0:
            adjustment -= 0.5
            factor_breakdown['wind'] = -0.5
        elif wind_dir in ["N", "NW"] and wind_speed < 10.0:
            adjustment += 0.6
            factor_breakdown['wind'] = 0.6
        else:
            factor_breakdown['wind'] = 0.0

        # High humidity bounds energy delta via high latent heat requirements
        humidity = details.get("humidity", 0.0)
        if humidity > 85.0:
            adjustment -= 0.3
            factor_breakdown['humidity'] = -0.3
        else:
            factor_breakdown['humidity'] = 0.0
        
        # UV index adjustment with spline peak prediction
        uv_data = self.get_uv_adjustment()
        
        # Try spline prediction for better UV estimate
        uv_data_with_spline = self._uv_spline_peak(uv_data['peak_uv'])
        uv_adjustment = uv_data_with_spline['uv_adjustment']
        uv_data = uv_data_with_spline
        adjustment += uv_adjustment
        factor_breakdown['uv'] = uv_adjustment
        factor_breakdown['peak_uv'] = uv_data['peak_uv']
        factor_breakdown['uv_level'] = uv_data['uv_level']

        self._last_adjustment = {
            'base_temp': base_hko_temp,
            'factors': factor_breakdown,
            'total_adjustment': adjustment,
            'adjusted_temp': base_hko_temp + adjustment,
        }
        
        return base_hko_temp + adjustment
    
    def get_last_adjustment(self) -> Optional[Dict]:
        """Get the breakdown of the last temperature adjustment calculation."""
        return self._last_adjustment if hasattr(self, '_last_adjustment') else None
