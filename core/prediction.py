import json
import math
import time
from datetime import datetime, timedelta
from statistics import median
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from threading import Lock

from flask import abort

_cache = {}
_lock = Lock()


def fetch(url):
    now = time.monotonic()
    with _lock:
        cached = _cache.get(url)
        if cached and cached[0] > now:
            return cached[1]
    try:
        with urlopen(Request(url, headers={'User-Agent': 'OnTime-TST/1.0'}), timeout=3) as response:
            data = json.load(response)
    except (URLError, OSError, ValueError):
        data = {}
    with _lock:
        if len(_cache) >= 128:
            _cache.pop(next(iter(_cache)))
        _cache[url] = (now + (600 if data else 30), data)
    return data


def route_minutes(a, b):
    coords = ';'.join(f'{lon},{lat}' for lat, lon in [a, b])
    routes = fetch('https://router.project-osrm.org/route/v1/driving/' + coords + '?overview=false').get('routes', [])
    return routes[0]['duration'] / 60 if routes else None


def predict_slots(a, b, vehicle, target, now, history, user, road):
    forecast = fetch('https://api.open-meteo.com/v1/forecast?' + urlencode({
        'latitude': a[0], 'longitude': a[1], 'hourly': 'precipitation',
        'timezone': 'Asia/Jakarta', 'forecast_days': 8
    })).get('hourly', {})
    rain = dict(zip(forecast.get('time', []), forecast.get('precipitation', [])))
    slots = []
    base_loaded = False
    base = None
    end = target.replace(minute=target.minute // 15 * 15, second=0, microsecond=0)
    records = [(datetime.fromtimestamp(r['started'], target.tzinfo), r['owner'],
                (r['ended'] - r['started']) / 60) for r in history]
    for offset in range(12, -1, -1):
        depart = end - timedelta(minutes=15 * offset)
        if depart < now:
            continue
        samples = [(owner, minutes) for dt, owner, minutes in records
                   if dt.weekday() == depart.weekday() and dt.hour == depart.hour
                   and dt.minute // 15 == depart.minute // 15]
        personal = [minutes for owner, minutes in samples if owner == user]
        if len(personal) >= 3:
            duration, source = median(personal), 'personal'
        elif len(samples) >= 3:
            duration, source = median(x[1] for x in samples), 'collective'
        else:
            if not base_loaded:
                base = route_minutes(a, b)
                base_loaded = True
            if base is None:
                continue
            peak = depart.weekday() < 5 and depart.hour in (7, 8, 16, 17, 18)
            duration, source = base * (1.3 if peak else 1), 'osrm'
        rainfall = rain.get(depart.strftime('%Y-%m-%dT%H:00'))
        if rainfall and rainfall > 0:
            duration += 5 if vehicle == 'motorcycle' else 3
        # Active reports contribute only while valid at the candidate departure.
        if road and depart.timestamp() < road:
            duration += 5
        minutes = max(1, math.ceil(duration))
        safe = depart + timedelta(minutes=minutes + 5) <= target
        slots.append({'departure': depart.isoformat(), 'durationMinutes': minutes,
                      'source': source, 'confidence': {'personal':'high','collective':'medium','osrm':'low'}[source],
                      'weatherAvailable': rainfall is not None, 'meetsTarget': safe})
    if not slots and end >= now:
        abort(503, 'Insufficient history and routing unavailable')
    feasible = [s for s in slots if s['meetsTarget']]
    return {'targetArrival': target.isoformat(), 'bufferMinutes': 5, 'slots': slots,
            'latestSafe': feasible[-1] if feasible else None,
            'fastest': min(feasible, key=lambda s: s['durationMinutes'], default=None)}

