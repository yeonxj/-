#!/usr/bin/env python3
"""Festival kiosk. Standard-library only; run behind HTTPS for remote access."""
import argparse
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('MATCH_DATA_DIR', str(ROOT / 'data')))
PIN = os.environ.get('ADMIN_PIN', '1010')
CATEGORIES = {
    'look': ['강아지상', '고양이상', '곰상', '여우상'],
    'social': ['조용한 편', '중간', '활발한 편'],
    'date': ['실내', '야외', '둘 다'],
    'plan': ['즉흥적', '중간', '계획적'],
    'vibe': ['테토형', '에겐형', '반반'],
    'affection': ['말로 표현', '행동으로 챙김', '둘 다'],
}
LABELS = dict(look='외적 이미지', social='사교 성향', date='데이트 취향', plan='계획 성향', vibe='테토·에겐 스타일', affection='애정 표현 방식')
SCORE_VERSION = 2
SESSIONS = {}
ATTEMPTS = {}
GUARD = threading.Lock()


def connect():
    c = sqlite3.connect(DATA / 'festival.sqlite3', timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA synchronous=FULL')
    return c


def init():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / 'backups').mkdir(exist_ok=True)
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS tickets (
          id TEXT PRIMARY KEY, group_name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'paid',
          created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS participants (
          ticket TEXT PRIMARY KEY REFERENCES tickets(id), instagram TEXT UNIQUE NOT NULL,
          payload TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS deliveries (
          run TEXT NOT NULL REFERENCES runs(id), ticket TEXT NOT NULL REFERENCES participants(ticket),
          state TEXT NOT NULL, PRIMARY KEY(run,ticket));
        ''')


def snapshot():
    stamp = str(time.time_ns())
    dest = DATA / 'backups' / (stamp + '.sqlite3')
    with connect() as src, sqlite3.connect(dest) as target:
        src.backup(target)
    # Keep the latest 100 consistent snapshots, separately from the live database.
    for old in sorted((DATA / 'backups').glob('*.sqlite3'))[:-100]:
        old.unlink()
    return dest


def validate(p):
    if not isinstance(p, dict):
        raise ValueError('올바른 응답이 아닙니다.')
    name = str(p.get('name', '')).strip()
    insta = str(p.get('instagram', '')).strip().lstrip('@').lower()
    import re
    if not name or len(name) > 24 or not re.fullmatch(r'[a-z0-9._]{1,30}', insta):
        raise ValueError('이름과 인스타 아이디를 확인해 주세요.')
    own, ideal = p.get('own', {}), p.get('ideal', {})
    if not isinstance(own, dict) or not isinstance(ideal, dict):
        raise ValueError('문항을 확인해 주세요.')
    for k, opts in CATEGORIES.items():
        if own.get(k) not in opts:
            raise ValueError('나의 성향을 모두 선택해 주세요.')
        v = ideal.get(k)
        if not isinstance(v, list) or not v or len(v) != len(set(map(str, v))) or any(x not in opts + ['상관없음'] for x in v):
            raise ValueError('원하는 상대 성향을 모두 선택해 주세요.')
        if '상관없음' in v and len(v) != 1:
            raise ValueError('상관없음은 단독으로 선택해 주세요.')
    ranks = p.get('ranks')
    if not isinstance(ranks, list) or len(ranks) != 2 or any(x not in CATEGORIES for x in ranks) or ranks[0] == ranks[1]:
        raise ValueError('서로 다른 중요도 1·2순위를 선택해 주세요.')
    if p.get('consent') is not True:
        raise ValueError('정보 수집과 상대방 인스타 전달 동의가 필요합니다.')
    return dict(name=name, instagram=insta, own=own, ideal=ideal, ranks=ranks, consent=True, consent_version='2026-01')


def directed(a, b):
    total = 0
    weight_sum = 0
    for k, opts in CATEGORIES.items():
        # Earlier four-question responses remain intact; only known answers count.
        prefs = a['ideal'].get(k)
        actual = b['own'].get(k)
        if not prefs or actual is None:
            continue
        if prefs == ['상관없음'] or actual in prefs or (k in ('date', 'affection') and (actual == '둘 다' or '둘 다' in prefs)):
            value = 1
        elif k in ('social', 'plan'):
            distance = min(abs(opts.index(actual) - opts.index(x)) for x in prefs)
            value = .5 if distance == 1 else 0
        else:
            value = 0
        weight = 3 if a['ranks'][0] == k else 2 if a['ranks'][1] == k else 1
        total += weight * value
        weight_sum += weight
    return total / weight_sum * 100 if weight_sum else 0


def hungarian(matrix):
    n = len(matrix)
    if not n:
        return []
    m = len(matrix[0])
    u, v, p, way = [0]*(n+1), [0]*(m+1), [0]*(m+1), [0]*(m+1)
    for i in range(1, n+1):
        p[0] = i
        j0 = 0
        minv, used = [float('inf')]*(m+1), [False]*(m+1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float('inf'), 0
            for j in range(1, m+1):
                if not used[j]:
                    cur = -matrix[i0-1][j-1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m+1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    ans = [-1]*n
    for j in range(1, m+1):
        if p[j]:
            ans[p[j]-1] = j-1
    return ans


def optimize(people):
    a = sorted([x for x in people if x['group'] == '남성'], key=lambda x: x['ticket'])
    b = sorted([x for x in people if x['group'] == '여성'], key=lambda x: x['ticket'])
    if len(a) > len(b):
        a, b = b, a
    count = len(a)
    if not count:
        return dict(matches=[], unmatched=people, floor=None, total=0)
    pairs = [[(directed(x,y), directed(y,x)) for y in b] for x in a]
    thresholds = sorted(set(min(x,y) for row in pairs for x,y in row))
    def assignment(floor):
        return hungarian([[math.sqrt(x*y) if min(x,y) >= floor - 1e-9 else -1e9 for x,y in row] for row in pairs])
    lo, hi, best = 0, len(thresholds)-1, 0
    while lo <= hi:
        mid = (lo+hi)//2
        ass = assignment(thresholds[mid])
        if all(min(pairs[i][j]) >= thresholds[mid]-1e-9 for i,j in enumerate(ass)):
            best, lo = mid, mid+1
        else:
            hi = mid-1
    floor = thresholds[best]
    ass = assignment(floor)
    matches, used = [], set()
    for i,j in enumerate(ass):
        x,y = pairs[i][j]
        matches.append(dict(a=a[i], b=b[j], aToB=round(x,1), bToA=round(y,1), score=round(math.sqrt(x*y),1)))
        used.update([a[i]['ticket'],b[j]['ticket']])
    return dict(matches=matches, unmatched=[x for x in people if x['ticket'] not in used], floor=round(floor,1), total=round(sum(m['score'] for m in matches),1))


def people(c):
    return [dict(json.loads(r['payload']), ticket=r['ticket'], group=r['group_name']) for r in c.execute("SELECT p.*,t.group_name FROM participants p JOIN tickets t ON p.ticket=t.id WHERE t.state='paid' ORDER BY t.created,t.id")]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Avoid recording IDs and private payloads in access logs.
        pass

    def reply(self, obj, status=200, cookie=None, download=None):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/octet-stream' if download else 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="'+download+'"')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def session(self):
        cookies = SimpleCookie()
        cookies.load(self.headers.get('Cookie', ''))
        key = cookies.get('festival')
        s = SESSIONS.get(key.value) if key else None
        return s if s and s['expires'] > time.time() else {}

    def admin(self):
        if self.session().get('admin',0) < time.time():
            raise PermissionError('운영진 비밀번호를 입력해 주세요.')

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            raw = (ROOT/'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        try:
            if path == '/api/session':
                s = self.session()
                return self.reply(dict(admin=s.get('admin',0)>time.time()))
            self.admin()
            if path == '/api/state':
                with connect() as c:
                    tickets = [dict(r) for r in c.execute('SELECT t.*,p.ticket IS NOT NULL AS submitted FROM tickets t LEFT JOIN participants p ON p.ticket=t.id ORDER BY t.created DESC,t.id')]
                    latest = c.execute('SELECT * FROM runs ORDER BY created DESC,rowid DESC LIMIT 1').fetchone()
                    run = json.loads(latest['payload']) if latest else None
                    delivery = {r['ticket']:r['state'] for r in c.execute('SELECT * FROM deliveries WHERE run=?',(latest['id'],))} if latest else {}
                    ps = people(c)
                    return self.reply(dict(tickets=tickets,people=ps,run=run,delivery=delivery,stale=bool(run and (run['people_digest'] != digest(ps) or run.get('score_version') != SCORE_VERSION))))
            if path == '/api/backup':
                file = snapshot()
                return self.reply(file.read_bytes(), download='festival-backup.sqlite3')
            return self.reply(dict(error='찾을 수 없습니다.'),404)
        except PermissionError as e:
            self.reply(dict(error=str(e)),401)
        except Exception:
            self.reply(dict(error='조회 실패. 서버 상태를 확인해 주세요.'),500)

    def do_POST(self):
        try:
            # No cross-origin writes, including login requests.
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.reply(dict(error='허용되지 않은 요청입니다.'),403)
            if not self.headers.get('Content-Type','').startswith('application/json'):
                return self.reply(dict(error='JSON 요청이 필요합니다.'),415)
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length < 32768:
                raise ValueError('요청 크기를 확인해 주세요.')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError('올바른 요청이 아닙니다.')
            path = urlsplit(self.path).path
            with GUARD:
                self.post(path, data)
        except PermissionError as e:
            self.reply(dict(error=str(e)),401)
        except (ValueError, KeyError, TypeError) as e:
            self.reply(dict(error=str(e)),400)
        except sqlite3.IntegrityError:
            self.reply(dict(error='이미 접수된 인스타 아이디입니다. 운영진에게 확인해 주세요.'),409)
        except Exception:
            self.reply(dict(error='저장 여부를 확인하지 못했습니다. 입력을 지우지 말고 다시 시도해 주세요.'),500)

    def post(self,path,data):
        if path == '/api/login':
            addr = self.client_address[0]
            tries = [t for t in ATTEMPTS.get(addr,[]) if t > time.time()-60]
            ATTEMPTS[addr] = tries
            if len(tries) >= 10:
                return self.reply(dict(error='잠시 후 다시 시도해 주세요. (1분 제한)'),429)
            if not hmac.compare_digest(str(data.get('pin','')),PIN):
                tries.append(time.time())
                return self.reply(dict(error='비밀번호가 맞지 않습니다.'),401)
            ATTEMPTS.pop(addr,None)
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = dict(expires=time.time()+12*3600,admin=time.time()+1800)
            cookie = 'festival='+token+'; HttpOnly; SameSite=Strict; Path=/'
            if os.environ.get('COOKIE_SECURE') == '1':
                cookie += '; Secure'
            return self.reply(dict(ok=True),cookie=cookie)
        if path == '/api/submit':
            import re
            ticket = data.get('ticket')
            if not isinstance(ticket, str) or not re.fullmatch(r'P-[A-F0-9]{16,32}', ticket):
                raise ValueError('접수 식별자가 올바르지 않습니다. 화면을 새로 열어 주세요.')
            group = data.get('group')
            if group not in ('남성', '여성'):
                raise ValueError('성별을 선택해 주세요.')
            p = validate(data.get('person'))
            with connect() as c:
                prior = c.execute('SELECT p.payload,t.group_name,t.state FROM participants p JOIN tickets t ON p.ticket=t.id WHERE p.ticket=?', (ticket,)).fetchone()
                if prior:
                    if prior['state'] != 'paid':
                        raise ValueError('취소된 접수번호입니다. 운영진에게 문의해 주세요.')
                    if json.loads(prior['payload']) != p or prior['group_name'] != group:
                        raise ValueError('이미 저장된 접수입니다. 운영진에게 확인해 주세요.')
                else:
                    # Ticket and response are committed together: no pre-issuance or payment gate.
                    c.execute('INSERT INTO tickets(id,group_name) VALUES (?,?)', (ticket,group))
                    c.execute('INSERT INTO participants(ticket,instagram,payload) VALUES (?,?,?)', (ticket,p['instagram'],json.dumps(p,ensure_ascii=False)))
            # Commit is complete even if a secondary snapshot fails.
            warning = None
            try:
                snapshot()
            except Exception:
                warning = '원본 저장은 완료되었으나 자동 백업 실패. 운영진이 백업을 확인해 주세요.'
            return self.reply(dict(ok=True,ticket=ticket,warning=warning))
        self.admin()
        if path == '/api/lock':
            self.session()['admin'] = 0
            return self.reply(dict(ok=True))
        if path == '/api/cancel':
            with connect() as c:
                c.execute("UPDATE tickets SET state='cancelled' WHERE id=?",(data.get('ticket'),))
            return self.reply(dict(ok=True))
        if path == '/api/match':
            with connect() as c:
                ps = people(c)
                if not any(p['group']=='남성' for p in ps) or not any(p['group']=='여성' for p in ps):
                    raise ValueError('남성과 여성이 각각 1명 이상 필요합니다.')
                if len(ps)>400:
                    raise ValueError('회차당 최대 400명입니다. 운영 방식을 확인해 주세요.')
                result = optimize(ps)
                result.update(id=secrets.token_hex(8),created=time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime()),people_digest=digest(ps),score_version=SCORE_VERSION)
                c.execute('INSERT INTO runs(id,payload) VALUES (?,?)',(result['id'],json.dumps(result,ensure_ascii=False)))
            return self.reply(dict(run=result))
        if path == '/api/delivery':
            with connect() as c:
                r = c.execute('SELECT payload FROM runs WHERE id=?',(data.get('run'),)).fetchone()
                if not r:
                    raise ValueError('매칭 결과가 없습니다.')
                run = json.loads(r['payload'])
                if run['people_digest'] != digest(people(c)) or run.get('score_version') != SCORE_VERSION:
                    raise ValueError('참가자 명단이 변경되었습니다. 다시 매칭해 주세요.')
                valid = {p['ticket'] for m in run['matches'] for p in (m['a'],m['b'])}
                if data.get('ticket') not in valid or data.get('state') not in ('대기','완료','실패'):
                    raise ValueError('발송 정보를 확인해 주세요.')
                c.execute('INSERT OR REPLACE INTO deliveries(run,ticket,state) VALUES (?,?,?)',(run['id'],data['ticket'],data['state']))
            return self.reply(dict(ok=True))
        if path == '/api/verify':
            with connect() as c:
                r = c.execute('SELECT payload FROM participants WHERE ticket=?',(data.get('ticket'),)).fetchone()
                return self.reply(dict(saved=bool(r)))
        self.reply(dict(error='찾을 수 없습니다.'),404)


def digest(ps):
    return hashlib.sha256(json.dumps(ps,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--restore', metavar='SQLITE_BACKUP')
    args = parser.parse_args()
    if args.restore:
        DATA.mkdir(parents=True,exist_ok=True)
        dbpath = DATA/'festival.sqlite3'
        if dbpath.exists():
            parser.error('복구 대상 data 폴더가 비어 있어야 합니다. 기존 데이터를 먼저 별도로 보존하세요.')
        source = Path(args.restore).resolve()
        with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as src:
            if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                parser.error('백업 무결성 검사 실패')
            with sqlite3.connect(dbpath) as dst:
                src.backup(dst)
    init()
    print(f'Festival server: {args.host}:{args.port}. Data: {DATA}',flush=True)
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
