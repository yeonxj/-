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
from unittest.mock import patch
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
        body=dict(ticket=ticket,group=group,person=p,generation=self.request('session')[1]['generation'])
        code,result=self.request('submit',body)
        self.assertEqual(code,200,result)
        self.assertEqual(result['ticket'],ticket)
        retried=self.request('submit',body)
        self.assertEqual(retried[0],200)
        self.assertEqual(retried[1]['number'],result['number'])
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
        app.init()
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

    def test_reset_requires_admin_pin_and_preserves_recoverable_backup(self):
        t1,p1=self.add(1,'남성')
        self.add(2,'여성')
        generation=self.request('session')[1]['generation']
        request=dict(pin='1010',confirm='초기화',request_id='A'*32,generation=generation)
        self.assertEqual(self.request('reset',request)[0],401)
        self.login()
        run=self.request('match',{})[1]['run']
        self.request('delivery',dict(run=run['id'],ticket=t1,state='완료'))
        self.assertEqual(self.request('reset',{**request,'pin':'0000'})[0],403)
        self.assertEqual(self.request('reset',{**request,'confirm':'no'})[0],400)
        self.assertEqual(len(self.request('state')[1]['people']),2)
        code,result=self.request('reset',request)
        self.assertEqual(code,200,result)
        self.assertNotEqual(result['generation'],generation)
        state=self.request('state')[1]
        self.assertEqual(state['people'],[])
        self.assertEqual(state['tickets'],[])
        self.assertIsNone(state['run'])
        self.assertEqual(state['delivery'],{})
        backup=app.DATA/result['backup']
        self.assertTrue(backup.is_file())
        with sqlite3.connect(backup) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM participants').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM runs').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],1)
        # Another kiosk's old request must not put test data back after reset.
        self.assertEqual(self.request('submit',dict(ticket=t1,group='남성',person=p1,generation=generation))[0],409)
        self.assertEqual(self.request('submit',dict(ticket=t1,group='남성',person=p1))[0],409)
        self.add(3,'남성')
        self.assertEqual(self.request('state')[1]['people'][0]['number'],1)
        # A lost reset response followed by retry must not wipe subsequent registrations.
        self.assertEqual(self.request('reset',request),(200,result))
        self.assertEqual(len(self.request('state')[1]['people']),1)
        self.assertEqual(len(list((app.DATA/'reset-backups').glob('*.sqlite3'))),1)
        app.init()
        self.assertEqual(self.request('session')[1]['generation'],result['generation'])

    def test_failed_reset_backup_keeps_all_data(self):
        self.add(1,'남성')
        self.login()
        generation=self.request('session')[1]['generation']
        with patch.object(app,'snapshot',side_effect=OSError('disk unavailable')):
            code,result=self.request('reset',dict(pin='1010',confirm='초기화',request_id='B'*32,generation=generation))
        self.assertEqual(code,500)
        self.assertIn('기존 데이터는 유지',result['error'])
        self.assertEqual(len(self.request('state')[1]['people']),1)
        self.assertEqual(self.request('session')[1]['generation'],generation)

    def test_sequential_numbers_survive_restart_cancellation_and_failed_submission(self):
        t1,_=self.add(90,'남성')
        self.add(10,'여성')
        self.login()
        state=self.request('state')[1]
        self.assertEqual([(p['ticket'],p['number']) for p in sorted(state['people'],key=lambda p:p['number'])],[(t1,1),('P-'+format(10,'032X'),2)])
        self.request('cancel',dict(ticket=t1))
        app.init()
        self.add(5,'남성')
        self.assertEqual(sorted(p['number'] for p in self.request('state')[1]['people']),[2,3])
        body=dict(ticket='P-'+format(7,'032X'),group='여성',person=profile(10))
        self.assertEqual(self.request('submit',body)[0],409)
        body['person']['instagram']='unique_new_account'
        code,result=self.request('submit',body)
        self.assertEqual(code,200)
        self.assertEqual(result['number'],4)

    def test_number_migration_keeps_previous_match_and_delivery(self):
        self.add(9,'남성')
        self.add(3,'여성')
        self.login()
        run=self.request('match',{})[1]['run']
        ticket=run['matches'][0]['a']['ticket']
        self.request('delivery',dict(run=run['id'],ticket=ticket,state='완료'))
        # Recreate the previous schema and saved result shape before upgrading.
        with app.connect() as c:
            for m in run['matches']:
                m['a'].pop('number')
                m['b'].pop('number')
            c.execute('UPDATE runs SET payload=? WHERE id=?',(json.dumps(run),run['id']))
            c.execute('DROP TABLE participant_numbers')
        app.init()
        app.init()
        state=self.request('state')[1]
        self.assertEqual(sorted(p['number'] for p in state['people']),[1,2])
        self.assertFalse(state['stale'])
        self.assertEqual(state['delivery'][ticket],'완료')
        self.assertEqual({state['run']['matches'][0][k]['number'] for k in ('a','b')},{1,2})

    def test_concurrent_kiosks_receive_unique_consecutive_numbers(self):
        from concurrent.futures import ThreadPoolExecutor
        def submit(i):
            body=dict(ticket='P-'+format(i,'032X'),group='남성',person=profile(i))
            req=urllib.request.Request(self.base+'/api/submit',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())['number']
        with ThreadPoolExecutor(max_workers=4) as pool:
            numbers=list(pool.map(submit,range(1,9)))
        self.assertEqual(sorted(numbers),list(range(1,9)))


if __name__=='__main__':
    unittest.main()
