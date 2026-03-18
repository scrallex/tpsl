from datetime import datetime, timezone


def parse_utc_time(dt_str: str | datetime) -> datetime:
    """Parse an ISO timestamp or ensure a datetime is UTC."""
    if isinstance(dt_str, datetime):
        if dt_str.tzinfo is None:
            return dt_str.replace(tzinfo=timezone.utc)
        return dt_str.astimezone(timezone.utc)
    if "Z" in dt_str:
        dt_str = dt_str.replace("Z", "+00:00")
    try:
        from dateutil import parser
    except ImportError:
        parser = None
    if parser:
        dt = parser.parse(dt_str)
    else:
        dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
