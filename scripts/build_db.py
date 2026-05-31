#!/usr/bin/env python3
"""
Webshare DB builder v4 – rozšířené dotazy pro maximální pokrytí
"""
import os, sys, json, time, hashlib, re, unicodedata, datetime, gzip
import urllib.request, urllib.parse
from xml.etree import ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

API   = 'https://webshare.cz/api/'
YEAR  = datetime.datetime.now().year
UA    = 'Mozilla/5.0'
OUT_DIR   = os.path.join(os.path.dirname(__file__), '..', 'db')
WORKERS   = 6
PAUSE     = 0.3
MAX_MIN   = 315

os.makedirs(OUT_DIR, exist_ok=True)

# ── HTTP ──────────────────────────────────────────────────────────────────────
def _call(endpoint, params, retries=3):
    data = urllib.parse.urlencode(params).encode()
    headers = {'X-Requested-With':'XMLHttpRequest','Referer':'https://webshare.cz/','User-Agent':UA}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API+endpoint, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                return ET.fromstring(r.read())
        except Exception as e:
            time.sleep(2*(2**attempt))
    return None

def _ok(root): return root is not None and root.findtext('status') == 'OK'

# ── Login ─────────────────────────────────────────────────────────────────────
ITOA64 = b'./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

def _to64(v, n):
    r = b''
    while n > 0: r += bytes([ITOA64[v & 0x3f]]); v >>= 6; n -= 1
    return r

def md5crypt(password, salt):
    if isinstance(password, str): password = password.encode()
    if isinstance(salt, str):     salt = salt.encode()
    if salt.startswith(b'$1$'):   salt = salt[3:]
    if b'$' in salt:              salt = salt[:salt.index(b'$')]
    salt = salt[:8]
    ctx  = hashlib.md5(password + b'$1$' + salt)
    ctx2 = hashlib.md5(password + salt + password)
    final = ctx2.digest()
    i = len(password)
    while i > 0: ctx.update(final[:min(i,16)]); i -= 16
    i = len(password)
    while i > 0: ctx.update(b'\x00' if i & 1 else password[:1]); i >>= 1
    final = ctx.digest()
    for i in range(1000):
        c2 = hashlib.md5()
        c2.update(password if i & 1 else final)
        if i % 3: c2.update(salt)
        if i % 7: c2.update(password)
        c2.update(final if i & 1 else password)
        final = c2.digest()
    r = b'$1$' + salt + b'$'
    r += _to64((final[0]<<16)|(final[6]<<8) |final[12], 4)
    r += _to64((final[1]<<16)|(final[7]<<8) |final[13], 4)
    r += _to64((final[2]<<16)|(final[8]<<8) |final[14], 4)
    r += _to64((final[3]<<16)|(final[9]<<8) |final[15], 4)
    r += _to64((final[4]<<16)|(final[10]<<8)|final[5],  4)
    r += _to64(final[11], 2)
    return r

def do_login(u, p):
    root = _call('salt/', {'username_or_email': u, 'wst': ''})
    if not _ok(root): raise RuntimeError('salt selhal')
    salt = root.findtext('salt','')
    enc  = md5crypt(p, salt)
    ph   = hashlib.sha1(enc).hexdigest()
    dig  = hashlib.md5((u+':Webshare:'+p).encode()).hexdigest()
    root = _call('login/', {'username_or_email':u,'password':ph,'digest':dig,'keep_logged_in':1,'wst':''})
    if not _ok(root): raise RuntimeError(root.findtext('message','?') if root else 'timeout')
    return root.findtext('token')

_tokens = {}; _tok_lock = threading.Lock()
_U = _P = None

def get_token():
    tid = threading.get_ident()
    with _tok_lock:
        if tid not in _tokens: _tokens[tid] = do_login(_U, _P)
    return _tokens[tid]

# ── Parsování ─────────────────────────────────────────────────────────────────
_SKIP = re.compile(r'\.(nfo|txt|srt|sub|ass|ssa|idx|jpg|png|zip|rar|7z|exe|pdf)$', re.I)
_JUNK = re.compile(
    r'[\.\s_\-]*(19[5-9]\d|20[0-2]\d)'
    r'|2160p|4k\b|uhd\b|bdremux\b|ultrahd\b|1080[pi]|720p|576p|480p|fullhd\b'
    r'|blu.?ray|web.?rip|web.?dl|hd.?rip|bdrip|dvdrip|dvdscr|hdtv|amzn|dsnp'
    r'|x\.?264|x\.?265|h\.?264|h\.?265|hevc|avc|xvid|divx'
    r'|aac\d?\.?\d?|ac3|dts|truehd|atmos|eac3|opus'
    r'|hdr10?(?:\+)?\b|extended|remastered|proper|repack'
    r'|czdab(?:ing)?|cz\.dabing|cz\+dabing|czech\b|slovensky|sk\.dabing|sk\+dabing'
    r'|\.[a-z]{2,4}$', re.IGNORECASE)
_EP = [re.compile(r'[Ss](\d{1,2})[Ee](\d{1,2})'),
       re.compile(r'[Ss](\d{1,2})[\s\._x-][Ee](\d{1,2})'),
       re.compile(r'\b(\d{1,2})x(\d{2})\b')]

def _norm(s):
    if not s: return ''
    s = unicodedata.normalize('NFKD',s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower(); s = re.sub(r'[^\w\s]',' ',s)
    s = re.sub(r'^\s*(the|a|an)\s+','',s)
    return re.sub(r'\s+',' ',s).strip()

def _quality(n):
    nl = n.lower()
    if any(x in nl for x in ['2160p','4k','uhd','bdremux']): return '4K'
    if any(x in nl for x in ['1080p','1080i','fullhd']): return '1080p'
    if '720p' in nl: return '720p'
    if any(x in nl for x in ['480p','dvdrip','dvdscr']): return 'SD'
    return ''

def _cz(n):
    nl=n.lower()
    return any(x in nl for x in ['.cz.','_cz_','-cz-',' cz ','czech','cesky','czdab','czdabing','cz dabing','cz tit'])

def _sk(n):
    nl=n.lower()
    return any(x in nl for x in ['.sk.','_sk_','-sk-',' sk ','slovak','slovensky','skdab','skdabing','sk dabing','sk tit'])

def _ep(n):
    for p in _EP:
        m=p.search(n)
        if m: return True,int(m.group(1)),int(m.group(2))
    return False,None,None

def _title(n):
    n=re.sub(r'\.[a-zA-Z0-9]{2,4}$','',n)
    ym=re.search(r'\b(19[5-9]\d|20[0-2]\d)\b',n)
    year=int(ym.group(1)) if ym else None
    parts=_JUNK.split(n); t=parts[0] if parts else n
    t=re.sub(r'[\._\-]+',' ',t).strip()
    return re.sub(r'\s+',' ',t),year

def _show(ct):
    t=ct or ''
    t=re.sub(r'\s*[Ss]\d{1,2}\s*[Ee]\d{1,2}.*','',t)
    t=re.sub(r'\s*\d{1,2}x\d{2}.*','',t)
    return re.sub(r'[\s\.\-_]+$','',t).strip() or ct

def parse(f):
    n=f.get('name','')
    if not n or _SKIP.search(n): return None
    is_s,sea,epi=_ep(n); t,year=_title(n)
    show=_show(t) if is_s else t
    q=_quality(n); cz=_cz(n); sk=_sk(n)
    v=int(f.get('positive_votes',0) or 0)
    score=float(v)+{'4K':40,'1080p':30,'720p':15,'SD':2}.get(q,0)+(50 if cz else 0)+(20 if sk else 0)
    return {'ident':f['ident'],'name':n,'clean_title':t,'norm_title':_norm(t),
            'show_title':show,'norm_show':_norm(show),'year':year,'season':sea,'episode':epi,
            'type':'series' if is_s else 'movie','quality':q,'cz':cz,'sk':sk,
            'size':int(f.get('size',0) or 0),'score':score}

# ── Dotazy – rozšířené pro maximální pokrytí ──────────────────────────────────
def build_queries():
    q = []

    # ── SERIÁLY ──────────────────────────────────────────────────────────────
    for s in range(1, 21):
        tag = f's{s:02d}e'
        q += [
            tag,
            f'{tag} cz',
            f'{tag} sk',
            f'{tag} 1080p',
            f'{tag} 720p',
            f'{tag} 4k',
            f'{tag} dabing',
            f'{tag} titulky',
            f'{tag} mkv',
        ]

    # Seriály podle roku - více proxy epizod
    for y in range(YEAR, 1995, -1):
        q += [
            f's01e01 {y}',
            f's01e01 cz {y}',
            f's01e01 sk {y}',
            f's01e01 1080p {y}',
            f's01e01 4k {y}',
            f's01e01 dabing {y}',
            f's02e01 {y}',
            f's03e01 {y}',
        ]

    # ── FILMY 2000+ ───────────────────────────────────────────────────────────
    for y in range(YEAR, 1999, -1):
        q += [
            f'1080p {y}',
            f'4k {y}',
            f'720p {y}',
            f'cz dabing {y}',
            f'czech {y}',
            f'bluray {y}',
            f'webrip {y}',
            f'sk dabing {y}',
            f'slovak {y}',
            f'remux {y}',
            f'web-dl {y}',
            f'hdrip {y}',
            f'cz tit {y}',
            f'cz titulky {y}',
            f'2160p {y}',
            f'uhd {y}',
            f'hdtv {y}',
            f'dvdrip {y}',
        ]

    # ── FILMY 1950-1999 ───────────────────────────────────────────────────────
    for y in range(1999, 1949, -1):
        q += [
            f'cz dabing {y}',
            f'czech {y}',
            f'1080p {y}',
            f'720p {y}',
            f'dvdrip {y}',
            f'sk dabing {y}',
            f'bluray {y}',
        ]

    # ── OBECNÉ ───────────────────────────────────────────────────────────────
    q += [
        'cz dabing 1080p', 'cz dabing 4k', 'cz dabing bluray',
        'sk dabing 1080p', 'sk dabing 4k', 'sk dabing bluray',
        '4k bluray', 'uhd bluray', 'bdremux', '1080p remux', '1080p bluray',
        'bdrip', 'dvdrip',
        'cz dabing 2160p', 'cz dabing uhd',
        'cz titulky 1080p', 'cz titulky 4k',
        'sk titulky 1080p',
        'hevc 1080p cz', 'x265 cz', 'x264 cz',
        'hdremux cz', 'bdremux cz',
    ]

    seen = set(); out = []
    for x in q:
        xs = x.strip()
        if xs and xs not in seen:
            seen.add(xs); out.append(xs)
    return out

# ── Fetch + store ─────────────────────────────────────────────────────────────
all_records=[]; seen_idents=set(); lock=threading.Lock(); stats={'done':0,'new':0}
START=time.time()

def fetch_query(q):
    token=get_token(); results=[]
    for page in range(2):
        root=_call('search/',{'what':q,'category':'video','sort':'rating',
                               'limit':100,'offset':page*100,'wst':token})
        if not _ok(root): break
        batch=[{'ident':f.findtext('ident',''),'name':f.findtext('name',''),
                'positive_votes':int(f.findtext('positive_votes',0) or 0),
                'size':int(f.findtext('size',0) or 0)}
               for f in root.findall('file')]
        results.extend(batch)
        if len(batch)<100: break
        time.sleep(PAUSE)
    time.sleep(PAUSE)
    return q, results

# ── Dedup + save ──────────────────────────────────────────────────────────────
def dedup_movies(records):
    best={}
    for r in records:
        k=r['norm_title']
        if not k: continue
        if k not in best or r['score']>best[k]['score']: best[k]=r
    return sorted(best.values(),key=lambda x:(-x['score'],x.get('clean_title') or ''))

def dedup_series(records):
    shows={}
    for r in records:
        k=r['norm_show']
        if not k or r['season'] is None: continue
        if k not in shows:
            shows[k]={'show_title':r['show_title'],'norm_show':k,
                      'year':r.get('year'),'cz':False,'sk':False,'episodes':{}}
        se=(r['season'],r['episode']); ep=shows[k]['episodes']
        if se not in ep or r['score']>ep[se]['score']: ep[se]=r
        if r['cz']: shows[k]['cz']=True
        if r['sk']: shows[k]['sk']=True
        if r.get('year') and (not shows[k]['year'] or r['year']>shows[k]['year']):
            shows[k]['year']=r['year']
    result=[]
    for show in shows.values():
        eps=sorted(show['episodes'].values(),key=lambda x:(x['season'],x['episode']))
        result.append({'show_title':show['show_title'],'norm_show':show['norm_show'],
                       'year':show['year'],'cz':show['cz'],'sk':show['sk'],
                       'ep_count':len(eps),
                       'episodes':[{'ident':e['ident'],'season':e['season'],'episode':e['episode'],
                                    'quality':e['quality'],'cz':e['cz'],'sk':e['sk'],'score':e['score']}
                                   for e in eps]})
    return sorted(result,key=lambda x:x.get('show_title') or '')

def save_json(path,data):
    os.makedirs(os.path.dirname(path),exist_ok=True)
    text=json.dumps(data,ensure_ascii=False,separators=(',',':'))
    with open(path,'w',encoding='utf-8') as f: f.write(text)
    with gzip.open(path+'.gz','wb') as f: f.write(text.encode())
    print(f'  Ulozeno: {os.path.basename(path)} ({os.path.getsize(path)//1024} KB, {len(data)} polozek)',flush=True)

def checkpoint():
    mv=dedup_movies([r for r in all_records if r['type']=='movie'])
    sr=dedup_series([r for r in all_records if r['type']=='series'])
    save_json(os.path.join(OUT_DIR,'movies.json'),mv)
    save_json(os.path.join(OUT_DIR,'series.json'),sr)
    meta={'updated':datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
          'movies_count':len(mv),'series_count':len(sr),'year':YEAR}
    save_json(os.path.join(OUT_DIR,'meta.json'),meta)
    print(f'  -> Checkpoint: {len(mv)} filmu, {len(sr)} serialu',flush=True)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _U,_P
    _U=os.environ.get('WS_USER'); _P=os.environ.get('WS_PASS')
    if not _U or not _P: print('ERROR: Nastav WS_USER a WS_PASS'); sys.exit(1)
    print(f'Prihlasovani jako {_U}...',flush=True)
    _tokens[threading.get_ident()]=do_login(_U,_P)
    print(f'Token OK',flush=True)

    queries=build_queries(); total=len(queries)
    print(f'Celkem dotazu: {total}',flush=True)
    print(f'Vlaken: {WORKERS}, pauza: {PAUSE}s',flush=True)
    print(f'Odhadovany cas: ~{int(total*PAUSE*2/WORKERS/60)}-{int(total*PAUSE*4/WORKERS/60)} minut',flush=True)
    print('─'*60,flush=True)

    last_ck=time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures={ex.submit(fetch_query,q):q for q in queries}
        for future in as_completed(futures):
            q,results=future.result()
            with lock:
                new=0
                for r in results:
                    if r['ident'] in seen_idents: continue
                    seen_idents.add(r['ident'])
                    p=parse(r)
                    if p: all_records.append(p); new+=1
                stats['done']+=1; stats['new']+=new
                done=stats['done']; elapsed=time.time()-START
                if done%50==0:
                    pct=int(done*100/total); eta=int((total-done)*(elapsed/max(done,1))/60)
                    print(f'[{pct:3d}%] {done}/{total} | ETA ~{eta}m | zaznamu: {len(all_records)}',flush=True)
                if time.time()-last_ck>180 or new>=200:
                    checkpoint(); last_ck=time.time()
                if (time.time()-START)/60>=MAX_MIN:
                    print('Casovy limit – ukladam.',flush=True)
                    ex.shutdown(wait=False,cancel_futures=True); break

    print('\n=== FINALE ===',flush=True)
    checkpoint()
    elapsed=int(time.time()-START)
    print(f'HOTOVO za {elapsed//60}m {elapsed%60}s',flush=True)

if __name__=='__main__': main()

# ── Aliasy pro kompatibilitu s update_db.py ───────────────────────────────────
def login(username, password):
    """Alias pro do_login() – kompatibilita s update_db.py."""
    global _U, _P
    _U = username
    _P = password
    token = do_login(username, password)
    _tokens[threading.get_ident()] = token
    return token

def parse_file(f):
    """Alias pro parse() – kompatibilita s update_db.py."""
    return parse(f)

def fetch_all_pages(query, token, max_pages=2, pause=0.3):
    """Stáhne všechny stránky pro daný dotaz – kompatibilita s update_db.py."""
    results = []
    for page in range(max_pages):
        root = _call('search/', {
            'what': query, 'category': 'video', 'sort': 'rating',
            'limit': 100, 'offset': page * 100, 'wst': token
        })
        if not _ok(root):
            break
        batch = [
            {
                'ident': f.findtext('ident', ''),
                'name': f.findtext('name', ''),
                'positive_votes': int(f.findtext('positive_votes', 0) or 0),
                'size': int(f.findtext('size', 0) or 0)
            }
            for f in root.findall('file')
        ]
        results.extend(batch)
        if len(batch) < 100:
            break
        time.sleep(pause)
    return results

def _normalize(s):
    """Alias pro _norm() – kompatibilita s update_db.py."""
    return _norm(s)
