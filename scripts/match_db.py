#!/usr/bin/env python3
"""
match_db.py v2 – TMDB matching s Bearer tokenem (v4 API)
CZ název pokud existuje, jinak EN název
"""
import os, json, time, re, unicodedata, urllib.request, urllib.parse, gzip
from difflib import SequenceMatcher

TMDB_KEY = os.environ.get('TMDB_KEY', '')
DB_DIR   = os.path.join(os.path.dirname(__file__), '..', 'db')
API      = 'https://api.themoviedb.org/3'
IMG      = 'https://image.tmdb.org/t/p/w300'

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
    url = f'{API}{path}?{urllib.parse.urlencode(params)}'
    try:
        req = urllib.request.Request(url, headers={
            'Authorization': f'Bearer {TMDB_KEY}',
            'Accept': 'application/json',
            'User-Agent': 'KodiAddon/5.0',
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'  [TMDB] {path}: {e}')
        return None

def best_title(cs_title, en_title, ws_title):
    """
    Vrátí (display_title, alt_title).
    Preferuje CZ název pokud existuje a je smysluplný.
    """
    cs = (cs_title or '').strip()
    en = (en_title or '').strip()
    if cs and cs.lower() != en.lower():
        return cs, en   # mame CZ preklad → zobraz CZ, alternativa EN
    return en, ''       # jen EN nazev

def search_movie(title, year=None):
    params = {'query': title, 'language': 'cs'}
    if year: params['primary_release_year'] = year
    data = tmdb_get('/search/movie', params)
    results = (data or {}).get('results', [])
    if not results and year:
        data = tmdb_get('/search/movie', {'query': title, 'language': 'cs'})
        results = (data or {}).get('results', [])
    return results[0] if results else None

def search_tv(title):
    data = tmdb_get('/search/tv', {'query': title, 'language': 'cs'})
    results = (data or {}).get('results', [])
    return results[0] if results else None

def movie_details(tmdb_id):
    cs = tmdb_get(f'/movie/{tmdb_id}', {'language': 'cs'}) or {}
    en = tmdb_get(f'/movie/{tmdb_id}', {'language': 'en'}) or {}
    cs_title = cs.get('title', '')
    en_title = en.get('title', '') or cs.get('original_title', '')
    display, alt = best_title(cs_title, en_title, '')
    return {
        'tmdb_id':       tmdb_id,
        'display_title': display,   # co se zobrazí v Kodi
        'alt_title':     alt,       # alternativní název
        'cz_title':      cs_title,
        'en_title':      en_title,
        'overview':      cs.get('overview','') or en.get('overview',''),
        'poster':        IMG + cs.get('poster_path','') if cs.get('poster_path') else
                         (IMG + en.get('poster_path','') if en.get('poster_path') else ''),
        'genres':        [g['name'] for g in (cs.get('genres') or [])],
        'vote_average':  cs.get('vote_average', 0),
    }

def tv_details(tmdb_id):
    cs = tmdb_get(f'/tv/{tmdb_id}', {'language': 'cs'}) or {}
    en = tmdb_get(f'/tv/{tmdb_id}', {'language': 'en'}) or {}
    cs_title = cs.get('name', '')
    en_title = en.get('name', '') or cs.get('original_name', '')
    display, alt = best_title(cs_title, en_title, '')
    return {
        'tmdb_id':       tmdb_id,
        'display_title': display,
        'alt_title':     alt,
        'cz_title':      cs_title,
        'en_title':      en_title,
        'overview':      cs.get('overview','') or en.get('overview',''),
        'poster':        IMG + cs.get('poster_path','') if cs.get('poster_path') else
                         (IMG + en.get('poster_path','') if en.get('poster_path') else ''),
        'genres':        [g['name'] for g in (cs.get('genres') or [])],
        'vote_average':  cs.get('vote_average', 0),
    }

def load_json(path):
    if not os.path.exists(path): return []
    with open(path, encoding='utf-8') as f: return json.load(f)

def save_json(path, data):
    text = json.dumps(data, ensure_ascii=False, separators=(',',':'))
    with open(path, 'w', encoding='utf-8') as f: f.write(text)
    with gzip.open(path+'.gz','wb') as f: f.write(text.encode())
    print(f'  Ulozeno: {os.path.basename(path)} ({os.path.getsize(path)//1024} KB)')

def match_movies(movies):
    print(f'\nParovani filmu ({len(movies)} zaznamu)...')
    matched = 0
    for i, m in enumerate(movies):
        if i % 200 == 0:
            print(f'  [{i}/{len(movies)}] matched: {matched}', flush=True)
        if m.get('tmdb_id'): continue
        title = m.get('clean_title','')
        year  = m.get('year')
        if not title: continue

        result = search_movie(title, year)
        if not result: continue

        sim = max(
            similarity(title, result.get('title','')),
            similarity(title, result.get('original_title',''))
        )
        if sim < 0.55: continue

        details = movie_details(result['id'])
        if details:
            m.update(details)
            matched += 1
        time.sleep(0.05)

    print(f'  Hotovo: {matched}/{len(movies)} filmu sparovano')
    return movies

def match_series(series):
    print(f'\nParovani serialu ({len(series)} zaznamu)...')
    matched = 0
    for i, s in enumerate(series):
        if i % 100 == 0:
            print(f'  [{i}/{len(series)}] matched: {matched}', flush=True)
        if s.get('tmdb_id'): continue
        title = s.get('show_title','')
        if not title: continue

        result = search_tv(title)
        if not result: continue

        sim = max(
            similarity(title, result.get('name','')),
            similarity(title, result.get('original_name',''))
        )
        if sim < 0.5: continue

        details = tv_details(result['id'])
        if details:
            s.update(details)
            matched += 1
        time.sleep(0.05)

    print(f'  Hotovo: {matched}/{len(series)} serialu sparovano')
    return series

def main():
    if not TMDB_KEY:
        print('WARN: TMDB_KEY neni nastaven – preskakuji matching.')
        return

    print('=== TMDB Matching v2 ===')
    print(f'Token: {TMDB_KEY[:10]}...')

    # Test spojeni
    test = tmdb_get('/configuration')
    if not test:
        print('ERROR: TMDB API nedostupne – zkontroluj TMDB_KEY v GitHub Secrets')
        return
    print('TMDB spojeni OK')

    movies_path = os.path.join(DB_DIR, 'movies.json')
    series_path = os.path.join(DB_DIR, 'series.json')

    movies = load_json(movies_path)
    series = load_json(series_path)
    print(f'Nacteno: {len(movies)} filmu, {len(series)} serialu')

    movies = match_movies(movies)
    series = match_series(series)

    save_json(movies_path, movies)
    save_json(series_path, series)

    m_matched = sum(1 for m in movies if m.get('tmdb_id'))
    s_matched = sum(1 for s in series if s.get('tmdb_id'))
    print(f'\n=== VYSLEDEK ===')
    print(f'Filmy:   {m_matched}/{len(movies)} sparovano')
    print(f'Serialy: {s_matched}/{len(series)} sparovano')

    # Ukazka
    print('\nUkazka CZ nazvů:')
    for s in series[:5]:
        if s.get('display_title'):
            print(f'  WS: {s["show_title"]}  →  {s["display_title"]} / {s.get("alt_title","")}')

if __name__ == '__main__':
    main()
