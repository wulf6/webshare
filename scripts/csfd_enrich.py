#!/usr/bin/env python3
"""
ČSFD obohacovač – přidá hodnocení, žánry a kanonické názvy do DB.
Běží po build_db.py jako samostatný GitHub Action krok.
Používá scraping HTML ze csfd.cz (bez API klíče).
"""

import os, sys, json, time, re, gzip, unicodedata
import urllib.request, urllib.parse

OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'db')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
      'AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/120.0.0.0 Safari/537.36')

CSFD_CACHE_FILE = os.path.join(OUT_DIR, 'csfd_cache.json')

# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get(url, retries=3):
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'cs-CZ,cs;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate',
        'Referer': 'https://www.csfd.cz/',
        'Connection': 'keep-alive',
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
                # Decompress gzip if needed
                ce = r.headers.get('Content-Encoding', '')
                if 'gzip' in ce:
                    import gzip as gz
                    data = gz.decompress(data)
                return data.decode('utf-8', errors='ignore')
        except Exception as e:
            wait = 2 * (2 ** attempt)
            print(f'  [retry {attempt+1}] {url}: {e} – čekám {wait}s')
            time.sleep(wait)
    return None

# ── Parsování ČSFD ────────────────────────────────────────────────────────────

def search_csfd(query, is_series=False):
    """Vyhledá na ČSFD a vrátí první výsledek."""
    q = urllib.parse.quote_plus(query)
    typ = 'tvshow' if is_series else 'film'
    url = f'https://www.csfd.cz/hledat/?q={q}'
    html = _get(url)
    if not html:
        return None

    # Najdi první výsledek - film nebo seriál
    # ČSFD vrátí sekce: Filmy, Seriály, Osoby
    results = []

    # Hledej film/seriál linky ve výsledcích
    pattern = re.compile(
        r'href="(/film/(\d+)-[^"]+/)"[^>]*>([^<]+)</a>',
        re.IGNORECASE
    )
    for m in pattern.finditer(html):
        path, csfd_id, title = m.group(1), m.group(2), m.group(3).strip()
        results.append({'id': csfd_id, 'path': path, 'title': title})

    if not results:
        return None

    # Vyber nejlepší shodu
    norm_query = _normalize(query)
    best = None
    best_score = -1
    for r in results[:5]:
        norm_title = _normalize(r['title'])
        score = _similarity(norm_query, norm_title)
        if score > best_score:
            best_score = score
            best = r

    if best_score < 0.4:
        return None

    return best

def get_csfd_detail(csfd_id, path):
    """Stáhne detail filmu/seriálu ze ČSFD."""
    url = f'https://www.csfd.cz{path}'
    html = _get(url)
    if not html:
        return None

    result = {'csfd_id': csfd_id, 'url': url}

    # Hodnocení - hledej procenta
    rating_m = re.search(r'<div[^>]*class="[^"]*film-rating-average[^"]*"[^>]*>(\d+)%', html)
    if not rating_m:
        rating_m = re.search(r'"ratingValue"\s*:\s*"?(\d+(?:\.\d+)?)"?', html)
    if rating_m:
        try:
            val = float(rating_m.group(1))
            # ČSFD používá procenta 0-100
            result['rating'] = int(val) if val <= 100 else int(val / 10)
        except:
            pass

    # Žánry
    genres = []
    genre_m = re.findall(r'<a[^>]+href="/filmy/zanr[^"]*"[^>]*>([^<]+)</a>', html)
    genres = [g.strip() for g in genre_m if g.strip()]
    if genres:
        result['genres'] = genres[:5]

    # Rok
    year_m = re.search(r'<span[^>]*itemprop="dateCreated"[^>]*>(\d{4})</span>', html)
    if year_m:
        result['year'] = int(year_m.group(1))

    # Kanonický název (CZ)
    title_m = re.search(r'<h1[^>]*itemprop="name"[^>]*>([^<]+)</h1>', html)
    if title_m:
        result['canon_title'] = title_m.group(1).strip()

    return result

# ── Pomocné funkce ────────────────────────────────────────────────────────────

def _normalize(s):
    if not s: return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'^\s*(the|a|an)\s+', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def _similarity(a, b):
    """Jednoduchá podobnost dvou řetězců (Jaccard na slovech)."""
    wa = set(a.split())
    wb = set(b.split())
    if not wa or not wb: return 0
    return len(wa & wb) / len(wa | wb)

def load_cache():
    if os.path.exists(CSFD_CACHE_FILE):
        with open(CSFD_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CSFD_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    with gzip.open(path + '.gz', 'wb') as gz:
        gz.write(text.encode('utf-8'))
    kb = os.path.getsize(path) // 1024
    print(f'  Uloženo: {os.path.basename(path)} ({kb} KB, {len(data)} položek)')

# ── Hlavní logika ─────────────────────────────────────────────────────────────

def enrich_movies(movies, cache, max_items=500):
    """Obohatí filmy o ČSFD data."""
    enriched = 0
    for i, movie in enumerate(movies):
        title = movie.get('clean_title') or movie.get('norm_title', '')
        year  = movie.get('year')
        key   = f"movie:{_normalize(title)}"

        if key in cache:
            # Použij cache
            cd = cache[key]
        else:
            # Vyhledej na ČSFD
            query = f"{title} {year}" if year else title
            print(f'  [{i+1}/{min(max_items, len(movies))}] Hledám: {query}')
            result = search_csfd(query, is_series=False)
            if result:
                detail = get_csfd_detail(result['id'], result['path'])
                cd = detail or {}
                cd['csfd_title'] = result['title']
            else:
                cd = {}
            cache[key] = cd
            time.sleep(1.5)  # Pauza mezi dotazy

        if cd.get('rating'):
            movie['csfd_rating'] = cd['rating']
        if cd.get('genres'):
            movie['csfd_genres'] = cd['genres']
        if cd.get('canon_title'):
            movie['csfd_title'] = cd['canon_title']
        if cd.get('csfd_id'):
            movie['csfd_id'] = cd['csfd_id']

        enriched += 1
        if enriched >= max_items:
            break

        # Uložení cache každých 50 položek
        if enriched % 50 == 0:
            save_cache(cache)
            print(f'  → Cache uložena ({enriched} zpracováno)')

    return movies, enriched

def enrich_series(series, cache, max_items=300):
    """Obohatí seriály o ČSFD data."""
    enriched = 0
    for i, show in enumerate(series):
        title = show.get('show_title', '')
        year  = show.get('year')
        key   = f"series:{_normalize(title)}"

        if key in cache:
            cd = cache[key]
        else:
            query = f"{title} {year}" if year else title
            print(f'  [{i+1}/{min(max_items, len(series))}] Hledám seriál: {query}')
            result = search_csfd(query, is_series=True)
            if result:
                detail = get_csfd_detail(result['id'], result['path'])
                cd = detail or {}
                cd['csfd_title'] = result['title']
            else:
                cd = {}
            cache[key] = cd
            time.sleep(1.5)

        if cd.get('rating'):
            show['csfd_rating'] = cd['rating']
        if cd.get('genres'):
            show['csfd_genres'] = cd['genres']
        if cd.get('canon_title'):
            show['csfd_title'] = cd['canon_title']
        if cd.get('csfd_id'):
            show['csfd_id'] = cd['csfd_id']

        enriched += 1
        if enriched >= max_items:
            break

        if enriched % 50 == 0:
            save_cache(cache)
            print(f'  → Cache uložena ({enriched} zpracováno)')

    return series, enriched

def main():
    print('=== ČSFD Obohacovač ===')

    movies_path = os.path.join(OUT_DIR, 'movies.json')
    series_path = os.path.join(OUT_DIR, 'series.json')

    if not os.path.exists(movies_path):
        print('ERROR: movies.json nenalezen. Spusť nejdřív build_db.py')
        sys.exit(1)

    with open(movies_path, 'r', encoding='utf-8') as f:
        movies = json.load(f)
    with open(series_path, 'r', encoding='utf-8') as f:
        series = json.load(f)

    print(f'Načteno: {len(movies)} filmů, {len(series)} seriálů')

    cache = load_cache()
    print(f'Cache: {len(cache)} položek\n')

    # Nejdřív seriály (méně položek, důležitější)
    print(f'=== SERIÁLY ===')
    series, n_s = enrich_series(series, cache, max_items=500)
    save_cache(cache)
    save_json(series_path, series)
    print(f'Seriálů obohaceno: {n_s}')

    # Pak filmy - top podle score
    print(f'\n=== FILMY ===')
    # Seřaď podle score, obohatíme nejlepší
    movies_sorted = sorted(movies, key=lambda x: -x.get('score', 0))
    movies_sorted, n_m = enrich_movies(movies_sorted, cache, max_items=2000)
    save_cache(cache)

    # Vrátíme původní pořadí
    save_json(movies_path, movies_sorted)
    print(f'Filmů obohaceno: {n_m}')

    print(f'\nHOTOVO: {n_s} seriálů, {n_m} filmů obohaceno ze ČSFD')

if __name__ == '__main__':
    main()

