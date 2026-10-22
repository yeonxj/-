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
        b['own']['vibe']='에겐형'
        b['own']['affection']='행동으로 챙김'
        self.assertAlmostEqual(app.directed(a,b),500/9)
        a['ranks']=['social','plan']
        self.assertAlmostEqual(app.directed(a,b),200/9)

    def test_new_categories_affect_weighted_score(self):
        a,b=profile(1),profile(2)
        a['ideal']={k:['상관없음'] for k in app.CATEGORIES}
        a['ranks']=['vibe','affection']
        a['ideal']['vibe']=['테토형']
        a['ideal']['affection']=['말로 표현']
        b['own']['vibe']='에겐형'
        b['own']['affection']='행동으로 챙김'
        self.assertAlmostEqual(app.directed(a,b),400/9)
        b['own']['affection']='둘 다'
        self.assertAlmostEqual(app.directed(a,b),600/9)
        b['own']['vibe']='테토형'
        self.assertEqual(app.directed(a,b),100)


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
        ticket='P-'+format(i,'032X')
        p=profile(i)
        body=dict(ticket=ticket,group=group,person=p)
        code,result=self.request('submit',body)
        self.assertEqual(code,200,result)
        self.assertEqual(result['ticket'],ticket)
        self.assertEqual(self.request('submit',body)[0],200)
        return ticket,p

    def test_direct_registration_matching_backup_restore(self):
        self.assertEqual(self.request('state')[0],401)
        self.assertEqual(self.request('backup')[0],401)
        self.assertEqual(self.request('match',{})[0],401)
        self.assertEqual(self.request('cancel',dict(ticket='fake'))[0],401)
        self.assertEqual(self.request('login',dict(pin='0000'))[0],401)
        t1,p1=self.add(1,'남성')
        t2,_=self.add(2,'여성')
        # Open submission does not unlock the admin APIs or expose other responses.
        self.assertEqual(self.request('state')[0],401)
        self.assertNotIn('ticket',self.request('session')[1])
        self.login()
        s=self.request('state')[1]
        self.assertEqual(len(s['people']),2)
        self.assertEqual(len(s['tickets']),2)
        self.assertEqual(self.request('ticket',dict(group='남성'))[0],404)
        self.assertEqual(self.request('open',dict(ticket=t1))[0],404)
        run=self.request('match',{})[1]['run']
        self.assertEqual(len(run['matches']),1)
        self.assertEqual(run['score_version'],app.SCORE_VERSION)
        self.assertEqual(self.request('delivery',dict(run=run['id'],ticket=t1,state='완료'))[0],200)
        raw=self.request('backup')[1]
        backup=app.DATA/'download.sqlite3'
        backup.write_bytes(raw)
        with sqlite3.connect(backup) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM participants').fetchone()[0],2)
            with sqlite3.connect(app.DATA/'restored.sqlite3') as restored:
                c.backup(restored)
                self.assertEqual(restored.execute('SELECT COUNT(*) FROM runs').fetchone()[0],1)
                self.assertEqual(restored.execute('SELECT state FROM deliveries').fetchone()[0],'완료')
        self.assertFalse(self.request('state')[1]['stale'])
        self.request('cancel',dict(ticket=t2))
        self.assertTrue(self.request('state')[1]['stale'])
        self.assertEqual(self.request('delivery',dict(run=run['id'],ticket=t1,state='완료'))[0],400)
        self.assertEqual(self.request('submit',dict(ticket=t1,group='여성',person=p1))[0],400)
        app.init()
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM participants').fetchone()[0],2)

    def test_duplicate_instagram_rolls_back_ticket_and_can_be_corrected(self):
        self.add(1,'남성')
        ticket='P-00000000000000000000000000000002'
        p=profile(1)
        body=dict(ticket=ticket,group='여성',person=p)
        self.assertEqual(self.request('submit',body)[0],409)
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM tickets').fetchone()[0],1)
        p['instagram']='new_person'
        self.assertEqual(self.request('submit',body)[0],200)
        p['name']='변경된 응답'
        self.assertEqual(self.request('submit',body)[0],400)

    def test_validation_rejects_missing_new_answers_and_invalid_group(self):
        p=profile(5)
        ticket='P-00000000000000000000000000000005'
        body=dict(ticket=ticket,group='기타',person=p)
        self.assertEqual(self.request('submit',body)[0],400)
        body['group']='남성'
        del p['own']['affection']
        self.assertEqual(self.request('submit',body)[0],400)
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM tickets').fetchone()[0],0)

    def test_legacy_data_survives_and_old_results_are_invalidated(self):
        old=profile(31)
        for k in ('vibe','affection'):
            old['own'].pop(k)
            old['ideal'].pop(k)
        old['ranks']=['look','date']
        old['ideal']={k:['상관없음'] for k in old['ideal']}
        with app.connect() as c:
            c.execute('INSERT INTO tickets(id,group_name) VALUES (?,?)',('LEGACY','남성'))
            c.execute('INSERT INTO participants(ticket,instagram,payload) VALUES (?,?,?)',('LEGACY',old['instagram'],json.dumps(old)))
            # Unfinished pre-issued tickets from v1 must not block new matching.
            c.execute('INSERT INTO tickets(id,group_name) VALUES (?,?)',('LEGACY-PENDING','여성'))
        self.add(32,'여성')
        self.login()
        run=self.request('match',{})[1]['run']
        self.assertEqual(len(run['matches']),1)
        self.assertEqual(app.directed(old,profile(32)),100)
        with app.connect() as c:
            saved=json.loads(c.execute("SELECT payload FROM participants WHERE ticket='LEGACY'").fetchone()[0])
            self.assertNotIn('vibe',saved['own'])
            run.pop('score_version')
            c.execute('UPDATE runs SET payload=? WHERE id=?',(json.dumps(run),run['id']))
        self.assertTrue(self.request('state')[1]['stale'])
        self.assertEqual(self.request('delivery',dict(run=run['id'],ticket='LEGACY',state='완료'))[0],400)


if __name__=='__main__':
    unittest.main()
