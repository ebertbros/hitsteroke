"""Wspólny kod biblioteki kart Hitster (tylko biblioteka standardowa Pythona).

Ścieżki, cache, odczyt bazy Jumbo, metadane ze Spotify (embed), rok z MusicBrainz,
dopasowanie do katalogu USDB i parsowanie kodu QR z karty.
Opis całości: README.md w tym katalogu.
"""

import configparser
import csv
import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")          # w .gitignore
EDITIONS_DIR = os.path.join(HERE, "editions")
OVERRIDES_CSV = os.path.join(HERE, "overrides.csv")
SCANNER_MAP = os.path.join(HERE, "scanner_map.json")

JUMBO_URL = "https://hitster.jumboplay.com/hitster-assets/gameset_database.json"
JUMBO_DB = os.path.join(HERE, "gameset_database.json")   # kopia w repo (offline na evencie)
SPOTIFY_CACHE = os.path.join(CACHE_DIR, "spotify.json")
MB_CACHE = os.path.join(CACHE_DIR, "musicbrainz.json")
USDB_TXT_DIR = os.path.join(CACHE_DIR, "usdb_txt")
DEFAULT_USDB_CATALOG = os.path.expanduser("~/Downloads/hitster-usdx/usdb_katalog.json")

USDX_CONFIG = os.path.expanduser("~/Library/Application Support/UltraStar Deluxe/config.ini")
DEFAULT_SONG_ROOT = "/Volumes/GRZ EXTERNAL/Hitster Karaoke/songs"
SONG_SUBDIR = "hitster"                    # PLAY hitster/<folder>

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# MusicBrainz wymaga identyfikującego User-Agenta; kontakt ogólny (repo), bez danych osobowych.
MB_UA = "HitsterKaraokeLibrary/1.0 ( https://github.com/ebertbros/hitsteroke )"

CSV_COLUMNS = ["karta", "sku", "spotify", "wykonawca", "tytul", "rok", "rok_spotify",
               "rok_mb", "do_sprawdzenia", "usdb_status", "usdb_id", "folder"]

# statusy USDB
ZIELONY = "zielony"                 # są nuty tego wykonawcy
INNA_WERSJA = "inna wersja"         # nuty innego wykonawcy tej samej piosenki (potwierdzone ręcznie)
INNA_WERSJA_KANDYDAT = "inna wersja?"  # ten sam tytuł, inny wykonawca: do potwierdzenia w overrides.csv
CZERWONY = "czerwony"               # brak nut
NIEOBSLUGIWANY = "nieobslugiwany"   # rap, instrumental, opera... (decyzja ręczna)
DO_POBRANIA = (ZIELONY, INNA_WERSJA)


# ---------------------------------------------------------------- pliki, cache

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    """Zapis atomowy (plik tymczasowy + rename), żeby przerwanie nie psuło cache."""
    ensure_dir(os.path.dirname(path))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, path)


def song_root():
    """Katalog piosenek gry: [Directories] SongDir1 z config.ini, inaczej dysk zewnętrzny."""
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read(USDX_CONFIG, encoding="utf-8")
        d = cp.get("Directories", "SongDir1", fallback="").strip()
        if d:
            return d
    except (configparser.Error, OSError):
        pass
    return DEFAULT_SONG_ROOT


def hitster_song_dir(root=None):
    return os.path.join(root or song_root(), SONG_SUBDIR)


# ---------------------------------------------------------------- baza Jumbo

def load_jumbo_db(path=JUMBO_DB):
    db = load_json(path)
    if db is None:
        raise SystemExit(f"Brak bazy Jumbo ({path}). Uruchom najpierw: python3 fetch_jumbo_db.py")
    return db


def gamesets_by_sku(db):
    return {g["sku"].lower(): g for g in db["gamesets"]}


# ---------------------------------------------------------------- QR

# the Honeywell scanner sends the code without "https://" (www.hitstergame.com/...)
QR_RE = re.compile(r"^\s*(?:https?://)?(?:www\.)?hitstergame\.com/([a-z]{2}(?:[-_][a-z]{2})?)/"
                   r"([a-z0-9]{4,12})/(\d{1,6})/?\s*$", re.I)


def card_key(sku, number):
    """Klucz karty w mapie skanera: 'aaae0005/00171' (SKU małymi literami, numer 5 cyfr)."""
    return f"{sku.lower()}/{int(number):05d}"


def parse_qr(text):
    """URL z karty -> klucz 'sku/numer' albo None (inny format = nieznana karta).
    Język w URL jest pomijany."""
    m = QR_RE.match(text or "")
    if not m:
        return None
    return card_key(m.group(2), m.group(3))


# ---------------------------------------------------------------- normalizacja

# "(feat. X)", "[with X]" albo " feat. X" na końcu; samo "With" na początku tytułu zostaje
_FEAT_RE = re.compile(r"\s*[\(\[]\s*(feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]|\s+(feat\.?|ft\.?|featuring)\s.*$", re.I)
_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
_PAREN_RE = re.compile(r"\(([^)]*)\)")
# dopiski Spotify do usunięcia z tytułu (po " - " albo w nawiasie)
_SUFFIX_WORDS = (r"remaster|remastered|edit|version|mono|stereo|single|mix|live|"
                 r"from|bonus|deluxe|anniversary|recorded|rerecorded|soundtrack|re-recorded|"
                 r"take|demo|acoustic|extended|original|explicit|clean|instrumental|\d{4}")
_DASH_SUFFIX_RE = re.compile(r"\s+[-–—]\s+[^-–—]*\b(" + _SUFFIX_WORDS + r")\b.*$", re.I)
_PAREN_SUFFIX_RE = re.compile(r"\s*[\(\[][^\)\]]*\b(" + _SUFFIX_WORDS + r")\b[^\)\]]*[\)\]]", re.I)


def clean_spotify_title(title):
    """'Freedom! '90 - Remastered' -> 'Freedom! '90'; usuwa też '(feat. X)'."""
    t = title or ""
    t = _DASH_SUFFIX_RE.sub("", t)
    t = _PAREN_SUFFIX_RE.sub("", t)
    t = re.sub(r"\s*[\(\[]\s*(feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s+-\s+(feat\.?|ft\.?)\s.*$", "", t, flags=re.I)
    return t.strip() or (title or "").strip()


def norm(s):
    """Do porównań: bez akcentów, małe litery, & -> and, bez interpunkcji i 'the'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("$", "s").replace("’", "'").replace("‘", "'").replace("`", "'")
    s = s.replace("&", " and ").replace("+", " and ")
    s = re.sub(r"'", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\bthe\b", " ", s)
    s = re.sub(r"\band\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_variants(title):
    """Zbiór znormalizowanych wariantów tytułu: rdzeń bez nawiasów, treść nawiasów,
    części rozdzielone '/'. 'It's In His Kiss (The Shoop Shoop Song)' pasuje do
    'The Shoop Shoop Song (It's In His Kiss)'."""
    t = _BRACKET_TAG_RE.sub("", title or "")
    t = _FEAT_RE.sub("", t)
    out = set()
    core = _PAREN_RE.sub("", t)
    core = re.sub(r"\s+[-–—]\s+.*$", "", core)
    for part in [core] + [p for p in core.split("/") if "/" in core]:
        n = norm(part)
        if n:
            out.add(n)
    for inner in _PAREN_RE.findall(t):
        n = norm(inner)
        if n and len(n) >= 4 and not re.search(r"\b(" + _SUFFIX_WORDS + r")\b", inner, re.I):
            out.add(n)
    full = norm(t)
    if full:
        out.add(full)
    # bez spacji: "Loco-Motion" = "Locomotion", "Rock-n-Roll" = "Rock N Roll"
    return {v.replace(" ", "") for v in out}


_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\+|/|;|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\bvs\.?|\bx\b|\band\b|\bund\b|\bi\b|\bet\b|\bduet\b)\s*", re.I)


def artist_names(artist):
    """'Cardi B, Bad Bunny and J Balvin' -> {'cardi b', 'bad bunny', 'j balvin', ...}."""
    names = set()
    whole = norm(artist)
    if whole:
        names.add(whole)
    for part in _ARTIST_SPLIT_RE.split(artist or ""):
        n = norm(part)
        if n and len(n) >= 2:
            names.add(n)
    return names


def artists_match(card_artists, usdb_artist):
    """True, gdy któryś wykonawca z karty występuje w wykonawcy USDB (lub odwrotnie)."""
    u_names = artist_names(usdb_artist)
    u_full = " " + norm(usdb_artist) + " "
    for a in card_artists:
        for n in artist_names(a):
            if n in u_names:
                return True
            if len(n) >= 4 and (" " + n + " ") in u_full:
                return True
            for un in u_names:
                if len(un) >= 4 and (" " + un + " ") in (" " + n + " "):
                    return True
    return False


def safe_folder(name):
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name


# ---------------------------------------------------------------- HTTP

def http_get(url, headers=None, timeout=30, retries=4, backoff=2.0, max_wait=60.0):
    """GET z ponawianiem przy 429/5xx i błędach sieci. Zwraca bytes albo rzuca wyjątek.
    Gdy serwer podaje Retry-After (MusicBrainz przy globalnym dławieniu: 1 s), czekamy tyle,
    zamiast wykładniczo."""
    last = None
    for attempt in range(retries + 1):
        wait = min(backoff * (2 ** attempt), max_wait)
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
            ra = (e.headers or {}).get("Retry-After", "")
            if ra.strip().isdigit():
                wait = min(float(ra) + 0.5 + 0.5 * attempt, max_wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        if attempt < retries:
            time.sleep(wait)
    raise last


# ---------------------------------------------------------------- Spotify (embed, bez klucza)

_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def spotify_fetch(track_id):
    """Metadane utworu z open.spotify.com/embed (nieoficjalne). None, gdy utworu nie ma."""
    try:
        html = http_get("https://open.spotify.com/embed/track/" + track_id,
                        headers={"User-Agent": BROWSER_UA, "Accept-Language": "en"}).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": "404"}
        raise
    m = _NEXT_RE.search(html)
    if not m:       # np. strona blokady: błąd przejściowy, nie zapisujemy do cache
        raise RuntimeError("Spotify: brak __NEXT_DATA__ (blokada?)")
    data = json.loads(m.group(1))
    e = (((data.get("props") or {}).get("pageProps") or {}).get("state") or {}).get("data", {}).get("entity")
    if not e:
        return {"error": "no entity"}
    return {
        "name": e.get("name") or e.get("title") or "",
        "artists": [a.get("name", "") for a in e.get("artists") or []],
        "release": ((e.get("releaseDate") or {}).get("isoString") or "")[:10],
        "duration_ms": e.get("duration"),
        "explicit": bool(e.get("isExplicit")),
    }


class SpotifyCache:
    """Cache metadanych Spotify. Błędy przejściowe (429, 5xx, blokada) nie trafiają do cache:
    po kolejnych porażkach odczekanie rośnie wykładniczo (1, 2, 4... do 15 min), a ID ląduje
    w self.failed. Zapisywane są tylko wyniki trwałe (dane albo 404 / brak utworu)."""

    def __init__(self, path=SPOTIFY_CACHE, delay=0.25, log=print):
        self.path = path
        self.data = load_json(path, {})
        self.delay = delay
        self.dirty = 0
        self.failed = set()
        self.streak = 0
        self.log = log

    def get(self, track_id, refetch_errors=False):
        hit = self.data.get(track_id)
        if hit is not None and not (refetch_errors and "error" in hit):
            return hit
        try:
            res = spotify_fetch(track_id)
        except Exception as e:
            self.failed.add(track_id)
            self.streak += 1
            pause = min(60 * 2 ** (self.streak - 1), 900)
            self.log(f"Spotify {track_id}: {str(e)[:150]} -> odczekanie {pause} s (porażek z rzędu: {self.streak})")
            time.sleep(pause)
            return None
        self.streak = 0
        self.failed.discard(track_id)
        self.data[track_id] = res
        self.dirty += 1
        if self.dirty >= 25:
            self.save()
        time.sleep(self.delay)
        return res

    def save(self):
        if self.dirty:
            save_json(self.path, self.data)
            self.dirty = 0


# ---------------------------------------------------------------- MusicBrainz

def _mb_query(artist, title):
    q = f'recording:"{title}" AND artist:"{artist}"'
    url = "https://musicbrainz.org/ws/2/recording/?fmt=json&limit=50&query=" + urllib.parse.quote(q)
    return json.loads(http_get(url, headers={"User-Agent": MB_UA, "Accept": "application/json"},
                               retries=10, backoff=1.5, max_wait=30.0))


_MB_SKIP_RE = re.compile(r"\b(live|remix|demo|karaoke|instrumental|cover|acapella|a cappella)\b", re.I)


def mb_earliest_year(artist, title):
    """Najwcześniejszy first-release-date nagrania o tym tytule i wykonawcy.
    Zwraca {'year': int|None, 'n': liczba pasujących nagrań}."""
    clean = clean_spotify_title(title)
    q_title = re.sub(r'["\\]', " ", _PAREN_RE.sub("", clean)).strip() or clean
    q_artist = re.sub(r'["\\]', " ", artist)
    data = _mb_query(q_artist, q_title)
    want_titles = title_variants(clean)
    years = []
    for r in data.get("recordings", []):
        if r.get("video"):
            continue
        dis = r.get("disambiguation") or ""
        if _MB_SKIP_RE.search(dis):
            continue
        credit = " ".join((c.get("name") or "") + " " + (c.get("joinphrase") or "")
                          for c in r.get("artist-credit") or [])
        if not artists_match([artist], credit):
            continue
        if not (title_variants(r.get("title", "")) & want_titles):
            continue
        frd = r.get("first-release-date") or ""
        if re.match(r"^\d{4}", frd):
            years.append(int(frd[:4]))
    return {"year": min(years) if years else None, "n": len(years)}


def mb_key(artist, title):
    return f"{norm(artist)}\t{norm(clean_spotify_title(title))}"


class MBCache:
    """Cache roku z MusicBrainz, 1 zapytanie/s. Błędy nie trafiają do cache (ponowienie przy
    następnym uruchomieniu); po kolejnych porażkach odczekanie rośnie do 10 min."""

    def __init__(self, path=MB_CACHE, delay=1.1, log=print):
        self.path = path
        self.data = load_json(path, {})
        self.delay = delay
        self.dirty = 0
        self._last = 0.0
        self.failed = set()
        self.streak = 0
        self.log = log

    def get(self, artist, title):
        key = mb_key(artist, title)
        if key in self.data:
            return self.data[key]
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            res = mb_earliest_year(artist, title)
        except Exception as e:      # MB bywa przeciążony; brak roku MB nie blokuje reszty
            self._last = time.monotonic()
            self.failed.add(key)
            self.streak += 1
            if self.streak >= 3:
                pause = min(30 * 2 ** (self.streak - 3), 600)
                self.log(f"MusicBrainz: {str(e)[:150]} -> odczekanie {pause} s (porażek z rzędu: {self.streak})")
                time.sleep(pause)
            return {"year": None, "n": 0, "error": str(e)[:200]}
        self.streak = 0
        self.failed.discard(key)
        self._last = time.monotonic()
        self.data[key] = res
        self.dirty += 1
        if self.dirty >= 20:
            self.save()
        return res

    def save(self):
        if self.dirty:
            save_json(self.path, self.data)
            self.dirty = 0


# ---------------------------------------------------------------- USDB

class UsdbCatalog:
    """Katalog USDB: lista wierszy [id, artist, title, genre, year, edition, golden,
    language, creator, '', views, '']."""

    def __init__(self, path=DEFAULT_USDB_CATALOG):
        rows = load_json(path)
        if rows is None:
            raise SystemExit(f"Brak katalogu USDB: {path} (opcja --usdb-katalog)")
        self.rows = {}
        self.by_title = {}
        for r in rows:
            rec = {"id": str(r[0]), "artist": r[1], "title": r[2], "genre": r[3],
                   "year": r[4], "edition": r[5], "golden": r[6] == "Yes",
                   "language": r[7], "views": int(r[10] or 0) if str(r[10]).isdigit() else 0}
            self.rows[rec["id"]] = rec
            for v in title_variants(rec["title"]):
                self.by_title.setdefault(v, []).append(rec)

    def get(self, usdb_id):
        return self.rows.get(str(usdb_id))

    _QUALIFIER_RE = re.compile(r"\b(version|live|remix|mix|edit|recall|acoustic|unplugged|demo|"
                               r"instrumental|medley|cover|short|long|extended)\b", re.I)

    @classmethod
    def _rank(cls, rec):
        """Wybór pliku: bez dopisków (wersja, live, remix...), nie duet, złoty, najwięcej wyświetleń."""
        duet = "[duet]" in rec["title"].lower() or "duet" in (rec["edition"] or "").lower()
        plain = not cls._QUALIFIER_RE.search(rec["title"])
        return (plain, not duet, rec["golden"], rec["views"])

    def match(self, artists, title):
        """-> (status, rekord|None). Zielony: tytuł i wykonawca; 'inna wersja?': sam tytuł."""
        cands = {}
        for v in title_variants(title):
            for rec in self.by_title.get(v, []):
                cands[rec["id"]] = rec
        same = [r for r in cands.values() if artists_match(artists, r["artist"])]
        if same:
            return ZIELONY, max(same, key=self._rank)
        if cands:
            return INNA_WERSJA_KANDYDAT, max(cands.values(), key=self._rank)
        return CZERWONY, None


def usdb_folder(rec):
    """Nazwa folderu piosenki: 'Wykonawca - Tytuł' z USDB (bez znacznika [DUET])."""
    title = re.sub(r"\s*\[DUET\]\s*", " ", rec["title"], flags=re.I).strip()
    return safe_folder(f"{rec['artist']} - {title}")


# ---------------------------------------------------------------- overrides

def load_overrides(path=OVERRIDES_CSV):
    """overrides.csv: ręczne decyzje. Klucz 'klucz' = ID Spotify (dotyczy wszystkich edycji)
    albo 'sku/numer' (jedna karta). Puste pola nie zmieniają wyniku automatu."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            k = (row.get("klucz") or "").strip()
            if not k or k.startswith("#"):
                continue
            if "/" in k:
                sku, num = k.split("/", 1)
                k = card_key(sku, num)
            out[k] = {c: (v or "").strip() for c, v in row.items() if c != "klucz"}
    return out


def read_edition_csv(sku):
    path = os.path.join(EDITIONS_DIR, f"{sku.lower()}.csv")
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
