import http.cookiejar
import importlib.util
import itertools
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

spec=importlib.util.spec_from_file_location('festival',Path(__file__).resolve().parents[1]/'server.py')
app=importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def profile(i, group='남성'):
    rng=random.Random(i)
    return dict(name='참가자'+str(i),instagram='person_'+str(i),
        own={k:rng.choice(v) for k,v in app.CATEGORIES.items()},
        ideal={k:[rng.choice(v)] for k,v in app.CATEGORIES.items()},
        ranks=rng.sample(list(app.CATEGORIES),2),consent=True,ticket=str(i),group=group)


class MatchingTests(unittest.TestCase):
    def test_optimal_against_exhaustive(self):
        for seed in range(12):
            ps=[profile(seed*10+i,'남성' if i<3 else '여성') for i in range(7)]
            a,b=ps[:3],ps[3:]
            def objective(js):
                pairs=[(app.directed(x,b[j]),app.directed(b[j],x)) for x,j in zip(a,js)]
                return min(min(p) for p in pairs),sum((x*y)**.5 for x,y in pairs)
            expected=max(objective(js) for js in itertools.permutations(range(4),3))
            result=app.optimize(ps)
            self.assertEqual(len(result['matches']),3)
            self.assertEqual(len(result['unmatched']),1)
            self.assertAlmostEqual(result['floor'],round(expected[0],1))
            actual=sum((app.directed(m['a'],m['b'])*app.directed(m['b'],m['a']))**.5 for m in result['matches'])
            self.assertAlmostEqual(actual,expected[1])

    def test_indifference_and_weights(self):
        a,b=profile(1),profile(2)
        a['ideal']={k:['상관없음'] for k in app.CATEGORIES}
        self.assertEqual(app.directed(a,b),100)
        a['ideal']={k:[v[0]] for k,v in app.CATEGORIES.items()}
        b['own']={k:v[-1] for k,v in app.CATEGORIES.items()}
        b['own']['look']='강아지상'
        b['own']['date']='실내'
        a['ranks']=['look','date']
        self.assertAlmostEqual(app.directed(a,b),500/7)
        a['ranks']=['social','plan']
        self.assertAlmostEqual(app.directed(a,b),200/7)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        app.DATA=Path(self.temp.name)
        app.SESSIONS.clear()
        app.ATTEMPTS.clear()
        app.init()
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self,path,data=None):
        req=urllib.request.Request(self.base+'/api/'+path,data=None if data is None else json.dumps(data).encode(),headers={} if data is None else {'Content-Type':'application/json'})
        try:
            with self.client.open(req) as r:
                raw=r.read()
                return r.status, raw if path=='backup' else json.loads(raw)
        except urllib.error.HTTPError as e:
            return e.code,json.loads(e.read())

    def login(self):
        self.assertEqual(self.request('login',dict(pin='1010'))[0],200)

    def add(self,i,group):
        self.login()
        ticket=self.request('ticket',dict(group=group))[1]['ticket']
        self.assertEqual(self.request('open',dict(ticket=ticket))[0],200)
        self.assertEqual(self.request('state')[0],401)
        p=profile(i)
        code,body=self.request('submit',dict(ticket=ticket,person=p))
        self.assertEqual(code,200,body)
        self.assertEqual(self.request('submit',dict(ticket=ticket,person=p))[0],200)
        return ticket,p

    def test_paid_registration_matching_backup_restore(self):
        self.assertEqual(self.request('state')[0],401)
        self.assertEqual(self.request('login',dict(pin='0000'))[0],401)
        self.assertEqual(self.request('submit',dict(ticket='fake',person=profile(5)))[0],401)
        self.login()
        payment=dict(group='남성',ticket='P-12345678ABCDEF00')
        self.assertEqual(self.request('ticket',payment)[0],200)
        self.assertEqual(self.request('ticket',payment)[0],200)
        self.assertEqual(len(self.request('state')[1]['tickets']),1)
        self.request('cancel',dict(ticket=payment['ticket']))
        t1,_=self.add(1,'남성')
        t2,_=self.add(2,'여성')
        self.login()
        s=self.request('state')[1]
        self.assertEqual(len(s['people']),2)
        self.assertEqual(len(s['tickets']),3)
        run=self.request('match',{})[1]['run']
        self.assertEqual(len(run['matches']),1)
        self.assertEqual(self.request('delivery',dict(run=run['id'],ticket=t1,state='완료'))[0],200)
        raw=self.request('backup')[1]
        backup=app.DATA/'download.sqlite3'
        backup.write_bytes(raw)
        with sqlite3.connect(backup) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM participants').fetchone()[0],2)
            # Verify the downloaded file can restore a separate database.
            with sqlite3.connect(app.DATA/'restored.sqlite3') as restored:
                c.backup(restored)
                self.assertEqual(restored.execute('SELECT COUNT(*) FROM runs').fetchone()[0],1)
        t3=self.request('ticket',dict(group='남성'))[1]['ticket']
        self.assertEqual(self.request('match',{})[0],400)
        self.request('cancel',dict(ticket=t3))
        self.assertFalse(self.request('state')[1]['stale'])
        self.request('cancel',dict(ticket=t2))
        self.assertTrue(self.request('state')[1]['stale'])
        self.assertEqual(self.request('delivery',dict(run=run['id'],ticket=t1,state='완료'))[0],400)
        # Reinitialization preserves committed data.
        app.init()
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM participants').fetchone()[0],2)

    def test_duplicate_instagram_does_not_consume_ticket(self):
        self.add(1,'남성')
        self.login()
        ticket=self.request('ticket',dict(group='여성'))[1]['ticket']
        self.request('open',dict(ticket=ticket))
        p=profile(1)
        self.assertEqual(self.request('submit',dict(ticket=ticket,person=p))[0],409)
        p['instagram']='new_person'
        self.assertEqual(self.request('submit',dict(ticket=ticket,person=p))[0],200)


if __name__=='__main__':
    unittest.main()
