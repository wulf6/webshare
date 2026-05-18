#!/usr/bin/env python3
"""
Webshare DB builder – běží jako GitHub Action.
Pomalý ale kompletní – cíl: celá databáze, limit 6 hodin.
Pauzy mezi dotazy aby se nezablokoval přístup.
"""

import os, sys, json, time, hashlib, re, unicodedata, datetime, gzip
import urllib.request, urllib.parse
from xml.etree import ElementTree as ET

API   = 'https://webshare.cz/api/'
YEAR  = datetime.datetime.now().year
UA    = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

OUT_DIR   = os.path.join(os.path.dirname(__file__), '..', 'db')
META_FILE = os.path.join(OUT_DIR, 'meta.json')

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
            print(f'  [retry {attempt+1}/{retries}] {endpoint}: {e} – čekám {wait}s')
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
    r += _to64((final[0] << 16) | (final[6] << 8)  | final[12], 4)
    r += _to64((final[1] << 16) | (final[7] << 8)  | final[13], 4)
    r += _to64((final[2] << 16) | (final[8] << 8)  | final[14], 4)
    r += _to64((final[3] << 16) | (final[9] << 8)  | final[15], 4)
    r += _to64((final[4] << 16) | (final[10] << 8) | final[5],  4)
    r += _to64(final[11], 2)
    return r

def login(username, password):
    root = _call('salt/', {'username_or_email': username, 'wst': ''})
    if not _ok(root):
        raise RuntimeError('Nelze získat salt')
    salt     = root.findtext('salt', '')
    enc      = md5crypt(password, salt)
    pwd_hash = hashlib.sha1(enc).hexdigest()
    digest   = hashlib.md5((username + ':Webshare:' + password).encode()).hexdigest()
    root = _call('login/', {
        'username_or_email': username,
        'password': pwd_hash,
        'digest':   digest,
        'keep_logged_in': 1,
        'wst': '',
    })
    if not _ok(root):
        msg = root.findtext('message', '?') if root else 'timeout'
        raise RuntimeError(f'Login selhal: {msg}')
    return root.findtext('token')

# ── Parsování ─────────────────────────────────────────────────────────────────

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
    if any(x in nl for x in [
        '2160p', '4k', 'uhd', 'bdremux', 'ultrahd', '4kuhd',
    ]): return '4K'
    if any(x in nl for x in [
        '1080p', '1080i', 'fullhd', 'fhd',
    ]): return '1080p'
    if any(x in nl for x in [
        '720p',
    ]): return '720p'
    if any(x in nl for x in [
        '480p', '576p', '360p', '240p',
        'dvdrip', 'dvdscr', 'dvd', 'vhsrip', 'cam', 'ts',
    ]): return 'SD'
    return ''

def _cz(n):
    nl = n.lower()
    return any(x in nl for x in [
        '.cz.', '_cz_', '-cz-', ' cz ', 'czech', 'cesky', 'česky',
        'czdab', 'czdabing', 'cz dabing', 'cz.dabing', 'cz+dabing'])

def _sk(n):
    nl = n.lower()
    return any(x in nl for x in [
        '.sk.', '_sk_', '-sk-', ' sk ', 'slovak', 'slovensky',
        'skdab', 'skdabing', 'sk dabing', 'sk.dabing', 'sk+dabing'])

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
             + {'4K': 40, '1080p': 30, '720p': 15, '480p': 5}.get(q, 0)
             + (50 if cz else 0)
             + (20 if sk else 0))
    return {
        'ident':       f['ident'],
        'name':        n,
        'clean_title': t,
        'norm_title':  _normalize(t),
        'show_title':  show,
        'norm_show':   _normalize(show),
        'year':        year,
        'season':      sea,
        'episode':     ep,
        'type':        'series' if is_s else 'movie',
        'quality':     q,
        'cz':          cz,
        'sk':          sk,
        'size':        int(f.get('size', 0) or 0),
        'score':       score,
    }

# ── Vyhledávání ───────────────────────────────────────────────────────────────

def search_page(query, token, limit=100, offset=0):
    root = _call('search/', {
        'what': query, 'category': 'video',
        'sort': 'rating', 'limit': limit, 'offset': offset,
        'wst': token,
    })
    if not _ok(root): return []
    return [{'ident':          f.findtext('ident', ''),
             'name':           f.findtext('name', ''),
             'positive_votes': int(f.findtext('positive_votes', 0) or 0),
             'size':           int(f.findtext('size', 0) or 0)}
            for f in root.findall('file')]

def fetch_all_pages(query, token, max_pages=10, pause=0.3):
    """Stáhne všechny stránky výsledků pro jeden dotaz."""
    results = []
    for page in range(max_pages):
        batch = search_page(query, token, limit=100, offset=page * 100)
        if not batch: break
        results.extend(batch)
        if len(batch) < 100: break
        time.sleep(pause)
    return results

# ── Generování dotazů ────────────────────────────────────────────────────────

def build_queries():
    """Kompletní sada dotazů pro celou databázi."""
    q = []

    # SERIÁLY – sezóny 1–20, s CZ/SK variantami
    for s in range(1, 21):
        tag = f's{s:02d}e'
        q.append(tag)
        q.append(f'{tag} cz')
        q.append(f'{tag} sk')
        q.append(f'{tag} 1080p')
        q.append(f'{tag} 720p')
        # Konkrétní roky pro novější sezóny
        for y in range(YEAR, YEAR - 5, -1):
            q.append(f's{s:02d} {y}')

    # Seriály podle roku + s01e01
    for y in range(YEAR, 1999, -1):
        q.append(f's01e01 {y}')
        q.append(f's01e01 cz {y}')
        q.append(f's01e01 1080p {y}')

    # FILMY – kombinace roku × kvality × jazyka
    qualities_4k  = [
        '4k', '2160p', 'uhd', 'ultrahd', '4k bluray', 'uhd bluray',
        'bdremux', 'uhd remux', '4k remux', '2160p remux',
        '4k hdr', '2160p hdr', '4k hdr10', '4k dv', '4k dolby',
        '4k hevc', '4k x265', '4k h265', '2160p hevc', '2160p x265',
        '4k web-dl', '4k webrip', '2160p web-dl', '2160p webrip',
        '2160p amzn', '2160p nf', '2160p dsnp',
    ]
    qualities_hd  = [
        '1080p', '1080i', 'fullhd', 'fhd',
        '1080p bluray', '1080p remux', '1080p bdremux',
        '1080p hevc', '1080p x265', '1080p h265',
        '1080p x264', '1080p h264',
        '1080p web-dl', '1080p webrip', '1080p amzn',
        '1080p nf', '1080p dsnp', '1080p hmax',
        '1080p hdr', '1080p hdr10',
    ]
    qualities_hd_low = [
        '720p', '720p bluray', '720p web-dl', '720p webrip',
        '720p hevc', '720p x265', '720p x264', '720p hdrip', '720p bdrip',
    ]
    qualities_std = [
        'bluray', 'bdrip', 'dvdrip', 'dvdscr', 'dvd',
        'webrip', 'web-dl', 'hdtv', 'pdtv', 'hdrip',
        'xvid', 'divx', 'vhsrip', 'vodrip',
    ]

    # Novější roky – více kombinací
    for y in range(YEAR, YEAR - 6, -1):
        for qk in qualities_4k + qualities_hd:
            q.append(f'{qk} {y}')
        for qk in qualities_hd_low + qualities_std:
            q.append(f'{qk} {y}')
        q.append(f'cz dabing {y}')
        q.append(f'cz dabing 1080p {y}')
        q.append(f'cz dabing 4k {y}')
        q.append(f'cz dabing 720p {y}')
        q.append(f'sk dabing {y}')
        q.append(f'czech {y}')
        q.append(f'slovak {y}')
        q.append(f'cz titulky {y}')
        q.append(f'{y}')

    # Starší roky – jen základní dotazy
    for y in range(YEAR - 6, 1979, -1):
        q.append(f'{y}')
        q.append(f'1080p {y}')
        q.append(f'cz dabing {y}')
        q.append(f'bluray {y}')
        q.append(f'czech {y}')

    # Obecné dotazy bez roku
    q += [
        'cz dabing 1080p', 'cz dabing 4k', 'cz dabing 720p',
        'sk dabing 1080p', 'sk dabing 4k', 'sk dabing 720p',
        'cz dabing bluray', 'cz titulky 1080p',
        '4k bluray cz', 'uhd bluray cz', '4k remux', 'bdremux',
        'uhd remux', '4k hdr', '4k hevc', '2160p hevc', 'uhd bluray',
        '4k web-dl', '4k dv', '1080p remux', '1080p bluray',
        'hevc 1080p', 'x265 1080p', 'h265 1080p', '1080p hdr',
        '720p bluray', '720p web-dl', '720p hevc', '720p x265',
        'remux', 'bluray remux', 'bdrip', 'dvdrip', 'dvdscr',
        'xvid cz', 'divx cz', 'bluray cz', 'webrip cz', 'hdtv cz',
    ]

    # Přímé názvy populárních seriálů – CZ i EN
    POPULAR_SERIES = [
        # Animované
        'simpsonovi', 'the simpsons',
        'futurama', 'rick and morty',
        'family guy', 'american dad',
        'south park', 'avatar',
        'beavis and butt-head',
        'bobs burgers', 'archer',
        'bojack horseman', 'disenchantment',
        'final space', 'big mouth',

        # Akční / Sci-Fi / Fantasy
        'hra o truny', 'game of thrones',
        'breaking bad', 'better call saul',
        'the walking dead', 'fear the walking dead',
        'stranger things', 'dark',
        'the witcher', 'zaklinar',
        'mandalorian', 'star wars',
        'westworld', 'altered carbon',
        'black mirror', 'lost',
        'battlestar galactica',
        'the expanse', 'firefly',
        'fringe', 'x-files', 'akta x',
        'heroes', 'supernatural',
        'vikings', 'the last kingdom',
        'house of dragon', 'rod draku',
        'rings of power', 'wheel of time',
        'the boys', 'invincible',
        'loki', 'wandavision', 'hawkeye',
        'daredevil', 'jessica jones',
        'prison break', '24',
        'la casa de papel', 'papirovy dum',
        'money heist', 'narcos',
        'squid game', 'hra na olihen',
        'kingdom', 'dark tourist',
        'severance', 'yellowjackets',
        'the last of us', 'fallout',
        'euphoria', 'succession',
        'andor', 'obi-wan',

        # Komedie / Drama
        'pratele', 'friends',
        'how i met your mother',
        'the office', 'parks and recreation',
        'seinfeld', 'it crowd',
        'brooklyn nine-nine', 'brooklyn 99',
        'new girl', 'two and a half men',
        'big bang theory', 'teorie velkeho tresku',
        'scrubs', 'community',
        'arrested development',
        'curb your enthusiasm',
        'modern family', 'schitts creek',
        'fleabag', 'ted lasso',
        'abbott elementary', 'what we do in the shadows',

        # Krimi / Thriller
        'true detective', 'mindhunter',
        'dexter', 'the wire',
        'sopranos', 'boardwalk empire',
        'peaky blinders', 'ozark',
        'yellowstone', 'justified',
        'sherlock', 'elementary',
        'mentalist', 'monk',
        'columbo', 'psych',
        'bones', 'castle',
        'criminal minds', 'csi',
        'law and order', 'ncis',
        'fargo', 'true blood',

        # Reality / Dokumenty
        'formula 1 drive to survive',
        'planet earth', 'blue planet',
        'our planet', 'attenborough',

        # České a slovenské
        'ulice', 'ordinace v ruzove zahrade',
        'krejzovi', 'comeback',
        'vypravi', 'dekalog',
        'hordubalovi', 'pan tajemnik',
        'lajna', 'most',
        'rtvs', 'ceska televize',
        'prima cool', 'nova cinema',

        # Japonské / Anime
        'naruto', 'one piece',
        'dragon ball', 'attack on titan',
        'demon slayer', 'jujutsu kaisen',
        'death note', 'fullmetal alchemist',
        'sword art online', 'my hero academia',
        'hunter x hunter', 'bleach',
        'fairy tail', 'one punch man',
        'vinland saga', 'chainsaw man',
        'spy x family', 'tokyo revengers',
        'overlord', 'shield hero',
        'black clover', 're zero',
        'steins gate', 'code geass',
        'cowboy bebop', 'neon genesis evangelion',

        # Korejské
        'all of us are dead',
        'sweet home', 'hellbound',
        'moving', 'mask girl',

        # Turecké
        'dirilis ertugrul', 'kurulus osman',
        'kara para ask', 'fatih harbiye',
    ]

    # Dlouhé seriály (35+ sezón) – hledáme každou sezónu zvlášť
    LONG_SERIES = [
        'simpsonovi', 'the simpsons',
        'law and order', 'ncis', 'criminal minds',
        'csi', 'supernatural', 'greys anatomy',
        'er', 'bones', 'castle', 'monk', 'columbo',
        'one piece', 'naruto', 'bleach', 'fairy tail',
        'dragon ball', 'pokemon',
    ]
    LONG_SERIES = [s.replace("'", "") for s in LONG_SERIES]

    for show in POPULAR_SERIES:
        q.append(show)
        q.append(f'{show} cz')
        q.append(f'{show} 1080p')
        q.append(f'{show} 720p')
        max_seasons = 40 if show in LONG_SERIES else 15
        for s in range(1, max_seasons + 1):
            stag = f's{s:02d}'
            q.append(f'{show} {stag}')
            q.append(f'{show} {stag} cz')

    # Deduplikace při zachování pořadí
    seen = set()
    out = []
    for x in q:
        xs = x.strip()
        if xs and xs not in seen:
            seen.add(xs)
            out.append(xs)
    return out

# ── Deduplikace ───────────────────────────────────────────────────────────────

def dedup_movies(records):
    best = {}
    for r in records:
        k = r['norm_title']
        if not k: continue
        if k not in best or r['score'] > best[k]['score']:
            best[k] = r
    return sorted(best.values(), key=lambda x: (-x['score'], x['clean_title'] or ''))

def dedup_series(records):
    shows = {}
    for r in records:
        k = r['norm_show']
        if not k or r['season'] is None: continue
        if k not in shows:
            shows[k] = {
                'show_title': r['show_title'],
                'norm_show':  k,
                'year':       r.get('year'),
                'cz': False, 'sk': False,
                'episodes': {}
            }
        se = (r['season'], r['episode'])
        ep_map = shows[k]['episodes']
        if se not in ep_map or r['score'] > ep_map[se]['score']:
            ep_map[se] = r
        if r['cz']: shows[k]['cz'] = True
        if r['sk']: shows[k]['sk'] = True
        # Nejnovější rok
        if r.get('year') and (not shows[k]['year'] or r['year'] > shows[k]['year']):
            shows[k]['year'] = r['year']
    result = []
    for show in shows.values():
        eps = sorted(show['episodes'].values(),
                     key=lambda x: (x['season'], x['episode']))
        result.append({
            'show_title': show['show_title'],
            'norm_show':  show['norm_show'],
            'year':       show['year'],
            'cz':         show['cz'],
            'sk':         show['sk'],
            'ep_count':   len(eps),
            'episodes': [{
                'ident':   e['ident'],
                'season':  e['season'],
                'episode': e['episode'],
                'quality': e['quality'],
                'cz':      e['cz'],
                'sk':      e['sk'],
                'score':   e['score'],
            } for e in eps],
        })
    return sorted(result, key=lambda x: x['show_title'] or '')

# ── Uložení ───────────────────────────────────────────────────────────────────

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    with gzip.open(path + '.gz', 'wb') as f:
        f.write(text.encode('utf-8'))
    kb = os.path.getsize(path) // 1024
    n  = len(data) if isinstance(data, list) else 'meta'
    print(f'  Uloženo: {os.path.basename(path)} ({kb} KB, {n} položek)')

# ── Checkpoint (pokračování po přerušení) ─────────────────────────────────────

def load_checkpoint(path):
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {'done_queries': [], 'records': []}

def save_checkpoint(path, done, records):
    with open(path, 'w') as f:
        json.dump({'done_queries': done, 'records': records}, f)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    username = os.environ.get('WS_USER')
    password = os.environ.get('WS_PASS')
    if not username or not password:
        print('ERROR: Nastav WS_USER a WS_PASS jako GitHub Secrets.')
        sys.exit(1)

    print(f'Přihlašování jako {username}...')
    token = login(username, password)
    print(f'Token OK: {token[:8]}...')

    queries = build_queries()
    total_q = len(queries)
    print(f'\nCelkem dotazů: {total_q}')
    print(f'Odhadovaný čas: {total_q * 1.5 / 60:.0f}–{total_q * 3 / 60:.0f} minut\n')

    ckpt_path = os.path.join(OUT_DIR, '_checkpoint.json')
    ckpt = load_checkpoint(ckpt_path)
    done_set = set(ckpt['done_queries'])
    all_records = ckpt['records']

    if done_set:
        print(f'Pokračuji od checkpointu: {len(done_set)}/{total_q} dotazů hotovo, '
              f'{len(all_records)} záznamů načteno')

    seen_idents = {r['ident'] for r in all_records}
    start_time  = time.time()

    for i, q in enumerate(queries):
        if q in done_set:
            continue

        elapsed  = time.time() - start_time
        pct      = int(i * 100 / total_q)
        eta_s    = (elapsed / max(i - len(done_set), 1)) * (total_q - i) if i > len(done_set) else 0
        eta_m    = int(eta_s / 60)

        print(f'[{pct:3d}%] {i+1}/{total_q} | ETA ~{eta_m}m | záznamy: {len(all_records)} | dotaz: {q}')

        results = fetch_all_pages(q, token, max_pages=5, pause=0.3)
        new_count = 0
        for r in results:
            if r['ident'] in seen_idents:
                continue
            seen_idents.add(r['ident'])
            p = parse_file(r)
            if p:
                all_records.append(p)
                new_count += 1

        done_set.add(q)

        # Checkpoint každých 20 dotazů
        if (i + 1) % 20 == 0:
            save_checkpoint(ckpt_path, list(done_set), all_records)
            movies_tmp = dedup_movies([r for r in all_records if r['type'] == 'movie'])
            series_tmp = dedup_series([r for r in all_records if r['type'] == 'series'])
            save_json(os.path.join(OUT_DIR, 'movies.json'), movies_tmp)
            save_json(os.path.join(OUT_DIR, 'series.json'), series_tmp)
            print(f'  → Checkpoint: {len(movies_tmp)} filmů, {len(series_tmp)} seriálů')

        # Pauza mezi dotazy – 1.5s základní, každých 50 dotazů 10s přestávka
        if (i + 1) % 50 == 0:
            print(f'  *** Přestávka 10s ***')
            time.sleep(3)
        else:
            time.sleep(0.5)

    # Finální uložení
    print('\n=== FINÁLNÍ ZPRACOVÁNÍ ===')
    movies = dedup_movies([r for r in all_records if r['type'] == 'movie'])
    series = dedup_series([r for r in all_records if r['type'] == 'series'])

    save_json(os.path.join(OUT_DIR, 'movies.json'), movies)
    save_json(os.path.join(OUT_DIR, 'series.json'), series)

    meta = {
        'updated':      datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'movies_count': len(movies),
        'series_count': len(series),
        'year':         YEAR,
    }
    save_json(META_FILE, meta)

    # Smaž checkpoint po úspěšném dokončení
    if os.path.exists(ckpt_path):
        os.remove(ckpt_path)

    elapsed_total = int(time.time() - start_time)
    print(f'\nHOTOVO za {elapsed_total // 60}m {elapsed_total % 60}s')
    print(f'Výsledek: {len(movies)} filmů, {len(series)} seriálů')

if __name__ == '__main__':
    main()
