#!/usr/bin/env python3
"""
Webshare DB updater – denní aktualizace.
P�idává nové filmy a epizody bez přepisování celé DB.
Běží rychle ~15–30 minut.
"""

import os, sys, json, time, hashlib, re, unicodedata, datetime, gzip
import urllib.request, urllib.parse
from xml.etree import ElementTree as ET

# Importuj sdílené funkce z build_db.py
sys.path.insert(0, os.path.dirname(__file__))
from build_db import (
    login, parse_file, dedup_movies, dedup_series,
    fetch_all_pages, save_json, _normalize
)

YEAR    = datetime.datetime.now().year
OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'db')

def update_queries():
    """Dotazy pro denní aktualizaci – jen nový obsah."""
    q = []

    # Nové seriály – aktuální rok, všechny sezóny
    for s in range(1, 21):
        stag = f's{s:02d}'
        q.append(f'{stag} {YEAR}')
        q.append(f'{stag} cz {YEAR}')
        q.append(f'{stag} {YEAR-1}')

    # Nejnovější filmy – aktuální a minulý rok
    for y in [YEAR, YEAR - 1]:
        q += [
            f'{y}', f'1080p {y}', f'4k {y}', f'2160p {y}',
            f'uhd {y}', f'cz dabing {y}', f'czech {y}',
            f'bluray {y}', f'webrip {y}', f'web-dl {y}',
            f'720p {y}', f'bdremux {y}', f'4k hdr {y}',
            f'1080p remux {y}', f'1080p bluray {y}',
            f'cz dabing 1080p {y}', f'cz dabing 4k {y}',
            f'sk dabing {y}', f'slovak {y}',
        ]

    # Populární seriály – jen aktuální sezóny
    ACTIVE_SERIES = [
        'simpsonovi', 'the simpsons', 'family guy', 'south park',
        'rick and morty', 'futurama', 'american dad',
        'game of thrones', 'house of dragon', 'rod draku',
        'the boys', 'stranger things', 'euphoria', 'succession',
        'the last of us', 'fallout', 'andor', 'mandalorian',
        'yellowstone', 'peaky blinders', 'ozark', 'true detective',
        'black mirror', 'severance', 'the witcher', 'zaklinar',
        'one piece', 'naruto', 'demon slayer', 'jujutsu kaisen',
        'attack on titan', 'my hero academia', 'chainsaw man',
        'squid game', 'all of us are dead', 'moving',
        'ulice', 'ordinace v ruzove zahrade', 'comeback',
        'ted lasso', 'abbott elementary', 'what we do in the shadows',
        'fargo', 'dexter', 'cobra kai', 'emily in paris',
        'bridgerton', 'wednesday', 'ginny and georgia',
        'outer banks', 'you', 'elite', 'money heist',
    ]
    for show in ACTIVE_SERIES:
        q.append(f'{show} {YEAR}')
        q.append(f'{show} {YEAR-1}')
        # Poslední 3 sezóny pro každý aktivní seriál
        for s in range(1, 4):
            q.append(f'{show} s{s:02d} {YEAR}')

    # Deduplikace
    seen = set(); out = []
    for x in q:
        xs = x.strip()
        if xs and xs not in seen:
            seen.add(xs); out.append(xs)
    return out

def load_existing(path):
    """Načti existující JSON DB."""
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def merge_movies(existing, new_records):
    """Přidej nové filmy do existující DB, zachovej lepší varianty."""
    best = {m['norm_title']: m for m in existing if m.get('norm_title')}
    added = 0
    for r in new_records:
        k = r.get('norm_title')
        if not k: continue
        if k not in best or r['score'] > best[k]['score']:
            if k not in best: added += 1
            best[k] = r
    result = sorted(best.values(), key=lambda x: (-x['score'], x.get('clean_title') or ''))
    return result, added

def merge_series(existing, new_records):
    """Přidej nové epizody do existujících seriálů."""
    # Indexuj existující seriály podle norm_show
    shows = {s['norm_show']: s for s in existing if s.get('norm_show')}
    added_eps = 0
    added_shows = 0

    for r in new_records:
        k = r.get('norm_show')
        if not k or r.get('season') is None: continue

        if k not in shows:
            shows[k] = {
                'show_title': r['show_title'],
                'norm_show':  k,
                'year':       r.get('year'),
                'cz': False, 'sk': False,
                'ep_count': 0,
                'episodes': [],
            }
            added_shows += 1

        show = shows[k]
        # Zkontroluj zda epizoda již existuje
        se = (r['season'], r['episode'])
        existing_eps = {(e['season'], e['episode']): e for e in show['episodes']}

        if se not in existing_eps or r['score'] > existing_eps[se]['score']:
            if se not in existing_eps: added_eps += 1
            existing_eps[se] = {
                'ident':   r['ident'],
                'season':  r['season'],
                'episode': r['episode'],
                'quality': r['quality'],
                'cz':      r['cz'],
                'sk':      r['sk'],
                'score':   r['score'],
            }
            show['episodes'] = sorted(existing_eps.values(),
                                      key=lambda x: (x['season'], x['episode']))
            show['ep_count'] = len(show['episodes'])

        if r.get('cz'): show['cz'] = True
        if r.get('sk'): show['sk'] = True
        if r.get('year') and (not show.get('year') or r['year'] > show['year']):
            show['year'] = r['year']

    result = sorted(shows.values(), key=lambda x: x.get('show_title') or '')
    return result, added_shows, added_eps

def main():
    username = os.environ.get('WS_USER')
    password = os.environ.get('WS_PASS')
    if not username or not password:
        print('ERROR: Nastav WS_USER a WS_PASS jako GitHub Secrets.')
        sys.exit(1)

    print(f'=== Denní aktualizace – {datetime.datetime.now().strftime("%d.%m.%Y %H:%M")} ===')
    print(f'Přihlašování jako {username}...')
    token = login(username, password)
    print(f'Token OK: {token[:8]}...')

    queries = update_queries()
    print(f'Dotazů pro aktualizaci: {len(queries)}')
    print(f'Odhadovaný čas: ~{len(queries)*1.5/60:.0f} minut\n')

    # Načti existující DB
    movies_path = os.path.join(OUT_DIR, 'movies.json')
    series_path = os.path.join(OUT_DIR, 'series.json')
    existing_movies = load_existing(movies_path)
    existing_series = load_existing(series_path)
    print(f'Existující DB: {len(existing_movies)} filmů, {len(existing_series)} seriálů')

    # Stáhni nové záznamy
    new_records = []
    seen_idents = set()
    start_time = time.time()

    for i, q in enumerate(queries):
        pct = int(i * 100 / len(queries))
        if i % 10 == 0:
            print(f'[{pct:3d}%] {i+1}/{len(queries)} | nových záznamů: {len(new_records)} | dotaz: {q}')

        results = fetch_all_pages(q, token, max_pages=3, pause=0.5)
        for r in results:
            if r['ident'] in seen_idents: continue
            seen_idents.add(r['ident'])
            p = parse_file(r)
            if p: new_records.append(p)

        if (i + 1) % 50 == 0:
            print(f'  *** Přestávka 5s ***')
            time.sleep(5)
        else:
            time.sleep(1.0)

    print(f'\nNačteno {len(new_records)} nových záznamů')

    # Merge do existující DB
    new_movies = [r for r in new_records if r['type'] == 'movie']
    new_series = [r for r in new_records if r['type'] == 'series']

    merged_movies, added_m = merge_movies(existing_movies, new_movies)
    merged_series, added_s, added_eps = merge_series(existing_series, new_series)

    print(f'Přidáno: {added_m} nových filmů, {added_s} nových seriálů, {added_eps} nových epizod')

    # Ulož
    save_json(movies_path, merged_movies)
    save_json(series_path, merged_series)

    meta = {
        'updated':      datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'movies_count': len(merged_movies),
        'series_count': len(merged_series),
        'year':         YEAR,
        'last_update_added': {
            'movies': added_m,
            'shows':  added_s,
            'episodes': added_eps,
        }
    }
    save_json(os.path.join(OUT_DIR, 'meta.json'), meta)

    elapsed = int(time.time() - start_time)
    print(f'\nHOTOVO za {elapsed // 60}m {elapsed % 60}s')
    print(f'DB celkem: {len(merged_movies)} filmů, {len(merged_series)} seriálů')

if __name__ == '__main__':
    main()
