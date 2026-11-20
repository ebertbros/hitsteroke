#!/usr/bin/env python3
"""Logowanie do USDB (usdb.animux.de) i zapis ciasteczka sesji dla pobierz.py.

Uruchom SAM w swoim terminalu (hasło nie trafia nigdzie poza USDB):

  python3 usdb_login.py

Login i hasło: pytanie w terminalu (hasło bez echa) albo zmienne USDB_USER / USDB_PASS.
Ciasteczka trafiają do .cache/usdb_cookies.txt (prawa 600, w .gitignore). Hasła nie zapisujemy.
"""

import getpass
import http.cookiejar
import os
import sys
import urllib.parse
import urllib.request

import hitster_cards as hc

COOKIES = os.path.join(hc.CACHE_DIR, "usdb_cookies.txt")
BASE = "https://usdb.animux.de/index.php"


def opener(jar):
    o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    o.addheaders = [("User-Agent", hc.BROWSER_UA)]
    return o


def load_jar():
    jar = http.cookiejar.MozillaCookieJar(COOKIES)
    if os.path.exists(COOKIES):
        jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def is_logged_in(o):
    html = o.open(BASE + "?link=gettxt&id=9888", data=b"wd=1", timeout=30).read().decode("utf-8", "replace")
    return "<textarea" in html.lower() and "#ARTIST" in html


def main():
    hc.ensure_dir(hc.CACHE_DIR)
    jar = load_jar()
    o = opener(jar)
    if os.path.exists(COOKIES) and is_logged_in(o):
        print("Sesja USDB w .cache/usdb_cookies.txt jest aktywna, nie trzeba logować się ponownie.")
        return
    user = os.environ.get("USDB_USER") or input("USDB login: ").strip()
    pw = os.environ.get("USDB_PASS") or getpass.getpass("USDB hasło: ")
    jar = http.cookiejar.MozillaCookieJar(COOKIES)
    o = opener(jar)
    o.open(BASE, timeout=30).read()      # PHPSESSID
    data = urllib.parse.urlencode({"user": user, "pass": pw, "login": "Login", "remember": "1"}).encode()
    o.open(BASE + "?link=login", data=data, timeout=30).read()
    if not is_logged_in(o):
        sys.exit("Logowanie nieudane (sprawdź login i hasło na usdb.animux.de).")
    jar.save(ignore_discard=True, ignore_expires=True)
    os.chmod(COOKIES, 0o600)
    print(f"Zalogowano. Sesja zapisana w {COOKIES}")


if __name__ == "__main__":
    main()
