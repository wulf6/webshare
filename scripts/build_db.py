#!/usr/bin/env python3
"""
Webshare DB builder – běží jako GitHub Action.
Optimalizováno pro rychlost: cíl < 20 minut.
"""

import os, sys, json, time, hashlib, re, unicodedata, datetime, gzip
import urllib.request, urllib.parse
from xml.etree import ElementTree as ET

API    = 'https://webshare.cz/api/'
REALM  = ':Webshare:'
YEAR   = datetime.datetime.now().year
UA     = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

OUT_DIR   = os.path.join(os.path.dirname(__file__), '..', 'db')
META_FILE = os.path.join(OUT_DIR, 'meta.json')

def _call(endpoint, params, retries=3):
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
            print(f'  [retry {attempt+1}/{retries}] {endpoint}: {e}')
            time.sleep(2 ** attempt)
    return None

def _ok(root):
    return root is not None and root.findtext('status') == 'OK'

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
    while i > 0: ctx.update(final[:min(i,16)]); i -= 16
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
    r += _to64((final[4]<<16)|(final[10]
