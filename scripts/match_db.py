#!/usr/bin/env python3
"""
match_db.py v5 – Wikidata matching
- Rychlý matching: index podle délky názvu, žádné O(n*m) fuzzy
- Retry pro Wikidata 429
- Dynamický threshold podle délky názvu (krátké názvy = vyšší threshold)
"""
import os, json, time, re, unicodedata, urllib.request, urllib.parse, gzip
from difflib import SequenceMatcher
from collections import defaultdict

DB_DIR = os.path.join(os.path.dirname(__file__), '..', 'db')
SPARQL = 'https://query.wikidata.org/sparql'

def norm(s):
    if not s: return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'^\s*(the|a|an)\s+', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

def best_title(cz, en):
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

def wikidata_query(sparql, retries=3):
    url = SPARQL + '?' + urllib.parse.urlencode({'query': sparql, 'format': 'json'})
    req = urllib.request.Request(url, headers={
        'User-Agent': 'WebshareKodiBot/1.0 (github.com/wulf6/webshare)',
        'Accept': 'application/json',
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f'  [WD] chyba (pokus {attempt+1}/{retries}): {e}')
            if '429' in str(e):
                wait = 70 * (attempt + 1)
                print(f'  [WD] rate limit – cekam {wait}s...')
                time.sleep(wait)
            else:
                time.sleep(5)
    return None

def fetch_wikidata(sparql, label):
    print(f'  Stahuji {label} z Wikidata...', flush=True)
    data = wikidata_query(sparql)
    if not data:
        return {}, {}

    by_norm = {}
    by_len  = defaultdict(list)

    for row in data.get('results', {}).get('bindings', []):
        en = row.get('enLabel', {}).get('value', '')
        cs = row.get('csLabel', {}).get('value', '')
        yr = row.get('year',   {}).get('value', '')
        val = {'en': en, 'cs': cs, 'year': int(yr) if yr else None}

        for title in set(filter(None, [en, cs])):
            k = norm(title)   # norm() odstraňuje diakritiku → srovnatelné s WS názvy
            if k and k not in by_norm:
                by_norm[k] = val
                by_len[len(k)].append(k)

    print(f'  Nacteno {len(by_norm)} zaznamu ({label})')
    return by_norm, by_len

MOVIES_SPARQL = '''
SELECT ?item ?enLabel ?csLabel ?year WHERE {
  ?item wdt:P31 wd:Q11424.
  ?item wdt:P577 ?date.
  BIND(YEAR(?date) AS ?year)
  FILTER(?year >= 1970)
  ?item rdfs:label ?enLabel. FILTER(LANG(?enLabel) = "en")
  OPTIONAL { ?item rdfs:label ?csLabel. FILTER(LANG(?csLabel) = "cs") }
}
LIMIT 50000
'''

SERIES_SPARQL = '''
SELECT ?item ?enLabel ?csLabel ?year WHERE {
  ?item wdt:P31 wd:Q5398426.
  OPTIONAL { ?item wdt:P580 ?date. BIND(YEAR(?date) AS ?year) }
  ?item rdfs:label ?enLabel. FILTER(LANG(?enLabel) = "en")
  OPTIONAL { ?item rdfs:label ?csLabel. FILTER(LANG(?csLabel) = "cs") }
}
LIMIT 30000
'''

def dynamic_threshold(title_norm):
    """Kratší názvy potřebují vyšší threshold aby se zabránilo špatným shodám."""
    l = len(title_norm)
    if l <= 5:  return 1.0   # jen přímá shoda (alf, alone, ...)
    if l <= 8:  return 0.95
    if l <= 12: return 0.92
    if l <= 20: return 0.88
    return 0.85

def fuzzy_match(title_norm, by_norm, by_len, year=None):
    # Přímá shoda
    hit = by_norm.get(title_norm)
    if hit:
        if year and hit.get('year') and abs(hit['year'] - year) > 3:
            pass  # rok nesedí, zkus fuzzy
        else:
            return hit

    threshold = dynamic_threshold(title_norm)

    # Fuzzy — jen záznamy s délkou ±3
    tlen = len(title_norm)
    candidates = []
    for delta in range(0, 4):
        for l in [tlen - delta, tlen + delta]:
            candidates.extend(by_len.get(l, []))

    best_sim = 0.0
    best_hit = None
    for k in candidates:
        val = by_norm[k]
        if year and val.get('year') and abs(val['year'] - year) > 3:
            continue
        s = similarity(title_norm, k)
        if s > best_sim and s >= threshold:
            best_sim = s
            best_hit = val

    return best_hit

def match_records(records, by_norm, by_len, title_field, label):
    print(f'\nParovani {label} ({len(records)} zaznamu)...')
    matched = 0
    for i, r in enumerate(records):
        if i % 500 == 0:
            print(f'  [{i}/{len(records)}] matched: {matched}', flush=True)
        if r.get('display_title'):
            continue
        title = r.get(title_field, '')
        if not title:
            continue
        year    = r.get('year')
        title_n = norm(title)
        hit     = fuzzy_match(title_n, by_norm, by_len, year=year)
        if hit:
            display, alt = best_title(hit.get('cs', ''), hit.get('en', ''))
            r['display_title'] = display
            r['alt_title']     = alt
            r['cz_title']      = hit.get('cs', '')
            r['en_title']      = hit.get('en', '')
            matched += 1

    print(f'  Hotovo: {matched}/{len(records)} sparovano')
    return records

def main():
    print('=== Wikidata Matching v5 ===', flush=True)

    movies_path = os.path.join(DB_DIR, 'movies.json')
    series_path = os.path.join(DB_DIR, 'series.json')

    movies = load_json(movies_path)
    series = load_json(series_path)
    print(f'Nacteno: {len(movies)} filmu, {len(series)} serialu')

    wd_movies, wd_movies_len = fetch_wikidata(MOVIES_SPARQL, 'filmy')
    time.sleep(3)
    wd_series, wd_series_len = fetch_wikidata(SERIES_SPARQL, 'serialy')

    if wd_movies:
        movies = match_records(movies, wd_movies, wd_movies_len,
                               title_field='clean_title', label='filmy')
    else:
        print('WARN: Wikidata filmy nedostupne – preskakuji.')

    if wd_series:
        series = match_records(series, wd_series, wd_series_len,
                               title_field='show_title', label='serialy')
    else:
        print('WARN: Wikidata serialy nedostupne – preskakuji.')

    save_json(movies_path, movies)
    save_json(series_path, series)

    m_matched = sum(1 for m in movies if m.get('display_title'))
    s_matched = sum(1 for s in series if s.get('display_title'))
    print(f'\n=== VYSLEDEK ===')
    print(f'Filmy:   {m_matched}/{len(movies)} sparovano')
    print(f'Serialy: {s_matched}/{len(series)} sparovano')

    print('\nUkazka serialu:')
    shown = 0
    for s in series:
        if s.get('display_title') and s['display_title'] != s.get('show_title', ''):
            print(f'  WS: "{s["show_title"]}"  ->  "{s["display_title"]}" / "{s.get("alt_title","")}"')
            shown += 1
            if shown >= 15: break

if __name__ == '__main__':
    main()
