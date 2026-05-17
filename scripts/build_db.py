#!/usr/bin/env python3
"""
Webshare DB builder v2.0
- Paralelni stahovani (6 vlaken)
- Bez zbytecnych serialu po jmenu (s01e01 dotazy je najdou stejne)
- Pauza 0.3s misto 1.5s
- Checkpoint zachovan
- Cil: dokoncit za 1.5-2.5 hodiny
"""

import os, sys, json, time, hashlib, re, unicodedata, datetime, gzip
import urllib.request, urllib.parse
from xml.etree import ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

API   = 'https://webshare.cz/api/'
YEAR  = datetime.datetime.now().year
UA    = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

OUT_DIR   = os.path.join(os.path.dirname(__file__), '..', 'db')
META_FILE = os.path.join(OUT_DIR, 'meta.json')
CKPT_FILE = os.path.join(OUT_DIR, '_checkpoint.json')

WORKERS     = 6    # paralelni vlakna
PAUSE       = 0.3  # pauza mezi requesty v jednom vlakne
MAX_MINUTES = 315  # bezpecnostni limit

os.makedirs(OUT_DIR, exist_ok=True)

# ── HTTP ──────────────────────────────────────────────────────────────────────
def _call(endpoint, params, retries=3):
    data = urllib.parse.urlencode(params).encode('utf-8')
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://webshare.cz/',
        'User-Agent': UA,
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API + endpoint, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                return ET.fromstring(r.read())
        except Exception as e:
            wait = 2 * (2 ** attempt)
            print(f'  [retry {attempt+1}/{retries}] {e} – wait {wait}s')
            time.sleep(wait)
    return None

def _ok(root): return root is not None and root.findtext('status') == 'OK'

# ── Login ─────────────────────────────────────────────────────────────────────
ITOA64 = b'./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

def _to64(v, n):
    r = b''
    while n > 0: r += bytes([ITOA64[v & 0x3f]]); v >>= 6; n -= 1
    return r

def md5crypt(password, salt):
    if isinstance(password, str): password = password.encode('utf-8')
    if isinstance(salt, str):     salt = salt.encode('utf-8')
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

def do_login(username, password):
    root = _call('salt/', {'username_or_email': username, 'wst': ''})
    if not _ok(root): raise RuntimeError('Nelze ziskat salt')
    salt     = root.findtext('salt', '')
    enc      = md5crypt(password, salt)
    pwd_hash = hashlib.sha1(enc).hexdigest()
    digest   = hashlib.md5((username + ':Webshare:' + password).encode()).hexdigest()
    root = _call('login/', {
        'username_or_email': username,
        'password': pwd_hash, 'digest': digest,
        'keep_logged_in': 1, 'wst': '',
    })
    if not _ok(root):
        raise RuntimeError(f'Login selhal: {root.findtext("message","?") if root else "timeout"}')
    return root.findtext('token')

# Sdilene tokeny per vlakno
_tokens = {}
_tok_lock = threading.Lock()
_username = None
_password = None

def get_token():
    tid = threading.get_ident()
    with _tok_lock:
        if tid not in _tokens:
            _tokens[tid] = do_login(_username, _password)
    return _tokens[tid]

# ── Parsování (beze změny) ────────────────────────────────────────────────────
VIDEO_SKIP = re.compile(
    r'\.(nfo|txt|srt|sub|ass|ssa|idx|jpg|png|zip|rar|7z|exe|pdf|doc|docx)$', re.I)

JUNK = re.compile(
    r'[\.\s_\-]*(19[5-9]\d|20[0-2]\d)'
    r'|2160p|4k\b|uhd\b|bdremux\b|ultrahd\b'
    r'|1080[pi]|720p|576p|480p|fullhd\b|fhd\b'
    r'|blu.?ray|web.?rip|web.?dl|hd.?rip|bdrip|dvdrip|dvdscr|hdtv|pdtv|amzn|dsnp'
    r'|x\.?264|x\.?265|h\.?264|h\.?265|hevc|avc|xvid|divx'
    r'|aac\d?\.?\d?|ac3|dts|truehd|atmos|eac3|opus'
    r'|hdr10?(?:\+)?\b|dv\b|dolby\.?vision'
    r'|extended|theatrical|remastered|proper|repack|internal|retail'
    r'|czdab(?:ing)?|cz\.dabing|cz\+dabing|czech\b|slovensky|sk\.dabing|sk\+dabing'
    r'|\.[a-z]{2,4}$', re.IGNORECASE)

EP_RE = [
    re.compile(r'[Ss](\d{1,2})[Ee](\d{1,2})'),
    re.compile(r'[Ss](\d{1,2})[\s\._x-][Ee](\d{1,2})'),
    re.compile(r'\b(\d{1,2})x(\d{2})\b'),
]

def _normalize(s):
    if not s: return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'^\s*(the|a|an)\s+', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def _quality(n):
    nl = n.lower()
    if any(x in nl for x in ['2160p','4k','uhd','bdremux','ultrahd']): return '4K'
    if any(x in nl for x in ['1080p','1080i','fullhd','fhd']): return '1080p'
    if '720p' in nl: return '720p'
    if any(x in nl for x in ['480p','576p','dvdrip','dvdscr','dvd']): return 'SD'
    return ''

def _cz(n):
    nl = n.lower()
    return any(x in nl for x in ['.cz.','_cz_','-cz-',' cz ','czech','cesky','česky',
        'czdab','czdabing','cz dabing','cz.dabing','cz+dabing'])

def _sk(n):
    nl = n.lower()
    return any(x in nl for x in ['.sk.','_sk_','-sk-',' sk ','slovak','slovensky',
        'skdab','skdabing','sk dabing','sk.dabing','sk+dabing'])

def _series_info(n):
    for p in EP_RE:
        m = p.search(n)
        if m: return True, int(m.group(1)), int(m.group(2))
    return False, None, None

def _title(n):
    n = re.sub(r'\.[a-zA-Z0-9]{2,4}$', '', n)
    ym = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', n)
    year = int(ym.group(1)) if ym else None
    parts = JUNK.split(n)
    t = parts[0] if parts else n
    t = re.sub(r'[\._\-]+', ' ', t).strip()
    t = re.sub(r'[\[\(]+\s*$', '', t).strip()
    return re.sub(r'\s+', ' ', t), year

def _show_title(clean_title):
    t = clean_title or ''
    t = re.sub(r'\s*[Ss]\d{1,2}\s*[Ee]\d{1,2}.*', '', t)
    t = re.sub(r'\s*\d{1,2}x\d{2}.*', '', t)
    return re.sub(r'[\s\.\-_]+$', '', t).strip() or clean_title

def parse_file(f):
    n = f.get('name', '')
    if not n or VIDEO_SKIP.search(n): return None
    is_s, sea, ep = _series_info(n)
    t, year = _title(n)
    show  = _show_title(t) if is_s else t
    q     = _quality(n)
    cz    = _cz(n)
    sk    = _sk(n)
    v     = int(f.get('positive_votes', 0) or 0)
    score = (float(v)
             + {'4K':40,'1080p':30,'720p':15,'480p':5}.get(q, 0)
             + (50 if cz else 0) + (20 if sk else 0))
    return {
        'ident': f['ident'], 'name': n,
        'clean_title': t, 'norm_title': _normalize(t),
        'show_title': show, 'norm_show': _normalize(show),
        'year': year, 'season': sea, 'episode': ep,
        'type': 'series' if is_s else 'movie',
        'quality': q, 'cz': cz, 'sk': sk,
        'size': int(f.get('size', 0) or 0), 'score': score,
    }

# ── Vyhledávání ───────────────────────────────────────────────────────────────
def search_page(query, token, offset=0):
    root = _call('search/', {
        'what': query, 'category': 'video',
        'sort': 'rating', 'limit': 100, 'offset': offset,
        'wst': token,
    })
    if not _ok(root): return []
    return [{'ident':          f.findtext('ident',''),
             'name':           f.findtext('name',''),
             'positive_votes': int(f.findtext('positive_votes',0) or 0),
             'size':           int(f.findtext('size',0) or 0)}
            for f in root.findall('file')]

def fetch_query(query):
    """Zpracuje jeden dotaz v samostatnem vlakne – max 3 stranky."""
    token = get_token()
    results = []
    for page in range(3):
        batch = search_page(query, token, offset=page * 100)
        if not batch: break
        results.extend(batch)
        if len(batch) < 100: break
        time.sleep(PAUSE)
    time.sleep(PAUSE)
    return query, results

# ── Dotazy ────────────────────────────────────────────────────────────────────
def build_queries():
    q = []

    # SERIALY – s01e az s20e, variace kvality a roku
    # Toto pokryje vsechny serialy bez nutnosti hledat je jmenem
    for s in range(1, 21):
        tag = f's{s:02d}e'
        q.append(tag)
        q.append(f'{tag} cz')
        q.append(f'{tag} sk')
        q.append(f'{tag} 1080p')
        q.append(f'{tag} 720p')
        q.append(f'{tag} 4k')
    # Serialy podle roku (jen s01 – rest najde vyssich sezon)
    for y in range(YEAR, 1999, -1):
        q.append(f's01e01 {y}')
        q.append(f's01e01 cz {y}')
        q.append(f's01e01 1080p {y}')

    # FILMY – rok x kvalita (pokryje 95%+ obsahu)
    for y in range(YEAR, 1999, -1):
        q += [
            f'1080p {y}', f'4k {y}', f'720p {y}',
            f'cz dabing {y}', f'czech {y}',
            f'bluray {y}', f'webrip {y}',
        ]
    # Starsi filmy bez roku
    q += [
        'cz dabing 1080p', 'cz dabing 4k', 'cz dabing bluray',
        'sk dabing 1080p', '4k bluray', 'uhd bluray',
        'bdremux', '4k remux', '1080p remux', '1080p bluray',
        'bdrip', 'dvdrip', 'dvdscr',
    ]

    # Deduplikace
    seen = set(); out = []
    for x in q:
        xs = x.strip()
        if xs and xs not in seen: seen.add(xs); out.append(xs)
    return out

def daily_queries():
    q = []
    for s in range(1, 21):
        q.append(f's{s:02d}e {YEAR}')
        q.append(f's{s:02d}e cz {YEAR}')
    for v in ['1080p','4k','720p','cz dabing','czech','bluray','webrip']:
        q.append(f'{v} {YEAR}')
        q.append(f'{v} {YEAR-1}')
    seen = set(); out = []
    for x in q:
        if x not in seen: seen.add(x); out.append(x)
    return out

# ── Deduplikace (beze změny) ──────────────────────────────────────────────────
def dedup_movies(records):
    best = {}
    for r in records:
        k = r['norm_title']
        if not k: continue
        if k not in best or r['score'] > best[k]['score']: best[k] = r
    return sorted(best.values(), key=lambda x: (-x['score'], x['clean_title'] or ''))

def dedup_series(records):
    shows = {}
    for r in records:
        k = r['norm_show']
        if not k or r['season'] is None: continue
        if k not in shows:
            shows[k] = {'show_title': r['show_title'], 'norm_show': k,
                        'year': r.get('year'), 'cz': False, 'sk': False, 'episodes': {}}
        se = (r['season'], r['episode'])
        ep_map = shows[k]['episodes']
        if se not in ep_map or r['score'] > ep_map[se]['score']: ep_map[se] = r
        if r['cz']: shows[k]['cz'] = True
        if r['sk']: shows[k]['sk'] = True
        if r.get('year') and (not shows[k]['year'] or r['year'] > shows[k]['year']):
            shows[k]['year'] = r['year']
    result = []
    for show in shows.values():
        eps = sorted(show['episodes'].values(), key=lambda x: (x['season'], x['episode']))
        result.append({
            'show_title': show['show_title'], 'norm_show': show['norm_show'],
            'year': show['year'], 'cz': show['cz'], 'sk': show['sk'],
            'ep_count': len(eps),
            'episodes': [{'ident':e['ident'],'season':e['season'],'episode':e['episode'],
                          'quality':e['quality'],'cz':e['cz'],'sk':e['sk'],'score':e['score']}
                         for e in eps],
        })
    return sorted(result, key=lambda x: x['show_title'] or '')

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    with open(path, 'w', encoding='utf-8') as f: f.write(text)
    with gzip.open(path + '.gz', 'wb') as f: f.write(text.encode('utf-8'))
    kb = os.path.getsize(path) // 1024
    n  = len(data) if isinstance(data, list) else 'meta'
    print(f'  Ulozeno: {os.path.basename(path)} ({kb} KB, {n} polozek)')

def load_checkpoint():
    if os.path.exists(CKPT_FILE):
        with open(CKPT_FILE, 'r') as f: return json.load(f)
    return {'done_queries': [], 'records': []}

def save_checkpoint(done, records):
    with open(CKPT_FILE, 'w') as f:
        json.dump({'done_queries': list(done), 'records': records}, f)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _username, _password
    _username = os.environ.get('WS_USER')
    _password = os.environ.get('WS_PASS')
    if not _username or not _password:
        print('ERROR: Nastav WS_USER a WS_PASS jako GitHub Secrets.')
        sys.exit(1)

    print(f'Prihlasovani jako {_username}...')
    test_token = do_login(_username, _password)
    _tokens[threading.get_ident()] = test_token
    print(f'Token OK: {test_token[:8]}...')

    queries  = build_queries()
    total_q  = len(queries)
    est_min  = int(total_q * PAUSE * 3 / WORKERS / 60)
    print(f'Celkem dotazu: {total_q}')
    print(f'Vlaken: {WORKERS}, pauza: {PAUSE}s')
    print(f'Odhadovany cas: {est_min}-{est_min*2} minut')
    print('─' * 60)

    ckpt = load_checkpoint()
    done_set    = set(ckpt['done_queries'])
    all_records = ckpt['records']
    seen_idents = {r['ident'] for r in all_records}

    if done_set:
        print(f'Pokracuji od checkpointu: {len(done_set)}/{total_q} hotovo, '
              f'{len(all_records)} zaznamu')

    remaining = [q for q in queries if q not in done_set]
    print(f'Zbyvajicich dotazu: {len(remaining)}')

    start_time   = time.time()
    done_count   = len(done_set)
    new_since_ck = 0
    lock         = threading.Lock()

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_query, q): q for q in remaining}

        for future in as_completed(futures):
            q, results = future.result()

            with lock:
                new_count = 0
                for r in results:
                    if r['ident'] in seen_idents: continue
                    seen_idents.add(r['ident'])
                    p = parse_file(r)
                    if p:
                        all_records.append(p)
                        new_count += 1
                done_set.add(q)
                done_count  += 1
                new_since_ck += new_count

                elapsed = time.time() - start_time
                elapsed_min = int(elapsed / 60)
                pct = int(done_count * 100 / total_q)
                eta = int((total_q - done_count) * (elapsed / max(done_count,1)) / 60)

                if done_count % 50 == 0:
                    print(f'[{pct:3d}%] {done_count}/{total_q} | ETA ~{eta}m | '
                          f'zaznamy: {len(all_records)} | dotaz: {q}')

                # Checkpoint kazde 2 minuty nebo 200 novych
                if new_since_ck >= 200 or (done_count % 100 == 0):
                    save_checkpoint(done_set, all_records)
                    movies_t = dedup_movies([r for r in all_records if r['type']=='movie'])
                    series_t = dedup_series([r for r in all_records if r['type']=='series'])
                    save_json(os.path.join(OUT_DIR,'movies.json'), movies_t)
                    save_json(os.path.join(OUT_DIR,'series.json'), series_t)
                    print(f'  → Checkpoint: {len(movies_t)} filmu, {len(series_t)} serialu')
                    new_since_ck = 0

                # Casovy limit
                if elapsed_min >= MAX_MINUTES:
                    print(f'Casovy limit ({MAX_MINUTES} min) – ukladam.')
                    ex.shutdown(wait=False, cancel_futures=True)
                    break

    # Finalni ulozeni
    print('\n=== FINALE ===')
    movies = dedup_movies([r for r in all_records if r['type']=='movie'])
    series = dedup_series([r for r in all_records if r['type']=='series'])
    save_json(os.path.join(OUT_DIR,'movies.json'), movies)
    save_json(os.path.join(OUT_DIR,'series.json'), series)
    save_json(META_FILE, {
        'updated':      datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'movies_count': len(movies),
        'series_count': len(series),
        'year': YEAR,
    })
    if os.path.exists(CKPT_FILE): os.remove(CKPT_FILE)

    elapsed_total = int(time.time() - start_time)
    print(f'\nHOTOVO za {elapsed_total//60}m {elapsed_total%60}s')
    print(f'Vysledek: {len(movies)} filmu, {len(series)} serialu')

if __name__ == '__main__':
    main()
