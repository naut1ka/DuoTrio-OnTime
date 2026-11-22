import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

temp = tempfile.TemporaryDirectory()
os.environ.update(DB_PATH=temp.name + '/test.db', JWT_SECRET='a'*32, PARTNER_KEYS='{"demo":"'+'b'*32+'"}')
import app as backend


class Flow(unittest.TestCase):
    def test_favorites_are_private_and_plans_keep_their_coordinates(self):
        client = backend.app.test_client()
        auth = {}
        for name in ('owner', 'other'):
            credentials = {'email': f'favorites-{name}@example.org', 'password': 'test-password'}
            self.assertEqual(client.post('/api/auth/register', json=credentials).status_code, 201)
            token = client.post('/api/auth/login', json=credentials).json['token']
            auth[name] = {'Authorization': 'Bearer ' + token}

        path = '/api/favorites'
        owner, other = auth['owner'], auth['other']
        self.assertEqual(client.get(path, headers={'X-API-Key': 'b' * 32}).status_code, 401)
        self.assertEqual(client.post(path, headers=owner, json={
            'name': 'Kos', 'location': [91, 107.62],
        }).status_code, 400)
        created = client.post(path, headers=owner, json={'name': ' Kos ', 'location': [-6.88, 107.62]})
        self.assertEqual(created.status_code, 201)
        favorite = created.json
        self.assertEqual(favorite['name'], 'Kos')
        self.assertEqual(client.get(path, headers=owner).json['items'], [favorite])
        self.assertEqual(client.get(path, headers=other).json['items'], [])

        plan = {
            'kind': 'routine', 'origin': favorite['location'], 'destination': [-6.89, 107.61],
            'vehicle': 'motorcycle', 'weekdays': [1], 'arrivalTime': '07:00',
        }
        self.assertEqual(client.post('/api/plans', headers=owner, json=plan).status_code, 201)
        item = f"{path}/{favorite['id']}"
        update = {'name': 'Kos baru', 'location': [-6.90, 107.63]}
        self.assertEqual(client.put(item, headers=other, json=update).status_code, 404)
        self.assertEqual(client.delete(item, headers=other).status_code, 404)
        self.assertEqual(client.put(item, headers=owner, json=update).status_code, 204)
        self.assertEqual(client.get(path, headers=owner).json['items'][0]['location'], update['location'])
        self.assertEqual(client.delete(item, headers=owner).status_code, 204)
        self.assertEqual(client.get(path, headers=owner).json['items'], [])
        self.assertEqual(client.get('/api/plans', headers=owner).json['items'][0]['origin'], plan['origin'])

    def test_full_flow(self):
        c = backend.app.test_client()
        creds = {'email':'test@example.org','password':'test-password'}
        registered = c.post('/api/auth/register', json=creds)
        self.assertEqual(registered.status_code, 201)
        jwt = c.post('/api/auth/login', json=creds).json['token']
        auth = {'Authorization':'Bearer '+jwt}
        partner = {'X-API-Key':'b'*32}
        route = dict(origin=[-6.89,107.61], destination=[-6.91,107.62], vehicle='car')
        target = (datetime.now(backend.WIB) + timedelta(hours=4)).replace(minute=0,second=0,microsecond=0)
        query = dict(route, targetArrival=target.isoformat())
        plan = c.post('/api/plans', headers=auth,json=dict(query,kind='once')).json
        self.assertIn('id',plan)
        self.assertEqual(c.get('/api/plans',headers=partner).status_code,401)
        trip = c.post('/api/trips/start',headers=auth,json=route).json['id']
        duplicate = c.post('/api/trips/start', headers=auth, json=route)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json['id'], trip)
        finish = '/api/trips/%s/finish' % duplicate.json['id']
        finished = c.post(finish, headers=auth)
        self.assertEqual(finished.status_code, 200)
        self.assertIn('durationSeconds', finished.json)
        self.assertEqual(finished.json, c.post(finish, headers=auth).json)
        self.assertEqual(c.post('/api/conditions',headers=auth,json={'kind':'traffic','location':route['origin']}).status_code,201)
        self.assertEqual(len(c.get('/api/conditions?lat=-6.89&lon=107.61',headers=partner).json['items']),1)
        with patch('prediction.fetch',return_value={}), patch('prediction.route_minutes',return_value=20):
            result=c.post('/api/predict',headers=partner,json=query).json
            self.assertEqual(result['latestSafe']['source'],'osrm')
            self.assertFalse(result['latestSafe']['weatherAvailable'])
        with backend.app.app_context():
            with backend.db() as db:
                for week in (1,2,3):
                    start=int((target-timedelta(weeks=week,minutes=30)).timestamp())
                    db.execute('INSERT INTO trips(owner,route,started,ended) VALUES(?,?,?,?)',
                               (registered.json['id'], backend.route_key(route), start, start + 600))
        with patch('prediction.fetch',return_value={}), patch('prediction.route_minutes',return_value=None):
            for headers,source in [(auth,'personal'),(partner,'collective')]:
                self.assertEqual(c.post('/api/predict',headers=headers,json=query).json['latestSafe']['source'],source)
        with c.get('/api/openapi.json') as contract:
            self.assertEqual(contract.json['info']['title'], 'OnTime API')
        self.assertEqual(c.delete('/api/plans/'+str(plan['id']),headers=auth).status_code,204)


if __name__=='__main__':
    unittest.main()


