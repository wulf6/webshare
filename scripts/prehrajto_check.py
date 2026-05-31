#!/usr/bin/env python3
"""
prehrajto_check.py – kontrola dostupnosti filmů a seriálů na Přehraj.to
Přidává pole pt_available do movies.json a series.json.
Běží jako GitHub Action jednou týdně.
"""
import os, sys, json, time, re, unicodedata, gzip, datetime
import urllib.request, urllib.parse

OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'db')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
      'AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/120.0.0.0 Safari/537.36')
BASE_URL = 'https://prehraj.to'
PAUSE = 1.5       # pauza mezi requesty (sec) - nesmíme být zablokováni
BATCH_PAUSE = 10  # pauza každých 100 filmů
MAX_ITEMS = 25000 # max filmů na jeden běh

# ── HTTP ──────────────────────────────────────────────────────────────────────
def _get(url, retries=3):
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'cs-CZ,cs;q=0.9,en;q=0.8',
        'Referer': 'https://prehraj.to/',
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
                ce = r.headers.get('Content-Encoding', '')
                if 'gzip' in ce:
                    import gzip as gz
                    data = gz.decompress(data)
                return data.decode('utf-8', errors='ignore')
        except Exception as e:
            wait = 3 * (2 ** attempt)
            print(f'  [retry {attempt+1}] {e} – čekám {wait}s', flush=True)
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

# ── Vyhledávání na Přehraj.to ─────────────────────────────────────────────────
def search_prehrajto(query, year=None):
    """Vrátí True pokud byl nalezen výsledek odpovídající dotazu."""
    encoded = urllib.parse.quote(query, safe='')
    url = f'{BASE_URL}/hledej/{encoded}'
    html = _get(url)
    if not html:
        return False

    # Najdi výsledky - anchor tagy s video URL /slug/hexid
    pattern = re.compile(
        r'<a[^>]+href="(/[a-z0-9][a-z0-9\-]*/[0-9a-f]{8,})"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    _SKIP = ('/hledej/', '/profil/', '/jak-na-to', '/podminky',
             '/faq', '/kontakt', '/nahlasit', '/navrhy')

    norm_query = _norm(query)
    results = []

    for m in pattern.finditer(html):
        slug_path = m.group(1)
        if any(slug_path.startswith(s) for s in _SKIP):
            continue
        link_text = re.sub(r'<[^>]+>', ' ', m.group(2))
        link_text = re.sub(r'\s+', ' ', link_text).strip()
        if not link_text or len(link_text) < 3:
            continue
        results.append((slug_path, link_text))

    if not results:
        return False

    # Zkontroluj shodu - normalizovaný název musí být obsažen v výsledku
    for slug, text in results[:5]:
        norm_text = _norm(text)
        norm_q_words = set(norm_query.split())
        norm_t_words = set(norm_text.split())
        # Alespoň 60% slov z dotazu musí být v textu výsledku
        if not norm_q_words:
            continue
        overlap = len(norm_q_words & norm_t_words) / len(norm_q_words)
        if overlap >= 0.6:
            # Zkontroluj rok pokud je k dispozici
            if year:
                year_in_text = str(year) in text or str(year-1) in text or str(year+1) in text
                year_in_slug = str(year) in slug or str(year-1) in slug or str(year+1) in slug
                if year_in_text or year_in_slug or overlap >= 0.85:
                    return True
            else:
                return True

    return False

# ── Zpracování ────────────────────────────────────────────────────────────────
def check_movies(movies):
    """Zkontroluje dostupnost filmů na Přehraj.to."""
    total = len(movies)
    found = 0
    already = sum(1 for m in movies if m.get('pt_available'))

    print(f'Filmů celkem: {total}', flush=True)
    print(f'Již označených: {already}', flush=True)
    print(f'Ke kontrole: {total - already}', flush=True)

    for i, movie in enumerate(movies):
        # Přeskoč již zkontrolované
        if movie.get('pt_available') is not None:
            if movie.get('pt_available'):
                found += 1
            continue

        title = movie.get('clean_title') or movie.get('norm_title', '')
        year = movie.get('year')

        if not title:
            movie['pt_available'] = False
            continue

        if i % 10 == 0:
            pct = int(i * 100 / total)
            print(f'[{pct:3d}%] {i}/{total} | PT dostupných: {found} | {title[:40]}', flush=True)

        # Sestaví dotaz
        query = f'{title} {year}' if year else title
        available = search_prehrajto(query, year)

        # Fallback: zkus bez roku pokud nenašel
        if not available and year:
            time.sleep(PAUSE * 0.5)
            available = search_prehrajto(title)

        movie['pt_available'] = available
        if available:
            found += 1

        time.sleep(PAUSE)

        # Delší pauza každých 100 filmů
        if i > 0 and i % 100 == 0:
            print(f'  *** Přestávka {BATCH_PAUSE}s ***', flush=True)
            time.sleep(BATCH_PAUSE)

        if i >= MAX_ITEMS:
            print(f'Dosažen limit {MAX_ITEMS} filmů', flush=True)
            break

    return movies, found

def check_series(series):
    """Zkontroluje dostupnost seriálů na Přehraj.to."""
    total = len(series)
    found = 0
    already = sum(1 for s in series if s.get('pt_available'))

    print(f'\nSeriálů celkem: {total}', flush=True)
    print(f'Již označených: {already}', flush=True)

    for i, show in enumerate(series):
        if show.get('pt_available') is not None:
            if show.get('pt_available'):
                found += 1
            continue

        title = show.get('show_title', '')
        year = show.get('year')

        if not title:
            show['pt_available'] = False
            continue

        if i % 10 == 0:
            pct = int(i * 100 / total)
            print(f'[{pct:3d}%] {i}/{total} | PT dostupných: {found} | {title[:40]}', flush=True)

        query = f'{title} {year}' if year else title
        available = search_prehrajto(query, year)

        if not available and year:
            time.sleep(PAUSE * 0.5)
            available = search_prehrajto(title)

        show['pt_available'] = available
        if available:
            found += 1

        time.sleep(PAUSE)

        if i > 0 and i % 100 == 0:
            print(f'  *** Přestávka {BATCH_PAUSE}s ***', flush=True)
            time.sleep(BATCH_PAUSE)

    return series, found

# ── Uložení ───────────────────────────────────────────────────────────────────
def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    with gzip.open(path + '.gz', 'wb') as gz:
        gz.write(text.encode('utf-8'))
    kb = os.path.getsize(path) // 1024
    print(f'  Uloženo: {os.path.basename(path)} ({kb} KB, {len(data)} položek)', flush=True)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f'=== Přehraj.to kontrola dostupnosti ===', flush=True)
    print(f'Datum: {datetime.datetime.now().strftime("%d.%m.%Y %H:%M")}', flush=True)

    movies_path = os.path.join(OUT_DIR, 'movies.json')
    series_path = os.path.join(OUT_DIR, 'series.json')
    meta_path   = os.path.join(OUT_DIR, 'meta.json')

    if not os.path.exists(movies_path):
        print('ERROR: movies.json nenalezen. Spusť nejdřív build_db.py', flush=True)
        sys.exit(1)

    with open(movies_path, 'r', encoding='utf-8') as f:
        movies = json.load(f)
    with open(series_path, 'r', encoding='utf-8') as f:
        series = json.load(f)
    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)

    print(f'Načteno: {len(movies)} filmů, {len(series)} seriálů\n', flush=True)

    # Test připojení
    print('Test připojení na Přehraj.to...', flush=True)
    test = _get(f'{BASE_URL}/hledej/test')
    if not test:
        print('ERROR: Přehraj.to není dostupné', flush=True)
        sys.exit(1)
    print('Přehraj.to dostupné ✓\n', flush=True)

    start = time.time()

    # Zkontroluj filmy
    print('=== FILMY ===', flush=True)
    movies, m_found = check_movies(movies)
    save_json(movies_path, movies)

    # Zkontroluj seriály
    print('\n=== SERIÁLY ===', flush=True)
    series, s_found = check_series(series)
    save_json(series_path, series)

    # Aktualizuj meta.json
    meta['pt_movies_available'] = m_found
    meta['pt_series_available'] = s_found
    meta['pt_checked_at'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    save_json(meta_path, [meta] if isinstance(meta, dict) else meta)
    # Přepis meta jako dict
    text = json.dumps(meta, ensure_ascii=False, separators=(',', ':'))
    with open(meta_path, 'w', encoding='utf-8') as f:
        f.write(text)

    elapsed = int(time.time() - start)
    print(f'\n=== HOTOVO za {elapsed//60}m {elapsed%60}s ===', flush=True)
    print(f'Filmy na PT:   {m_found}/{len(movies)}', flush=True)
    print(f'Seriály na PT: {s_found}/{len(series)}', flush=True)

if __name__ == '__main__':
    main()
