#!/usr/bin/env python3
"""
tmdb_enrich.py – obohatí movies.json a series.json o TMDB metadata.
Přidá: tmdb_id, title, original_title, overview, poster_path,
       backdrop_path, vote_average, popularity, genre_ids.
Běží po build_db.py jako GitHub Action krok.
"""
import os, sys, json, time, gzip, re, unicodedata
import urllib.request, urllib.parse

OUT_DIR  = os.path.join(os.path.dirname(__file__), '..', 'db')
TMDB_KEY = os.environ.get('TMDB_API_KEY', '')
BASE     = 'https://api.themoviedb.org/3'
LANG     = 'cs-CZ'
PAUSE    = 0.26   # TMDB rate limit: ~4 req/s
BATCH_PAUSE = 5   # pauza každých 200 filmů

# ── HTTP ──────────────────────────────────────────────────────────────────────
def _get(url, params=None, retries=3):
    if params:
        url = url + '?' + urllib.parse.urlencode(params)
    headers = {'User-Agent': 'StreamCinemaKodi/1.0'}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            wait = 3 * (2 ** attempt)
            if '429' in str(e):
                wait = 30
                print(f'  [rate limit] čekám {wait}s', flush=True)
            else:
                print(f'  [retry {attempt+1}] {e}', flush=True)
            time.sleep(wait)
    return None

# ── Normalizace ───────────────────────────────────────────────────────────────
def _norm(s):
    if not s: return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'^\s*(the|a|an)\s+', '', s)
    return re.sub(r'\s+', ' ', s).strip()

# ── TMDB vyhledávání ──────────────────────────────────────────────────────────
def search_movie(title, year=None):
    """Najde film na TMDB a vrátí metadata."""
    params = {
        'api_key': TMDB_KEY,
        'query': title,
        'language': LANG,
        'include_adult': 'false',
    }
    if year:
        params['year'] = str(year)

    data = _get(f'{BASE}/search/movie', params)
    if not data or not data.get('results'):
        # Zkus bez roku
        if year:
            params.pop('year', None)
            data = _get(f'{BASE}/search/movie', params)

    if not data or not data.get('results'):
        return None

    norm_title = _norm(title)
    best = None
    best_score = -1

    for r in data['results'][:5]:
        # Podobnost názvu
        r_norm_cz   = _norm(r.get('title', ''))
        r_norm_orig = _norm(r.get('original_title', ''))
        sim = max(
            _similarity(norm_title, r_norm_cz),
            _similarity(norm_title, r_norm_orig),
        )
        # Bonifikace za rok
        r_year = int((r.get('release_date') or '0')[:4] or 0)
        if year and r_year and abs(r_year - int(year)) <= 1:
            sim += 0.2
        if sim > best_score:
            best_score = sim
            best = r

    if best_score < 0.4:
        return None
    return best

def search_series(title, year=None):
    """Najde seriál na TMDB."""
    params = {
        'api_key': TMDB_KEY,
        'query': title,
        'language': LANG,
    }
    if year:
        params['first_air_date_year'] = str(year)

    data = _get(f'{BASE}/search/tv', params)
    if not data or not data.get('results'):
        if year:
            params.pop('first_air_date_year', None)
            data = _get(f'{BASE}/search/tv', params)

    if not data or not data.get('results'):
        return None

    norm_title = _norm(title)
    best = None
    best_score = -1

    for r in data['results'][:5]:
        r_norm = _norm(r.get('name', ''))
        r_norm_orig = _norm(r.get('original_name', ''))
        sim = max(
            _similarity(norm_title, r_norm),
            _similarity(norm_title, r_norm_orig),
        )
        if sim > best_score:
            best_score = sim
            best = r

    if best_score < 0.4:
        return None
    return best

def _similarity(a, b):
    wa = set(a.split())
    wb = set(b.split())
    if not wa or not wb: return 0.0
    return len(wa & wb) / max(len(wa), len(wb))

# ── Uložení ───────────────────────────────────────────────────────────────────
def save_json(path, data):
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    with gzip.open(path + '.gz', 'wb') as gz:
        gz.write(text.encode('utf-8'))
    kb = os.path.getsize(path) // 1024
    print(f'  Uloženo: {os.path.basename(path)} ({kb} KB, {len(data)} položek)', flush=True)

# ── Hlavní logika ─────────────────────────────────────────────────────────────
def enrich_movies(movies):
    """Obohatí filmy o TMDB metadata."""
    enriched = 0
    already  = sum(1 for m in movies if m.get('tmdb_id'))
    todo     = [m for m in movies if not m.get('tmdb_id')]

    print(f'Filmů celkem: {len(movies)}, již obohaceno: {already}, ke zpracování: {len(todo)}', flush=True)

    for i, movie in enumerate(movies):
        if movie.get('tmdb_id'):
            continue  # přeskoč již obohacené

        title = movie.get('clean_title') or movie.get('norm_title', '')
        year  = movie.get('year')

        if i % 50 == 0:
            pct = int(i * 100 / len(movies))
            print(f'[{pct:3d}%] {i}/{len(movies)} | obohaceno: {enriched} | {title[:40]}', flush=True)

        result = search_movie(title, year)
        if result:
            movie['tmdb_id']        = result['id']
            movie['title']          = result.get('title') or title
            movie['original_title'] = result.get('original_title', '')
            movie['overview']       = result.get('overview', '')
            movie['poster_path']    = result.get('poster_path', '')
            movie['backdrop_path']  = result.get('backdrop_path', '')
            movie['vote_average']   = float(result.get('vote_average') or 0)
            movie['popularity']     = float(result.get('popularity') or 0)
            movie['genre_ids']      = json.dumps([g for g in result.get('genre_ids', [])])
            enriched += 1

        time.sleep(PAUSE)
        if i > 0 and i % 200 == 0:
            print(f'  *** Přestávka {BATCH_PAUSE}s ***', flush=True)
            time.sleep(BATCH_PAUSE)

    print(f'Filmů obohaceno: {enriched}/{len(todo)}', flush=True)
    return movies

def enrich_series(series):
    """Obohatí seriály o TMDB metadata."""
    enriched = 0
    already  = sum(1 for s in series if s.get('tmdb_id'))
    todo     = [s for s in series if not s.get('tmdb_id')]

    print(f'\nSeriálů celkem: {len(series)}, již obohaceno: {already}, ke zpracování: {len(todo)}', flush=True)

    for i, show in enumerate(series):
        if show.get('tmdb_id'):
            continue

        title = show.get('show_title', '')
        year  = show.get('year')

        if i % 50 == 0:
            pct = int(i * 100 / len(series))
            print(f'[{pct:3d}%] {i}/{len(series)} | obohaceno: {enriched} | {title[:40]}', flush=True)

        result = search_series(title, year)
        if result:
            show['tmdb_id']        = result['id']
            show['title']          = result.get('name') or title
            show['original_title'] = result.get('original_name', '')
            show['overview']       = result.get('overview', '')
            show['poster_path']    = result.get('poster_path', '')
            show['backdrop_path']  = result.get('backdrop_path', '')
            show['vote_average']   = float(result.get('vote_average') or 0)
            show['popularity']     = float(result.get('popularity') or 0)
            show['genre_ids']      = json.dumps([g for g in result.get('genre_ids', [])])
            enriched += 1

        time.sleep(PAUSE)
        if i > 0 and i % 200 == 0:
            print(f'  *** Přestávka {BATCH_PAUSE}s ***', flush=True)
            time.sleep(BATCH_PAUSE)

    print(f'Seriálů obohaceno: {enriched}/{len(todo)}', flush=True)
    return series

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not TMDB_KEY:
        print('ERROR: Nastav TMDB_API_KEY jako GitHub Secret', flush=True)
        sys.exit(1)

    print('=== TMDB Enrich ===', flush=True)

    movies_path = os.path.join(OUT_DIR, 'movies.json')
    series_path = os.path.join(OUT_DIR, 'series.json')

    with open(movies_path, 'r', encoding='utf-8') as f:
        movies = json.load(f)
    with open(series_path, 'r', encoding='utf-8') as f:
        series = json.load(f)

    print(f'Načteno: {len(movies)} filmů, {len(series)} seriálů\n', flush=True)

    movies = enrich_movies(movies)
    save_json(movies_path, movies)

    series = enrich_series(series)
    save_json(series_path, series)

    print('\nHOTOVO', flush=True)

if __name__ == '__main__':
    main()
