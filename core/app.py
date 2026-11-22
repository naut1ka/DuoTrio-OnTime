import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
import re

from prediction import predict_slots

from flask import Flask, abort, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32768
DB = Path(os.environ.get('DB_PATH', 'ontime.sqlite3'))
SECRET = os.environ['JWT_SECRET'].encode()
if len(SECRET) < 32:
    raise RuntimeError('JWT_SECRET requires at least 32 bytes')
PARTNERS = {k: v for k, v in json.loads(os.getenv('PARTNER_KEYS') or '{}').items() if isinstance(v, str) and len(v) >= 32}
WIB = timezone(timedelta(hours=7))
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,email TEXT UNIQUE,password TEXT);
CREATE TABLE IF NOT EXISTS favorite_places(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner INTEGER NOT NULL,
    name TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS favorite_places_owner ON favorite_places(owner,id);
CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY,owner INTEGER,data TEXT);
CREATE TABLE IF NOT EXISTS trips(id INTEGER PRIMARY KEY,owner INTEGER,route TEXT,started INTEGER,ended INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS active ON trips(owner) WHERE ended IS NULL;
CREATE INDEX IF NOT EXISTS history ON trips(route,started);
CREATE TABLE IF NOT EXISTS subscriptions(owner INTEGER PRIMARY KEY,token TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS reminders(plan INTEGER,target TEXT,departure TEXT,PRIMARY KEY(plan,target));
CREATE TABLE IF NOT EXISTS conditions(id INTEGER PRIMARY KEY,lat REAL,lon REAL,kind TEXT,expires INTEGER);
"""
with sqlite3.connect(DB) as conn:
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
conn.close()


def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB, timeout=5)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error):
    if 'db' in g:
        g.db.close()


@app.errorhandler(HTTPException)
def error(e):
    return jsonify(error=e.description), e.code


def body():
    data = request.get_json()
    if not isinstance(data, dict):
        abort(400, 'Expected JSON object')
    return data


def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def sign(payload):
    return encode(hmac.digest(SECRET, payload.encode(), 'sha256'))


def access(partner=False):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            key = request.headers.get('X-API-Key', '')
            g.user = None
            if partner and key and any(secrets.compare_digest(key.encode(), v.encode()) for v in PARTNERS.values()):
                return fn(*args, **kwargs)
            token = request.headers.get('Authorization', '').split()
            try:
                scheme, jwt = token
                header, claims, signature = jwt.split('.')
                if scheme.lower() != 'bearer' or not hmac.compare_digest(sign(header + '.' + claims), signature):
                    raise ValueError
                data = json.loads(base64.urlsafe_b64decode(claims + '==='))
                if data['exp'] <= time.time():
                    raise ValueError
                g.user = int(data['sub'])
            except (ValueError, KeyError, TypeError):
                abort(401, 'Valid credentials required')
            return fn(*args, **kwargs)
        return wrapped
    return decorator


@app.get('/api')
def index():
    return {'service': 'OnTime', 'health': '/api/health', 'contract': '/api/openapi.json'}


@app.get('/api/health')
def health():
    db().execute('SELECT 1')
    return {'status': 'ok', 'service': 'ontime'}


@app.get('/api/openapi.json')
def contract():
    return send_file(Path(__file__).with_name('openapi.json'), mimetype='application/json')


@app.post('/api/auth/<action>')
def auth(action):
    d = body()
    email, password = d.get('email'), d.get('password')
    if not isinstance(email, str) or '@' not in email or not isinstance(password, str) or len(password) > 256:
        abort(400, 'Invalid email or password')
    email = email.strip().lower()
    if action == 'register':
        if len(password) < 8:
            abort(400, 'Minimum password length is 8')
        try:
            with db() as c:
                uid = c.execute('INSERT INTO users(email,password) VALUES(?,?)', (email, generate_password_hash(password))).lastrowid
        except sqlite3.IntegrityError:
            abort(409, 'Email already registered')
        return {'id': uid}, 201
    if action != 'login':
        abort(404)
    user = db().execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    if not user or not check_password_hash(user['password'], password):
        abort(401, 'Invalid credentials')
    payload = encode(b'{"alg":"HS256","typ":"JWT"}') + '.' + encode(json.dumps({'sub': str(user['id']), 'exp': int(time.time()) + 86400}).encode())
    return {'token': payload + '.' + sign(payload), 'expiresIn': 86400}


def point(value):
    if not isinstance(value, list) or len(value) != 2:
        abort(400, 'Coordinates must be [latitude, longitude]')
    for n, limit in zip(value, (90, 180)):
        if type(n) not in (int, float) or not -limit <= n <= limit:
            abort(400, 'Invalid coordinates')
    return value


def favorite_input():
    data = body()
    name = data.get('name')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        abort(400, 'Name must contain 1 to 80 characters')
    return name.strip(), point(data.get('location'))


@app.route('/api/favorites', methods=['GET', 'POST'])
@access()
def favorites():
    if request.method == 'GET':
        rows = db().execute(
            'SELECT id,name,latitude,longitude FROM favorite_places WHERE owner=? ORDER BY id',
            (g.user,),
        )
        return {'items': [
            {'id': row['id'], 'name': row['name'], 'location': [row['latitude'], row['longitude']]}
            for row in rows
        ]}

    name, location = favorite_input()
    with db() as conn:
        result = conn.execute(
            'INSERT INTO favorite_places(owner,name,latitude,longitude) VALUES(?,?,?,?)',
            (g.user, name, *location),
        )
    return {'id': result.lastrowid, 'name': name, 'location': location}, 201


@app.route('/api/favorites/<int:place_id>', methods=['PUT', 'DELETE'])
@access()
def edit_favorite(place_id):
    with db() as conn:
        if request.method == 'DELETE':
            result = conn.execute(
                'DELETE FROM favorite_places WHERE id=? AND owner=?', (place_id, g.user)
            )
        else:
            name, location = favorite_input()
            result = conn.execute(
                'UPDATE favorite_places SET name=?,latitude=?,longitude=? WHERE id=? AND owner=?',
                (name, *location, place_id, g.user),
            )
        if not result.rowcount:
            abort(404)
    return '', 204


def route(d):
    a, b = point(d.get('origin')), point(d.get('destination'))
    vehicle = d.get('vehicle')
    if vehicle not in ('car', 'motorcycle'):
        abort(400, 'vehicle must be car or motorcycle')
    return a, b, vehicle


def route_key(d):
    a, b, v = route(d)
    return json.dumps([round(x, 2) for x in a + b] + [v])


def timestamp(s):
    try:
        value = datetime.fromisoformat(s)
        if value.tzinfo is None:
            raise ValueError
        return value.astimezone(WIB)
    except (TypeError, ValueError, OverflowError):
        abort(400, 'Expected ISO timestamp with UTC offset')


def plan_data():
    d = body()
    route(d)
    if d.get('kind') == 'once':
        timestamp(d.get('targetArrival'))
    elif d.get('kind') == 'routine':
        days = d.get('weekdays')
        if not isinstance(days, list) or not days or any(type(x) != int or not 1 <= x <= 7 for x in days):
            abort(400, 'weekdays must contain ISO weekday numbers 1-7')
        try:
            datetime.strptime(d['arrivalTime'], '%H:%M')
        except (ValueError, KeyError, TypeError):
            abort(400, 'arrivalTime must be HH:MM WIB')
    else:
        abort(400, 'kind must be once or routine')
    d.pop('id', None)
    return d


@app.route('/api/plans', methods=['GET', 'POST'])
@access()
def plans():
    if request.method == 'GET':
        return {'items': [dict(json.loads(r['data']), id=r['id']) for r in db().execute('SELECT * FROM plans WHERE owner=?', (g.user,))]}
    d = plan_data()
    with db() as c:
        uid = c.execute('INSERT INTO plans(owner,data) VALUES(?,?)', (g.user, json.dumps(d))).lastrowid
    return dict(d, id=uid), 201


@app.route('/api/plans/<int:uid>', methods=['PUT', 'DELETE'])
@access()
def edit_plan(uid):
    with db() as c:
        args = (uid, g.user)
        if request.method == 'DELETE':
            result = c.execute('DELETE FROM plans WHERE id=? AND owner=?', args)
        else:
            result = c.execute('UPDATE plans SET data=? WHERE id=? AND owner=?', (json.dumps(plan_data()), *args))
        if not result.rowcount:
            abort(404)
    return '', 204


@app.post('/api/trips/start')
@access()
def start():
    key = route_key(body())
    with db() as c:
        result = c.execute(
            'INSERT INTO trips(owner,route,started) VALUES(?,?,?) '
            'ON CONFLICT(owner) WHERE ended IS NULL DO NOTHING',
            (g.user, key, int(time.time())),
        )
        if not result.rowcount:
            active = c.execute(
                'SELECT id FROM trips WHERE owner=? AND ended IS NULL', (g.user,)
            ).fetchone()
            return {'error': 'A trip is active', 'id': active['id']}, 409
        return {'id': result.lastrowid}, 201


@app.post('/api/trips/<int:uid>/finish')
@access()
def finish(uid):
    with db() as c:
        c.execute('UPDATE trips SET ended=unixepoch() WHERE id=? AND owner=? AND ended IS NULL', (uid, g.user))
        row = c.execute('SELECT ended-started FROM trips WHERE id=? AND owner=?', (uid, g.user)).fetchone()
        if not row:
            abort(404)
    return {'durationSeconds': row[0]}


def nearby(p):
    return db().execute('SELECT * FROM conditions WHERE expires>unixepoch() AND abs(lat-?)<=0.01 AND abs(lon-?)<=0.01', p).fetchall()


@app.get('/api/conditions')
@access(partner=True)
def conditions():
    p = point([request.args.get('lat', type=float), request.args.get('lon', type=float)])
    return {'items': [dict(r) for r in nearby(p)]}


@app.post('/api/conditions')
@access()
def report():
    d = body()
    lat, lon = point(d.get('location'))
    if d.get('kind') not in ('traffic', 'flood'):
        abort(400, 'Unsupported condition')
    with db() as c:
        uid = c.execute('INSERT INTO conditions(lat,lon,kind,expires) VALUES(?,?,?,?)', (lat, lon, d['kind'], int(time.time()) + 7200)).lastrowid
    return {'id': uid}, 201


@app.post('/api/predict')
@access(partner=True)
def predict():
    d = body()
    target = timestamp(d.get('targetArrival'))
    now = datetime.now(WIB)
    if target <= now or target > now + timedelta(days=7):
        abort(400, 'Target must be within the next seven days')
    return prediction_for(d, target, now, g.user)


def prediction_for(d, target, now, owner):
    a, b, vehicle = route(d)
    history = db().execute('SELECT * FROM trips WHERE route=? AND ended>started ORDER BY started DESC LIMIT 1000', (route_key(d),)).fetchall()
    road = max((r['expires'] for r in nearby(a) + nearby(b)), default=0)
    return predict_slots(a, b, vehicle, target, now, history, owner, road)

@app.route('/api/reminders/device', methods=['PUT', 'DELETE'])
@access()
def device():
    with db() as c:
        if request.method == 'DELETE':
            c.execute('DELETE FROM subscriptions WHERE owner=?', (g.user,))
        else:
            token = body().get('token')
            if not isinstance(token, str) or not re.fullmatch(r'(Expo|Exponent)PushToken\[[\w-]{8,200}\]', token):
                abort(400, 'Expected Expo push token')
            try:
                c.execute('INSERT INTO subscriptions VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET token=excluded.token', (g.user, token))
            except sqlite3.IntegrityError:
                abort(409, 'Device already registered')
    return '', 204

