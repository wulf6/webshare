#!/usr/bin/env python3
"""
match_db.py v3 – Wikidata matching
CZ + EN nazvy filmu a serialu, funguje z GitHub Actions
"""
import os, json, time, re, unicodedata, urllib.request, urllib.parse, gzip
from difflib import SequenceMatcher

DB_DIR = os.path.join(os.path.dirname(__file__), '..', 'db')
SPARQL = 'https://query.wikidata.org/sparql'
IMG    = 'https://image.tmdb.org/t/p/w300'

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

def wikidata_query(sparql):
    """Spustí SPARQL dotaz na Wikidata."""
    url = SPARQL + '?' + urllib.parse.urlencode({'query': sparql, 'format': 'json'})
    req = urllib.request.Request(url, headers={
        'User-Agent': 'WebshareKodiBot/1.0 (github.com/wulf6/webshare)',
        'Accept': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'  [WD] chyba: {e}')
        return None

def best_title(cz, en):
    """CZ název pokud existuje a liší se od EN, jinak EN."""
    cz = (cz or '').strip()
    en = (en or '').strip()
    if cz and norm(cz) != norm(en):
        return cz, en
    return en, ''

def load_json(path):
    if not os.path.exists(path): return []
    with open(path, encoding='utf-8') as f: return json.load(f)

def save_json(path, data):
    text = json.dumps(data, ensure_ascii=False, separators=(',',':'))
    with open(path, 'w', encoding='utf-8') as f: f.write(text)
    with gzip.open(path+'.gz','wb') as f: f.write(text.encode())
    print(f'  Ulozeno: {os.path.basename(path)} ({os.path.getsize(path)//1024} KB, {len(data)} polozek)')

# ── Stažení celé Wikidata DB filmů a seriálů ─────────────────────────────────

def fetch_wikidata_movies():
    """Stáhne top filmy z Wikidata s CZ+EN názvem."""
    print('  Stahuji filmy z Wikidata...', flush=True)
    sparql = '''
    SELECT ?item ?enLabel ?csLabel ?year WHERE {
      ?item wdt:P31 wd:Q11424.
      ?item wdt:P577 ?date.
      BIND(YEAR(?date) AS ?year)
      FILTER(?year >= 1980)
      ?item rdfs:label ?enLabel. FILTER(LANG(?enLabel) = "en")
      OPTIONAL { ?item rdfs:label ?csLabel. FILTER(LANG(?csLabel) = "cs") }
    }
    LIMIT 50000
    '''
    data = wikidata_query(sparql)
    if not data: return {}

    result = {}
    for row in data.get('results',{}).get('bindings',[]):
        en  = row.get('enLabel',{}).get('value','')
        cs  = row.get('csLabel',{}).get('value','')
        yr  = row.get('year',{}).get('value','')
        key = norm(en)
        if key:
            result[key] = {'en': en, 'cs': cs, 'year': int(yr) if yr else None}
        # Taky indexuj podle CS nazvu
        if cs:
            result[norm(cs)] = {'en': en, 'cs': cs, 'year': int(yr) if yr else None}
    print(f'  Nacteno {len(result)} filmovych zaznamu z Wikidata')
    return result

def fetch_wikidata_series():
    """Stáhne TV seriály z Wikidata s CZ+EN názvem."""
    print('  Stahuji serialy z Wikidata...', flush=True)
    sparql = '''
    SELECT ?item ?enLabel ?csLabel ?year WHERE {
      ?item wdt:P31 wd:Q5398426.
      OPTIONAL { ?item wdt:P580 ?date. BIND(YEAR(?date) AS ?year) }
      ?item rdfs:label ?enLabel. FILTER(LANG(?enLabel) = "en")
      OPTIONAL { ?item rdfs:label ?csLabel. FILTER(LANG(?csLabel) = "cs") }
    }
    LIMIT 30000
    '''
    data = wikidata_query(sparql)
    if not data: return {}

    result = {}
    for row in data.get('results',{}).get('bindings',[]):
        en  = row.get('enLabel',{}).get('value','')
        cs  = row.get('csLabel',{}).get('value','')
        yr  = row.get('year',{}).get('value','')
        key = norm(en)
        if key:
            result[key] = {'en': en, 'cs': cs, 'year': int(yr) if yr else None}
        if cs:
            result[norm(cs)] = {'en': en, 'cs': cs, 'year': int(yr) if yr else None}
    print(f'  Nacteno {len(result)} serialovych zaznamu z Wikidata')
    return result

# ── Matching ──────────────────────────────────────────────────────────────────

def match_movies(movies, wd_movies):
    print(f'\nParovani filmu ({len(movies)} zaznamu)...')
    matched = 0
    for i, m in enumerate(movies):
        if i % 500 == 0:
            print(f'  [{i}/{len(movies)}] matched: {matched}', flush=True)
        if m.get('display_title'): continue

        title = m.get('clean_title','')
        year  = m.get('year')
        if not title: continue

        # Hledej přímou shodu
        key = norm(title)
        hit = wd_movies.get(key)

        # Pokud není přímá shoda, zkus fuzzy
        if not hit:
            best_sim = 0.0
            for wd_key, wd_val in wd_movies.items():
                # Zkontroluj rok pokud ho máme
                if year and wd_val.get('year') and abs(wd_val['year'] - year) > 2:
                    continue
                s = similarity(title, wd_val['en'])
                if s > best_sim and s >= 0.85:
                    best_sim = s; hit = wd_val
                if wd_val.get('cs'):
                    s2 = similarity(title, wd_val['cs'])
                    if s2 > best_sim and s2 >= 0.85:
                        best_sim = s2; hit = wd_val

        if hit:
            display, alt = best_title(hit.get('cs',''), hit.get('en',''))
            m['display_title'] = display
            m['alt_title']     = alt
            m['cz_title']      = hit.get('cs','')
            m['en_title']      = hit.get('en','')
            matched += 1

    print(f'  Hotovo: {matched}/{len(movies)} filmu sparovano')
    return movies

def match_series(series, wd_series):
    print(f'\nParovani serialu ({len(series)} zaznamu)...')
    matched = 0
    for i, s in enumerate(series):
        if i % 200 == 0:
            print(f'  [{i}/{len(series)}] matched: {matched}', flush=True)
        if s.get('display_title'): continue

        title = s.get('show_title','')
        if not title: continue

        key = norm(title)
        hit = wd_series.get(key)

        if not hit:
            best_sim = 0.0
            for wd_key, wd_val in wd_series.items():
                sv = similarity(title, wd_val['en'])
                if sv > best_sim and sv >= 0.82:
                    best_sim = sv; hit = wd_val
                if wd_val.get('cs'):
                    sv2 = similarity(title, wd_val['cs'])
                    if sv2 > best_sim and sv2 >= 0.82:
                        best_sim = sv2; hit = wd_val

        if hit:
            display, alt = best_title(hit.get('cs',''), hit.get('en',''))
            s['display_title'] = display
            s['alt_title']     = alt
            s['cz_title']      = hit.get('cs','')
            s['en_title']      = hit.get('en','')
            matched += 1

    print(f'  Hotovo: {matched}/{len(series)} serialu sparovano')
    return series

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print('=== Wikidata Matching v3 ===', flush=True)

    movies_path = os.path.join(DB_DIR, 'movies.json')
    series_path = os.path.join(DB_DIR, 'series.json')

    movies = load_json(movies_path)
    series = load_json(series_path)
    print(f'Nacteno: {len(movies)} filmu, {len(series)} serialu')

    # Stáhni Wikidata
    wd_movies = fetch_wikidata_movies()
    time.sleep(2)  # respektuj rate limit Wikidata
    wd_series = fetch_wikidata_series()

    if not wd_movies and not wd_series:
        print('WARN: Wikidata nedostupna – preskakuji matching.')
        return

    # Párování
    movies = match_movies(movies, wd_movies)
    series = match_series(series, wd_series)

    # Ulož
    save_json(movies_path, movies)
    save_json(series_path, series)

    # Statistiky
    m_matched = sum(1 for m in movies if m.get('display_title'))
    s_matched = sum(1 for s in series if s.get('display_title'))
    print(f'\n=== VYSLEDEK ===')
    print(f'Filmy:   {m_matched}/{len(movies)} sparovano')
    print(f'Serialy: {s_matched}/{len(series)} sparovano')

    # Ukázka
    print('\nUkazka:')
    for s in series[:10]:
        if s.get('display_title') and s['display_title'] != s.get('show_title',''):
            print(f'  WS: "{s["show_title"]}"  →  "{s["display_title"]}" / "{s.get("alt_title","")}"')

if __name__ == '__main__':
    main()
