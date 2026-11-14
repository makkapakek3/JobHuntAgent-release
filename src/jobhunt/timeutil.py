from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_zone(name='Asia/Shanghai'):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == 'Asia/Shanghai':
            return timezone(timedelta(hours=8), name)
        if name == 'UTC':
            return timezone.utc
        raise ValueError('TIMEZONE_UNAVAILABLE: install tzdata or use Asia/Shanghai') from None


def period_start(period, zone):
    current = datetime.now(local_zone(zone))
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'monthly':
        start = start.replace(day=1)
    return start.astimezone(timezone.utc).isoformat(timespec='seconds')
