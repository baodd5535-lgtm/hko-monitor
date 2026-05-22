"""Parse HKO datetime strings: YYYYMMDDHHmm (14 chars)."""
from datetime import datetime

def parse_hko_datetime(dt_str: str) -> datetime:
    """Parse '202605200950' -> datetime."""
    return datetime.strptime(dt_str, "%Y%m%d%H%M")
