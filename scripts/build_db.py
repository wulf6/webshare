#!/usr/bin/env python3
"""
Webshare DB builder v2 – s checkpointem, ukládá průběžně.
Pokud se přeruší, pokračuje od místa kde skončil.
"""

import os, sys, json, time, hashlib, re, unicodedata, datetime, gzip, subprocess
import urllib.request, urllib.parse
from xml.etree import ElementTree as ET

API   = 'https://webshare.cz/api/'
YEAR  = datetime.datetime.now().year
UA    = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
OUT_DIR   = os.path.join(os.path.dirname(__file__), '..', 'db')
CKPT_FILE = os.path.join(OUT_DIR, '_checkpoint.json')

# ── HTTP ──────────────────────────────────────────────────────────────────────

def _call(endpoint, params, retries=4):
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
            with urllib.request.urlopen(req, timeout=30) as r:
                return ET.fromstring(r.read())
        except Exception as e:
            wait = 3 * (2 ** attempt)
            print(f'  [retry {attempt+1}/{retries}] {endpoint}: {e} – čekám {wait}s', flush=True)
            time.sleep(wait)
    return None

def _ok(root):
    return root is not None and root.findtext('status') == 'OK'

# ── Login ─────────────────────────────────────────────────────────────────────

ITOA64 = b'./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

def _to64(v, n):
    r = b''
    while n > 0:
        r += bytes([ITOA64[v & 0x3f]]); v >>= 6; n -= 1
    return r

def md5crypt(password, salt):
    if isinstance(password, str): password = password.encode('utf-8')
    if isinstance(salt, str):     salt     = salt.encode('utf-8')
    if salt.startswith(b'$1$'):   salt = salt[3:]
    if b'$' in salt:              salt = salt[:salt.index(b'$')]
    salt = salt[:8]
    ctx  = hashlib.md5(password + b'$1$' + salt)
    ctx2 = hashlib.md5(password + salt + password)
    final = ctx2.digest()
    i = len(password)
    while i > 0: ctx.update(final[:min(i, 16)]); i -= 16
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
    r += _to64((final[0]<<16)|(final[6]<<8)|final[12], 4)
    r += _to64((final[1]<<16)|(final[7]<<8)|final[13], 4)
    r += _to64((final[2]<<16)|(final[8]<<8)|final[14], 4)
    r += _to64((final[3]<<16)|(final[9]<<8)|final[15], 4)
    r += _to64((final[4]<<16)|(final[10]<<8)|final[5], 4)
    r += _to64(final[11], 2)
    return r

def login(username, password):
    root = _call('salt/', {'username_or_email': username, 'wst': ''})
    if not _ok(root): raise RuntimeError('Nelze získat salt')
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

# ── Parsování ─────────────────────────────────────────────────────────────────

VIDEO_SKIP = re.compile(r'\.(nfo|txt|srt|sub|ass|ssa|idx|jpg|png|zip|rar|7z|exe|pdf|doc|docx)$', re.I)
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
    if any(x in nl for x in ['1080p','1080i','fullhd','fhd']):         return '1080p'
    if '720p' in nl: return '720p'
    if any(x in nl for x in ['480p','576p','dvdrip','dvdscr','dvd','vhsrip']): return 'SD'
    return ''

def _cz(n):
    nl = n.lower()
    return any(x in nl for x in ['.cz.','-cz-','_cz_',' cz ','czech','cesky','czdab','czdabing','cz dabing','cz.dabing','cz+dabing'])

def _sk(n):
    nl = n.lower()
    return any(x in nl for x in ['.sk.','-sk-','_sk_',' sk ','slovak','slovensky','skdab','skdabing','sk dabing','sk.dabing','sk+dabing'])

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
    t = re.sub(r'(\s+\d{1,2}){2,}\s*$', '', t).strip()
    return re.sub(r'\s+', ' ', t), year

def _show_title(ct):
    t = ct or ''
    t = re.sub(r'\s*[Ss]\d{1,2}\s*[Ee]\d{1,2}.*', '', t)
    t = re.sub(r'\s*\d{1,2}x\d{2}.*', '', t)
    return re.sub(r'[\s\.\-_]+$', '', t).strip() or ct

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
    size  = int(f.get('size', 0) or 0)
    score = (float(v)
             + {'4K':40,'1080p':30,'720p':15,'SD':2}.get(q,0)
             + (50 if cz else 0) + (20 if sk else 0))
    return {
        'ident': f['ident'], 'name': n,
        'clean_title': t, 'norm_title': _normalize(t),
        'show_title': show, 'norm_show': _normalize(show),
        'year': year, 'season': sea, 'episode': ep,
        'type': 'series' if is_s else 'movie',
        'quality': q, 'cz': cz, 'sk': sk,
        'size': size, 'score': score,
    }

# ── Vyhledávání ───────────────────────────────────────────────────────────────

def search_page(query, token, limit=100, offset=0):
    root = _call('search/', {
        'what': query, 'category': 'video',
        'sort': 'rating', 'limit': limit, 'offset': offset,
        'wst': token,
    })
    if not _ok(root): return []
    return [{'ident': f.findtext('ident',''), 'name': f.findtext('name',''),
             'positive_votes': int(f.findtext('positive_votes',0) or 0),
             'size': int(f.findtext('size',0) or 0)}
            for f in root.findall('file')]

def fetch_all_pages(query, token, max_pages=5):
    results = []
    for page in range(max_pages):
        batch = search_page(query, token, limit=100, offset=page*100)
        if not batch: break
        results.extend(batch)
        if len(batch) < 100: break
        time.sleep(0.3)
    return results

# ── Dotazy ────────────────────────────────────────────────────────────────────

def build_queries():
    q = []

    # SERIÁLY – každá sezóna × každý rok × cz/sk
    for s in range(1, 21):
        tag = f's{s:02d}e'
        for y in range(YEAR, 2007, -1):
            q.append(f'{tag} {y}')
            q.append(f'{tag} cz {y}')
            q.append(f'{tag} sk {y}')
        q.append(tag)
        q.append(f'{tag} cz')
        q.append(f'{tag} sk')

    # FILMY – rok × kvalita
    q4k = ['4k','2160p','uhd','ultrahd','4k bluray','uhd bluray','bdremux',
            'uhd remux','4k remux','4k hdr','4k hevc','4k x265','4k web-dl']
    qhd = ['1080p','fullhd','1080p bluray','1080p remux','1080p hevc',
            '1080p x265','1080p web-dl','1080p webrip','1080p hdr']
    qlo = ['720p','720p bluray','720p web-dl','720p hevc']
    qsd = ['bluray','bdrip','dvdrip','webrip','web-dl','hdtv','xvid','divx']

    for y in range(YEAR, YEAR-6, -1):
        for qk in q4k + qhd: q.append(f'{qk} {y}')
        for qk in qlo + qsd: q.append(f'{qk} {y}')
        q += [f'cz dabing {y}', f'cz dabing 1080p {y}', f'cz dabing 4k {y}',
              f'sk dabing {y}', f'czech {y}', f'slovak {y}', f'{y}']
    for y in range(YEAR-6, 1979, -1):
        q += [f'{y}', f'1080p {y}', f'cz dabing {y}', f'bluray {y}', f'czech {y}']

    # Obecné dotazy
    q += ['cz dabing 1080p','cz dabing 4k','sk dabing 1080p','4k remux',
          'bdremux','uhd remux','1080p remux','1080p bluray','remux','bdrip']

    # Populární seriály – přímé názvy × každá sezóna
    SHOWS = [
        'simpsonovi','the simpsons','futurama','rick and morty','family guy',
        'south park','american dad','bobs burgers','archer','bojack horseman',
        'game of thrones','breaking bad','better call saul','the walking dead',
        'stranger things','dark','the witcher','zaklinar','mandalorian',
        'westworld','black mirror','lost','battlestar galactica','the expanse',
        'fringe','heroes','supernatural','vikings','the last kingdom',
        'house of dragon','rod draku','rings of power','the boys','invincible',
        'daredevil','prison break','la casa de papel','money heist','narcos',
        'squid game','the last of us','fallout','euphoria','succession','andor',
        'pratele','friends','how i met your mother','the office','seinfeld',
        'brooklyn nine-nine','new girl','two and a half men','big bang theory',
        'teorie velkeho tresku','scrubs','community','modern family','fleabag',
        'ted lasso','true detective','mindhunter','dexter','the wire','sopranos',
        'boardwalk empire','peaky blinders','ozark','yellowstone','sherlock',
        'mentalist','monk','columbo','bones','castle','criminal minds','csi',
        'law and order','ncis','fargo','true blood',
        'naruto','one piece','dragon ball','attack on titan','demon slayer',
        'jujutsu kaisen','death note','fullmetal alchemist','sword art online',
        'my hero academia','hunter x hunter','bleach','fairy tail','one punch man',
        'chainsaw man','spy x family','overlord','code geass','cowboy bebop',
        'all of us are dead','sweet home','moving',
        'ulice','ordinace v ruzove zahrade','comeback','krejzovi',
        'dirilis ertugrul','kurulus osman',
        'greys anatomy','er','pokemon','cobra kai','wednesday','you','elite',
        'bridgerton','emily in paris','outer banks','ginny and georgia',
    ]
    LONG = {'simpsonovi','the simpsons','law and order','ncis','criminal minds',
            'csi','supernatural','greys anatomy','er','bones','castle','monk',
            'columbo','one piece','naruto','bleach','fairy tail','dragon ball','pokemon'}

    for show in SHOWS:
        q.append(show)
        q.append(f'{show} cz')
        max_s = 40 if show in LONG else 15
        for s in range(1, max_s+1):
            q.append(f'{show} s{s:02d}')
            q.append(f'{show} s{s:02d} cz')

    # Deduplikace
    seen = set(); out = []
    for x in q:
        xs = x.strip()
        if xs and xs not in seen:
            seen.add(xs); out.append(xs)
    return out

# ── Deduplikace ───────────────────────────────────────────────────────────────

def dedup_movies(records):
    best = {}
    for r in records:
        k = r['norm_title']
        if not k: continue
        # Filtr: min 100MB nebo neznama velikost
        if r.get('size', 0) > 0 and r['size'] < 104857600: continue
        if k not in best or r['score'] > best[k]['score']:
            best[k] = r
    return sorted(best.values(), key=lambda x: (-x['score'], x.get('clean_title') or ''))

def dedup_series(records):
    shows = {}
    for r in records:
        k = r['norm_show']
        if not k or r['season'] is None: continue
        if k not in shows:
            shows[k] = {'show_title': r['show_title'], 'norm_show': k,
                        'year': r.get('year'), 'cz': False, 'sk': False, 'episodes': {}}
        se = (r['season'], r['episode'])
        ep = shows[k]['episodes']
        if se not in ep or r['score'] > ep[se]['score']:
            ep[se] = r
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
            'episodes': [{'ident': e['ident'], 'season': e['season'],
                          'episode': e['episode'], 'quality': e['quality'],
                          'cz': e['cz'], 'sk': e['sk'], 'score': e['score']}
                         for e in eps],
        })
    return sorted(result, key=lambda x: x.get('show_title') or '')

# ── Uložení ───────────────────────────────────────────────────────────────────

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    with open(path, 'w', encoding='utf-8') as f: f.write(text)
    with gzip.open(path + '.gz', 'wb') as gz: gz.write(text.encode('utf-8'))
    kb = os.path.getsize(path) // 1024
    print(f'  Uloženo: {os.path.basename(path)} ({kb} KB, {len(data)} položek)', flush=True)

def git_push(msg):
    try:
        subprocess.run(['git','config','user.name','github-actions[bot]'], check=True)
        subprocess.run(['git','config','user.email','github-actions[bot]@users.noreply.github.com'], check=True)
        subprocess.run(['git','add','db/'], check=True)
        result = subprocess.run(['git','diff','--staged','--quiet'])
        if result.returncode != 0:
            subprocess.run(['git','commit','-m', msg], check=True)
            subprocess.run(['git','push'], check=True)
            print(f'  Git push OK: {msg}', flush=True)
    except Exception as e:
        print(f'  Git push selhal: {e}', flush=True)

# ── Checkpoint ────────────────────────────────────────────────────────────────

def load_ckpt():
    if os.path.exists(CKPT_FILE):
        with open(CKPT_FILE) as f: return json.load(f)
    return {'done': [], 'records': []}

def save_ckpt(done, records):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CKPT_FILE, 'w') as f:
        json.dump({'done': done, 'records': records}, f)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    username = os.environ.get('WS_USER')
    password = os.environ.get('WS_PASS')
    if not username or not password:
        print('ERROR: Nastav WS_USER a WS_PASS jako GitHub Secrets.')
        sys.exit(1)

    print(f'Přihlašování jako {username}...', flush=True)
    token = login(username, password)
    print(f'Token OK: {token[:8]}...', flush=True)

    queries = build_queries()
    total_q = len(queries)
    print(f'Celkem dotazů: {total_q}', flush=True)
    print(f'Odhadovaný čas: ~{total_q*0.8/60:.0f} minut\n', flush=True)

    ckpt = load_ckpt()
    done_set = set(ckpt['done'])
    all_records = ckpt['records']
    seen_idents = {r['ident'] for r in all_records}

    if done_set:
        print(f'Checkpoint: {len(done_set)}/{total_q} dotazů hotovo, {len(all_records)} záznamů', flush=True)

    start = time.time()

    for i, q in enumerate(queries):
        if q in done_set:
            continue

        elapsed = int(time.time() - start)
        pct = int(i * 100 / total_q)
        remaining = total_q - i
        eta = int((elapsed / max(i - len(done_set) + 1, 1)) * remaining)
        print(f'[{pct:3d}%] {i+1}/{total_q} | ETA ~{eta//60}m | zázn: {len(all_records)} | {q}', flush=True)

        results = fetch_all_pages(q, token, max_pages=5)
        new = 0
        for r in results:
            if r['ident'] in seen_idents: continue
            seen_idents.add(r['ident'])
            p = parse_file(r)
            if p:
                all_records.append(p)
                new += 1
        if new:
            print(f'  -> {new} nových (celkem {len(all_records)})', flush=True)

        done_set.add(q)

        # Checkpoint + push každých 30 dotazů
        if (i + 1) % 30 == 0:
            save_ckpt(list(done_set), all_records)
            movies_tmp = dedup_movies([r for r in all_records if r['type'] == 'movie'])
            series_tmp = dedup_series([r for r in all_records if r['type'] == 'series'])
            save_json(os.path.join(OUT_DIR, 'movies.json'), movies_tmp)
            save_json(os.path.join(OUT_DIR, 'series.json'), series_tmp)
            meta = {'updated': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'movies_count': len(movies_tmp), 'series_count': len(series_tmp), 'year': YEAR}
            save_json(os.path.join(OUT_DIR, 'meta.json'), meta)
            git_push(f'DB checkpoint {i+1}/{total_q} – {len(movies_tmp)}f {len(series_tmp)}s')
            print(f'  -> Checkpoint: {len(movies_tmp)} filmů, {len(series_tmp)} seriálů', flush=True)

        # Pauzy
        if (i + 1) % 100 == 0:
            print('  *** Přestávka 5s ***', flush=True)
            time.sleep(5)
        else:
            time.sleep(0.5)

    # Finální
    print('\n=== FINÁLNÍ ZPRACOVÁNÍ ===', flush=True)
    movies = dedup_movies([r for r in all_records if r['type'] == 'movie'])
    series = dedup_series([r for r in all_records if r['type'] == 'series'])
    save_json(os.path.join(OUT_DIR, 'movies.json'), movies)
    save_json(os.path.join(OUT_DIR, 'series.json'), series)
    meta = {'updated': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'movies_count': len(movies), 'series_count': len(series), 'year': YEAR}
    save_json(os.path.join(OUT_DIR, 'meta.json'), meta)

    if os.path.exists(CKPT_FILE):
        os.remove(CKPT_FILE)

    elapsed = int(time.time() - start)
    print(f'\nHOTOVO za {elapsed//60}m {elapsed%60}s', flush=True)
    print(f'Výsledek: {len(movies)} filmů, {len(series)} seriálů', flush=True)

if __name__ == '__main__':
    main()
