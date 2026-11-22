"""Run one reminder scheduler alongside the single Gunicorn worker."""
import json
import logging
import time
from datetime import datetime, timedelta
from threading import Thread
from urllib.request import Request, urlopen

from werkzeug.exceptions import HTTPException


def check(app_module):
    m = app_module
    now = datetime.now(m.WIB)
    rows = m.db().execute('SELECT p.id,p.owner,p.data,s.token FROM plans p JOIN subscriptions s ON p.owner=s.owner').fetchall()
    for row in rows:
        plan = json.loads(row['data'])
        if plan['kind'] != 'routine':
            continue
        for day in (now, now + timedelta(days=1)):
            hour, minute = map(int, plan['arrivalTime'].split(':'))
            target = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target.isoweekday() not in plan['weekdays'] or not now < target <= now + timedelta(hours=3):
                continue
            try:
                result = m.prediction_for(plan, target, now, row['owner'])
            except HTTPException:
                continue
            latest = result['latestSafe']
            if not latest:
                continue
            suggested = m.timestamp(latest['departure'])
            if suggested > now + timedelta(minutes=20):
                continue
            previous = m.db().execute('SELECT departure FROM reminders WHERE plan=? AND target=?',
                                      (row['id'], target.isoformat())).fetchone()
            if previous and previous[0] == latest['departure']:
                continue
            msg = {'to': row['token'], 'title': 'Pengingat OnTime',
                   'body': 'Berangkat paling lambat pukul ' + suggested.strftime('%H:%M WIB')}
            req = Request('https://exp.host/--/api/v2/push/send', data=json.dumps(msg).encode(),
                          headers={'Content-Type': 'application/json'})
            try:
                with urlopen(req, timeout=4) as response:
                    ticket = json.load(response).get('data', {})
            except (OSError, ValueError):
                logging.warning('Expo request failed for plan %s', row['id'])
                continue
            if isinstance(ticket, list):
                ticket = ticket[0] if ticket else {}
            if ticket.get('status') == 'ok':
                with m.db() as c:
                    c.execute('INSERT OR REPLACE INTO reminders VALUES(?,?,?)',
                              (row['id'], target.isoformat(), latest['departure']))


def run():
    import app as module
    while True:
        try:
            with module.app.app_context():
                check(module)
        except Exception:
            logging.exception('Reminder check failed')
        time.sleep(60)


def start_scheduler():
    Thread(target=run, name='ontime-reminders', daemon=True).start()

