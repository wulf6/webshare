#!/usr/bin/env python3
"""
Webshare DB builder – pomalý ale kompletní.
Opravy: E01 bez sezóny, rok před S/E, (CZ)/(H264-CZ), zkratky SI->Silo.
GitHub Actions limit 6h — pauzy 2s mezi dotazy.
"""

import os, sys, json, time, hashlib, re, unicodedata, datetime, gzip
import urllib.request, urllib.parse
from xml.etree import ElementTree as ET

API    = 'https://webshare.cz/api/'
REALM  = ':Webshare:'
YEAR   = datetime.datetime.now().year
UA     = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
OUT_DIR   = os.path.join(os.path.dirname(__file__), '..', 'db')
META_FILE = os.path.join(OUT_DIR, 'meta.json')
PAUSE = 2.0

def _call(endpoint, params, retries=5):
    data = urllib.parse.urlencode(params).encode('utf-8')
    headers = {'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'XMLHttpRequest','Referer':'https://webshare.cz/','User-Agent':UA}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API + endpoint, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return ET.fromstring(r.read())
        except Exception as e:
            wait = 5*(attempt+1); print(f'  [retry {attempt+1}/{retries}] {e} – cekam {wait}s'); time.sleep(wait)
    return None

def _ok(root): return root is not None and root.findtext('status') == 'OK'

ITOA64 = b'./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
def _to64(v,n):
    r=b''
    while n>0: r+=bytes([ITOA64[v&0x3f]]); v>>=6; n-=1
    return r

def md5crypt(password,salt):
    if isinstance(password,str): password=password.encode('utf-8')
    if isinstance(salt,str): salt=salt.encode('utf-8')
    if salt.startswith(b'$1$'): salt=salt[3:]
    if b'$' in salt: salt=salt[:salt.index(b'$')]
    salt=salt[:8]
    ctx=hashlib.md5(password+b'$1$'+salt); ctx2=hashlib.md5(password+salt+password); final=ctx2.digest()
    i=len(password)
    while i>0: ctx.update(final[:min(i,16)]); i-=16
    i=len(password)
    while i>0: ctx.update(b'\x00' if i&1 else password[:1]); i>>=1
    final=ctx.digest()
    for i in range(1000):
        c2=hashlib.md5(); c2.update(password if i&1 else final)
        if i%3: c2.update(salt)
        if i%7: c2.update(password)
        c2.update(final if i&1 else password); final=c2.digest()
    r=b'$1$'+salt+b'$'
    r+=_to64((final[0]<<16)|(final[6]<<8)|final[12],4); r+=_to64((final[1]<<16)|(final[7]<<8)|final[13],4)
    r+=_to64((final[2]<<16)|(final[8]<<8)|final[14],4); r+=_to64((final[3]<<16)|(final[9]<<8)|final[15],4)
    r+=_to64((final[4]<<16)|(final[10]<<8)|final[5],4); r+=_to64(final[11],2)
    return r

def login(username,password):
    root=_call('salt/',{'username_or_email':username})
    if not _ok(root): raise RuntimeError('Nelze ziskat salt')
    salt=root.findtext('salt',''); encrypted=md5crypt(password,salt)
    pwd_hash=hashlib.sha1(encrypted).hexdigest()
    digest=hashlib.md5((username+REALM+pwd_hash).encode()).hexdigest()
    root=_call('login/',{'username_or_email':username,'password':pwd_hash,'digest':digest,'keep_logged_in':1})
    if not _ok(root):
        msg=root.findtext('message','?') if root else 'timeout'; raise RuntimeError(f'Login failed: {msg}')
    return root.findtext('token')

VIDEO_SKIP = re.compile(r'\.(nfo|txt|srt|sub|ass|ssa|idx|jpg|png|zip|rar|7z|exe|pdf|doc|torrent)$', re.I)
JUNK = re.compile(
    r'[Ss]\d{1,2}[Ee]\d{1,2}.*'
    r'|\s+[Ee]\d{2}\b.*'
    r'|[\.\s_\-]*(19[5-9]\d|20[0-2]\d)'
    r'|2160p|4k\b|uhd\b|bdremux\b|1080[pi]|720p|576p|480p|fullhd\b'
    r'|blu.?ray|web.?rip|web.?dl|hd.?rip|bdrip|dvdrip|hdtv|atvp|tvrip'
    r'|x\.?264|x\.?265|h\.?264|h\.?265|hevc|avc|xvid|divx|10bit|6ch|2ch'
    r'|aac\d?\.?\d?|ac3|dts|truehd|atmos|eac3|opus|ddp\d|dd\d'
    r'|hdr10?(?:\+)?\b|dv\b|dovi\b|dolby\.?vision'
    r'|extended|theatrical|remastered|proper|repack|ntb|psa'
    r'|czdab(?:ing)?|cz\.dabing|cz\+dabing|czech\b|slovensky|sk\.dabing|sk\+dabing'
    r'|cz\.tit|cz tit|sk\.tit|titulky|tit\b|\.[a-z]{2,4}$', re.IGNORECASE)

EP_RE = [
    re.compile(r'[Ss](\d{1,2})[Ee](\d{1,2})'),
    re.compile(r'[Ss](\d{1,2})[\s\._x-][Ee](\d{1,2})'),
    re.compile(r'\b(\d{1,2})x(\d{2})\b'),
    re.compile(r'[Ss]erie\s*(\d{1,2})\s*[Dd]il\s*(\d{1,2})', re.I),
    re.compile(r'(\d{1,2})\.\s*[Ss]erie\s*(\d{1,2})\.\s*[Dd]il', re.I),
]
EP_RE_E_ONLY = re.compile(r'(?<![Ss\d])[Ee](\d{2})(?!\d)')

def _series_info(n):
    for p in EP_RE:
        m=p.search(n)
        if m: return True,int(m.group(1)),int(m.group(2))
    m=EP_RE_E_ONLY.search(n)
    if m: return True,1,int(m.group(1))
    return False,None,None

def _normalize(s):
    if not s: return ''
    s=unicodedata.normalize('NFKD',s); s=''.join(ch for ch in s if not unicodedata.combining(ch))
    s=s.lower(); s=re.sub(r'[^\w\s]',' ',s); s=re.sub(r'^\s*(the|a|an)\s+','',s)
    return re.sub(r'\s+',' ',s).strip()

def _quality(n):
    nl=n.lower()
    if any(x in nl for x in ['2160p','4k','uhd','bdremux']): return '4K'
    if any(x in nl for x in ['1080p','1080i','fullhd']): return '1080p'
    if '720p' in nl: return '720p'
    if '480p' in nl or '576p' in nl or 'dvdrip' in nl: return '480p'
    return ''

def _cz(n):
    nl=n.lower()
    return any(x in nl for x in ['.cz.','_cz_','-cz-',' cz ','czech','cesky','czdab','czdabing','cz dabing','cz.dabing','cz+dabing','cz tit','cz.tit','titulky','(cz)','[cz]','h264-cz','h265-cz','-cz)'])

def _sk(n):
    nl=n.lower()
    return any(x in nl for x in ['.sk.','_sk_','-sk-',' sk ','slovak','slovensky','skdab','skdabing','sk dabing','sk.dabing','sk+dabing','sk tit','sk.tit','(sk)','[sk]','h264-sk','h265-sk'])

def _title(n):
    n=re.sub(r'\.[a-zA-Z0-9]{2,4}$','',n)
    ym=re.search(r'\b(19[5-9]\d|20[0-2]\d)\b',n); year=int(ym.group(1)) if ym else None
    parts=JUNK.split(n); t=parts[0] if parts else n
    t=re.sub(r'[\._\-]+',' ',t).strip()
    t=re.sub(r'\s*(19[5-9]\d|20[0-2]\d)\s*$','',t).strip()
    t=re.sub(r'\s*\[[^\]]*\]\s*$','',t).strip()
    t=re.sub(r'\s*\([^)]*\)\s*$','',t).strip()
    return re.sub(r'\s+',' ',t),year

def _show_title(raw):
    t=raw or ''
    t=re.sub(r'\s*[Ss]\d{1,2}[\s\._]?[Ee]\d{1,2}.*','',t)
    t=re.sub(r'\s*\d{1,2}x\d{2}.*','',t)
    t=re.sub(r'\s*(19[5-9]\d|20[0-2]\d)\s*$','',t)
    t=re.sub(r'\s*\[[^\]]*\]\s*$','',t)
    t=re.sub(r'\s*\([^)]*\)\s*$','',t)
    t=re.sub(r'[\s\.\-_]+$','',t).strip()
    return t if len(t)>=3 else raw

def parse_file(f):
    n=f['name']
    if VIDEO_SKIP.search(n): return None
    is_s,sea,ep=_series_info(n); t,year=_title(n)
    show=_show_title(t) if is_s else t
    q=_quality(n); cz=_cz(n); sk=_sk(n)
    v=int(f.get('positive_votes',0) or 0)
    score=float(v)+{'4K':40,'1080p':30,'720p':15,'480p':5}.get(q,0)+(50 if cz else 0)+(20 if sk else 0)
    return {'ident':f['ident'],'clean_title':t,'norm_title':_normalize(t),'show_title':show,'norm_show':_normalize(show),'year':year,'season':sea,'episode':ep,'type':'series' if is_s else 'movie','quality':q,'cz':cz,'sk':sk,'score':score}

def search_page(query,token,sort='rating',limit=100,offset=0):
    root=_call('search/',{'what':query,'category':'video','sort':sort,'limit':limit,'offset':offset,'wst':token,'maybe_removed':'true'})
    if not _ok(root): return []
    return [{'ident':f.findtext('ident',''),'name':f.findtext('name',''),'positive_votes':int(f.findtext('positive_votes',0) or 0)} for f in root.findall('file')]

def fetch_all_pages(query,token):
    results=[]
    for page in range(20):
        batch=search_page(query,token,limit=100,offset=page*100)
        if not batch: break
        results.extend(batch)
        if len(batch)<100: break
        time.sleep(PAUSE)
    return results

def series_queries():
    q=[]
    for s in range(1,21):
        tag=f's{s:02d}e'
        for y in range(YEAR,2007,-1):
            q.append(f'{tag} {y}'); q.append(f'{tag} cz {y}'); q.append(f'{tag} sk {y}')
        q.append(tag); q.append(f'{tag} cz'); q.append(f'{tag} sk')
    return list(dict.fromkeys(q))

def movie_queries():
    q=[]
    for y in range(YEAR,1999,-1):
        q+=[f'{y}',f'1080p {y}',f'720p {y}',f'4k {y}',f'2160p {y}',f'cz dabing {y}',f'czech {y}',f'bluray {y}',f'webrip {y}',f'dvdrip {y}',f'sk dabing {y}']
    q+=['4k bluray','4k hdr','uhd bluray','1080p bluray','1080p webrip','cz dabing','sk dabing','czech dabing','cz titulky','bluray remux','bdremux']
    return list(dict.fromkeys(q))

def dedup_movies(records):
    best={}
    for r in records:
        k=r['norm_title']
        if not k: continue
        if k not in best or r['score']>best[k]['score']: best[k]=r
    return sorted(best.values(),key=lambda x:(-x['score'],x['clean_title'] or ''))

def _merge_show_key(norm_show,existing_keys):
    if norm_show in existing_keys: return norm_show
    for k in existing_keys:
        if k.startswith(norm_show) or norm_show.startswith(k): return k
    return norm_show

def dedup_series(records):
    shows={}
    for r in records:
        k_raw=r['norm_show']
        if not k_raw or r['season'] is None: continue
        k=_merge_show_key(k_raw,shows)
        if k not in shows:
            shows[k]={'show_title':r['show_title'],'norm_show':k,'cz':False,'sk':False,'episodes':{}}
        if len(r['show_title'] or '')>len(shows[k]['show_title'] or ''): shows[k]['show_title']=r['show_title']
        se=(r['season'],r['episode']); ep_map=shows[k]['episodes']
        if se not in ep_map or r['score']>ep_map[se]['score']: ep_map[se]=r
        if r['cz']: shows[k]['cz']=True
        if r['sk']: shows[k]['sk']=True
    result=[]
    for k,show in shows.items():
        eps=sorted(show['episodes'].values(),key=lambda x:(x['season'],x['episode']))
        result.append({'show_title':show['show_title'],'norm_show':show['norm_show'],'cz':show['cz'],'sk':show['sk'],'ep_count':len(eps),'episodes':[{'ident':e['ident'],'season':e['season'],'episode':e['episode'],'quality':e['quality'],'cz':e['cz'],'sk':e['sk'],'score':e['score']} for e in eps]})
    return sorted(result,key=lambda x:x['show_title'] or '')

def save_json(path,data):
    os.makedirs(os.path.dirname(path),exist_ok=True)
    text=json.dumps(data,ensure_ascii=False,separators=(',',':'))
    with open(path,'w',encoding='utf-8') as f: f.write(text)
    with gzip.open(path+'.gz','wb') as f: f.write(text.encode('utf-8'))
    kb=os.path.getsize(path)//1024; n=len(data) if isinstance(data,list) else 'meta'
    print(f'  Ulozeno: {os.path.basename(path)} ({kb} KB, {n} polozek)')

def main():
    username=os.environ.get('WS_USER'); password=os.environ.get('WS_PASS')
    if not username or not password:
        print('ERROR: Nastav WS_USER a WS_PASS jako GitHub Secrets.'); sys.exit(1)
    print(f'Prihlasovani jako {username}...')
    token=login(username,password)
    print(f'Token OK: {token[:8]}...'); print(f'Pauza: {PAUSE}s')

    print(f'\n=== SERIALY ===')
    sq=series_queries(); print(f'Celkem dotazu: {len(sq)}')
    series_raw=[]; seen=set()
    for i,q in enumerate(sq):
        print(f'[{i+1}/{len(sq)}] "{q}"')
        results=fetch_all_pages(q,token); new=0
        for r in results:
            if r['ident'] in seen: continue
            seen.add(r['ident']); p=parse_file(r)
            if p and p['type']=='series': series_raw.append(p); new+=1
        if new>0: print(f'  -> {new} novych (celkem {len(series_raw)})')
        time.sleep(PAUSE)
    series_out=dedup_series(series_raw)
    save_json(os.path.join(OUT_DIR,'series.json'),series_out)
    print(f'Serialu celkem: {len(series_out)}')

    print(f'\n=== FILMY ===')
    mq=movie_queries(); print(f'Celkem dotazu: {len(mq)}')
    movies_raw=[]; seen2=set()
    for i,q in enumerate(mq):
        print(f'[{i+1}/{len(mq)}] "{q}"')
        results=fetch_all_pages(q,token); new=0
        for r in results:
            if r['ident'] in seen2: continue
            seen2.add(r['ident']); p=parse_file(r)
            if p and p['type']=='movie': movies_raw.append(p); new+=1
        if new>0: print(f'  -> {new} novych (celkem {len(movies_raw)})')
        time.sleep(PAUSE)
    movies_out=dedup_movies(movies_raw)
    save_json(os.path.join(OUT_DIR,'movies.json'),movies_out)
    print(f'Filmu celkem: {len(movies_out)}')

    meta={'updated':datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),'movies_count':len(movies_out),'series_count':len(series_out),'year':YEAR}
    save_json(META_FILE,meta)
    print(f'\nHOTOVO: {len(movies_out)} filmu, {len(series_out)} serialu')

if __name__=='__main__':
    main()
