#!/usr/bin/env python3
"""
match_db.py – Párování Webshare DB s TMDB
- Stáhne CZ + EN názvy z TMDB
- Spáruje s Webshare záznamy přes normalizovaný název
- Výsledek: movies.json a series.json mají pole cz_title, en_title, tmdb_id

Vyžaduje: TMDB_KEY v GitHub Secrets
"""

import os, json, time, re, unicodedata, urllib.request, urllib.parse, gzip
from difflib import SequenceMatcher

TMDB_KEY  = os.environ.get('TMDB_KEY','')
DB_DIR    = os.path.join(os.path.dirname(__file__), '..', 'db')
TMDB_LANG = ['cs', 'sk', 'en']
API       = 'https://api.themoviedb.org/3'

# ── Helpers ───────────────────────────────────────────────────────────────────
def norm(s):
    if not s: return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'^\s*(the|a|an)\s+', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def similarity(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()

def tmdb_get(path, params={}):
    if not TMDB_KEY: return None
    params['api_key'] = TMDB_KEY
    url = f'{API}{path}?{urllib.parse.urlencode(params)}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'KodiAddon/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'  [TMDB] {path}: {e}')
        return None

def load_json(path):
    if not os.path.exists(path): return []
    with open(path, encoding='utf-8') as f: return json.load(f)

def save_json(path, data):
    text = json.dumps(data, ensure_ascii=False, separators=(',',':'))
    with open(path, 'w', encoding='utf-8') as f: f.write(text)
    with gzip.open(path+'.gz','wb') as f: f.write(text.encode())
    print(f'  Ulozeno: {os.path.basename(path)} ({os.path.getsize(path)//1024} KB)')

# ── TMDB vyhledávání ──────────────────────────────────────────────────────────
_tmdb_cache = {}

def tmdb_search_movie(title, year=None):
    key = f'm:{norm(title)}:{year}'
    if key in _tmdb_cache: return _tmdb_cache[key]
    params = {'query': title, 'language': 'cs'}
    if year: params['year'] = year
    data = tmdb_get('/search/movie', params)
    results = (data or {}).get('results', [])
    if not results and year:
        data = tmdb_get('/search/movie', {'query': title, 'language': 'cs'})
        results = (data or {}).get('results', [])
    result = results[0] if results else None
    _tmdb_cache[key] = result
    time.sleep(0.05)
    return result

def tmdb_search_tv(title):
    key = f't:{norm(title)}'
    if key in _tmdb_cache: return _tmdb_cache[key]
    data = tmdb_get('/search/tv', {'query': title, 'language': 'cs'})
    results = (data or {}).get('results', [])
    result = results[0] if results else None
    _tmdb_cache[key] = result
    time.sleep(0.05)
    return result

def tmdb_movie_details(tmdb_id):
    """Stáhne CZ + EN název pro film."""
    cs = tmdb_get(f'/movie/{tmdb_id}', {'language': 'cs'})
    en = tmdb_get(f'/movie/{tmdb_id}', {'language': 'en'})
    if not cs and not en: return None
    return {
        'tmdb_id':  tmdb_id,
        'cz_title': (cs or {}).get('title',''),
        'en_title': (en or {}).get('title',''),
        'overview': (cs or en or {}).get('overview',''),
        'poster':   'https://image.tmdb.org/t/p/w300' + (cs or en or {}).get('poster_path','') if (cs or en or {}).get('poster_path') else '',
    }

def tmdb_tv_details(tmdb_id):
    """Stáhne CZ + EN název pro seriál."""
    cs = tmdb_get(f'/tv/{tmdb_id}', {'language': 'cs'})
    en = tmdb_get(f'/tv/{tmdb_id}', {'language': 'en'})
    if not cs and not en: return None
    return {
        'tmdb_id':  tmdb_id,
        'cz_title': (cs or {}).get('name',''),
        'en_title': (en or {}).get('name',''),
        'overview': (cs or en or {}).get('overview',''),
        'poster':   'https://image.tmdb.org/t/p/w300' + (cs or en or {}).get('poster_path','') if (cs or en or {}).get('poster_path') else '',
    }

# ── Párování filmů ────────────────────────────────────────────────────────────
def match_movies(movies):
    print(f'\nParovani filmu ({len(movies)} zaznamu)...')
    matched = 0
    for i, m in enumerate(movies):
        if i % 100 == 0:
            print(f'  [{i}/{len(movies)}] matched: {matched}')

        if m.get('tmdb_id'): continue  # uz sparovano

        title = m.get('clean_title','')
        year  = m.get('year')
        if not title: continue

        result = tmdb_search_movie(title, year)
        if not result:
            continue

        # Zkontroluj podobnost
        ws_norm    = norm(title)
        tmdb_title = result.get('title','')
        tmdb_orig  = result.get('original_title','')
        sim = max(similarity(ws_norm, norm(tmdb_title)),
                  similarity(ws_norm, norm(tmdb_orig)))

        if sim < 0.6:
            continue  # prilis odlisne

        details = tmdb_movie_details(result['id'])
        if details:
            m.update(details)
            matched += 1

    print(f'  Hotovo: {matched}/{len(movies)} filmu sparovano')
    return movies

# ── Párování seriálů ──────────────────────────────────────────────────────────
def match_series(series):
    print(f'\nParovani serialu ({len(series)} zaznamu)...')
    matched = 0
    for i, s in enumerate(series):
        if i % 50 == 0:
            print(f'  [{i}/{len(series)}] matched: {matched}')

        if s.get('tmdb_id'): continue

        title = s.get('show_title','')
        if not title: continue

        result = tmdb_search_tv(title)
        if not result:
            continue

        ws_norm    = norm(title)
        tmdb_name  = result.get('name','')
        tmdb_orig  = result.get('original_name','')
        sim = max(similarity(ws_norm, norm(tmdb_name)),
                  similarity(ws_norm, norm(tmdb_orig)))

        if sim < 0.55:
            continue

        details = tmdb_tv_details(result['id'])
        if details:
            s.update(details)
            # Pokud mame CZ nazev a je jiny nez ws nazev, aktualizuj show_title
            cz = details.get('cz_title','')
            en = details.get('en_title','')
            if cz and norm(cz) != norm(title):
                s['cz_title']  = cz
                s['en_title']  = en or title
                # Zachovej original pro vyhledavani
                s['ws_title']  = title
            matched += 1

    print(f'  Hotovo: {matched}/{len(series)} serialu sparovano')
    return series

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not TMDB_KEY:
        print('WARN: TMDB_KEY neni nastaven – preskakuji matching.')
        print('Pridej TMDB_KEY do GitHub Secrets pro CZ/EN nazvy.')
        return

    print('=== TMDB Matching ===')
    print(f'TMDB klic: {TMDB_KEY[:6]}...')

    movies_path = os.path.join(DB_DIR, 'movies.json')
    series_path = os.path.join(DB_DIR, 'series.json')

    movies = load_json(movies_path)
    series = load_json(series_path)

    print(f'Nacteno: {len(movies)} filmu, {len(series)} serialu')

    movies = match_movies(movies)
    series = match_series(series)

    save_json(movies_path, movies)
    save_json(series_path, series)

    # Statistiky
    m_matched = sum(1 for m in movies if m.get('tmdb_id'))
    s_matched = sum(1 for s in series if s.get('tmdb_id'))
    print(f'\n=== VYSLEDEK ===')
    print(f'Filmy:  {m_matched}/{len(movies)} sparovano')
    print(f'Serialy: {s_matched}/{len(series)} sparovano')

if __name__ == '__main__':
    main()
