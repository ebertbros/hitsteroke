#!/usr/bin/env python3
"""Pobiera piosenki dla kart ze statusem 'zielony' i 'inna wersja' z editions/<sku>.csv.

  python3 pobierz.py aaae0001 [aaae0005 ...]      wszystkie brakujące
  python3 pobierz.py aaae0001 --karty 1,2,171     wybrane karty
  python3 pobierz.py aaae0001 --limit 5 --dry-run plan bez pobierania
  python3 pobierz.py --import-json ~/Downloads/hitster-usdx/usdb_txt_batch1.json
                                                  dołóż gotowe .txt do cache (format {nr: {id, txt}})

Dla każdej piosenki (folder z kolumny `folder`, wspólny dla wszystkich edycji):
  1. .txt z USDB: z .cache/usdb_txt/<id>.txt albo z USDB (sesja z usdb_login.py),
  2. z #VIDEO: v=<YouTube> -> mp4 do 720p + mp3; a=<YouTube> -> samo audio; co=<plik> -> okładka
     z images.fanart.tv,
  3. mp3 (libmp3lame -q:a 2) znormalizowane do -14 LUFS (ffmpeg loudnorm, dwa przebiegi),
  4. .txt: #MP3/#AUDIO/#VIDEO/#COVER na pliki lokalne, #YEAR = rok z tabeli (jak na karcie),
     #PREVIEWSTART z fragmentu medley USDB albo na pierwszy refren (najwcześniejsza z najczęściej
     powtarzanych linii), jeśli USDB go nie podaje,
  5. folder <katalog piosenek>/hitster/<folder>. Pliki tymczasowe też na tym dysku.

Raport błędów: .cache/raporty/pobierz_<data>.csv (i podsumowanie na końcu).
"""

import argparse
import collections
import csv
import datetime
import html
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import hitster_cards as hc

YT_DLP = shutil.which("yt-dlp") or os.path.expanduser("~/.local/bin/yt-dlp")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
COOKIES = os.path.join(hc.CACHE_DIR, "usdb_cookies.txt")
TARGET_LUFS = -14.0
MIN_FREE_GB = 2.0


# ---------------------------------------------------------------- USDB txt

class Usdb:
    def __init__(self):
        self.opener = None
        self._last = 0.0

    def _open(self):
        if self.opener is None:
            if not os.path.exists(COOKIES):
                raise RuntimeError("brak sesji USDB: uruchom w terminalu python3 usdb_login.py")
            jar = http.cookiejar.MozillaCookieJar(COOKIES)
            jar.load(ignore_discard=True, ignore_expires=True)
            self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            self.opener.addheaders = [("User-Agent", hc.BROWSER_UA)]
        return self.opener

    def txt(self, usdb_id):
        path = os.path.join(hc.USDB_TXT_DIR, f"{usdb_id}.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
        wait = 1.0 - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        page = self._open().open(f"https://usdb.animux.de/index.php?link=gettxt&id={usdb_id}",
                                 data=b"wd=1", timeout=30).read().decode("utf-8", "replace")
        self._last = time.monotonic()
        m = re.search(r"<textarea[^>]*>(.*?)</textarea>", page, re.S | re.I)
        if not m or "#" not in m.group(1):
            raise RuntimeError("USDB nie zwróciło .txt (sesja wygasła? python3 usdb_login.py)")
        txt = html.unescape(m.group(1)).replace("\r\n", "\n").strip() + "\n"
        hc.ensure_dir(hc.USDB_TXT_DIR)
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
        return txt


def import_json(path):
    data = hc.load_json(path)
    hc.ensure_dir(hc.USDB_TXT_DIR)
    n = 0
    for v in data.values():
        if v.get("id") and v.get("txt"):
            with open(os.path.join(hc.USDB_TXT_DIR, f"{v['id']}.txt"), "w", encoding="utf-8") as f:
                f.write(v["txt"].replace("\r\n", "\n"))
            n += 1
    print(f"zaimportowano {n} plików .txt do {hc.USDB_TXT_DIR}")


# ---------------------------------------------------------------- txt

def headers(txt):
    h = {}
    for line in txt.splitlines():
        if line.startswith("#") and ":" in line:
            k, v = line[1:].split(":", 1)
            h.setdefault(k.strip().upper(), v.strip())
    return h


def video_meta(h):
    return dict(kv.split("=", 1) for kv in h.get("VIDEO", "").split(",") if "=" in kv)


def _num(s):
    return float(str(s).replace(",", "."))


def chorus_start(txt):
    """Sekunda początku pierwszego refrenu: pierwsza linia tekstu, która powtarza się
    najczęściej (min. 2 razy, min. 10 znaków). None, gdy się nie da ustalić."""
    h = headers(txt)
    if h.get("RELATIVE", "").lower() == "yes":
        return None
    try:
        bpm, gap = _num(h["BPM"]), _num(h.get("GAP", "0"))
    except (KeyError, ValueError):
        return None
    # USDB oznacza fragment do składanki (medley) zwykle na refrenie: bierzemy go w pierwszej kolejności
    medley = h.get("MEDLEYSTARTBEAT") or (video_meta(h).get("medley", "").split("-")[0])
    if medley and medley.strip().isdigit() and int(medley) > 0:
        return round(max(gap / 1000.0 + int(medley) * 60.0 / (bpm * 4.0) - 0.5, 0.0), 2)
    lines, cur, start = [], [], None
    for line in txt.splitlines():
        if line[:1] in (":", "*", "F", "R", "G") and len(line) > 2 and line[1] == " ":
            parts = line.split(" ", 4)
            if len(parts) < 4:
                continue
            if start is None:
                try:
                    start = int(parts[1])
                except ValueError:
                    continue
            cur.append(parts[4] if len(parts) > 4 else "")
        elif line.startswith("-") or line.startswith("P") or line.startswith("E"):
            if cur and start is not None:
                lines.append((hc.norm("".join(cur)), start))
            cur, start = [], None
    if cur and start is not None:
        lines.append((hc.norm("".join(cur)), start))
    counts = collections.Counter(t for t, _ in lines if len(t) >= 10)
    if not counts:
        return None
    top = counts.most_common(1)[0][1]
    if top < 2:
        return None
    # najwcześniejsza linia powtarzana prawie tak często jak najczęstsza: najczęstsza bywa
    # dopiero w zakończeniu (np. „That's what you get” w Freedom! '90)
    threshold = max(2, top - 2)
    beat = min(s for t, s in lines if counts.get(t, 0) >= threshold)
    sec = gap / 1000.0 + beat * 60.0 / (bpm * 4.0) - 0.5
    return round(max(sec, 0.0), 2)


def rewrite_txt(txt, name, year, has_video, cover):
    out = []
    for line in txt.splitlines():
        key = line.split(":", 1)[0].upper() if line.startswith("#") else ""
        if key in ("#MP3", "#AUDIO", "#VIDEO", "#COVER", "#BACKGROUND", "#ENCODING"):
            continue
        if key == "#YEAR" and year:
            continue
        out.append(line.rstrip("\r"))
    h = headers(txt)
    add = [f"#MP3:{name}.mp3", f"#AUDIO:{name}.mp3"]
    if has_video:
        add.append(f"#VIDEO:{name}.mp4")
    if cover:
        add.append(f"#COVER:{cover}")
    if year:
        add.append(f"#YEAR:{year}")
    if "PREVIEWSTART" not in h:
        ps = chorus_start(txt)
        if ps is not None:
            add.append(f"#PREVIEWSTART:{ps}")
    last_hdr = max((i for i, l in enumerate(out) if l.startswith("#")), default=-1)
    out = out[:last_hdr + 1] + add + out[last_hdr + 1:]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- media

def run(cmd, timeout=900):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cmd[0])}: " + (p.stderr or p.stdout).strip().splitlines()[-1][:300]
                           if (p.stderr or p.stdout).strip() else f"{cmd[0]} kod {p.returncode}")
    return p


def yt(args, url):
    run([YT_DLP, "-q", "--no-warnings", "--no-playlist"] + args + [url])


def to_mp3_normalized(src, dst):
    """Dwa przebiegi loudnorm (pomiar, potem liniowa korekta) do -14 LUFS, mp3 VBR q2."""
    p = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", src, "-vn", "-af",
                        f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=900)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
    if m:
        s = json.loads(m.group(0))
        af = (f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:measured_I={s['input_i']}:"
              f"measured_TP={s['input_tp']}:measured_LRA={s['input_lra']}:"
              f"measured_thresh={s['input_thresh']}:offset={s['target_offset']}:linear=true")
    else:
        af = f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11"
    run([FFMPEG, "-loglevel", "error", "-y", "-i", src, "-vn", "-af", af, "-ar", "44100",
         "-c:a", "libmp3lame", "-q:a", "2", dst])


def fetch_cover(co, dst, usdb_id=None):
    """Okładka: fanart.tv (assets., potem images. - ten zwraca dziś 403), na końcu mała okładka z USDB."""
    urls = []
    if co:
        urls += ["https://assets.fanart.tv/fanart/" + co, "https://images.fanart.tv/fanart/" + co]
    if usdb_id:
        urls.append(f"https://usdb.animux.de/data/cover/{usdb_id}.jpg")
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": hc.BROWSER_UA})
            b = urllib.request.urlopen(req, timeout=20).read()
            if len(b) > 1000 and b[:3] == b"\xff\xd8\xff":
                with open(dst, "wb") as f:
                    f.write(b)
                return True
        except Exception:
            continue
    return False


def download_song(txt, folder, year, work, final, search_fallback=False, usdb_id=None):
    """Buduje folder piosenki w `work`, potem przenosi do `final`. Zwraca opis wyniku."""
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    h = headers(txt)
    meta = video_meta(h)
    name = folder
    notes = []
    raw = os.path.join(work, "_audio_src")
    has_video = False
    if "v" in meta:
        try:
            yt(["-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b",
                "--merge-output-format", "mp4", "-o", os.path.join(work, f"{name}.mp4")],
               f"https://www.youtube.com/watch?v={meta['v']}")
            has_video = True
            src = os.path.join(work, f"{name}.mp4")
        except RuntimeError as e:
            if not search_fallback:
                raise RuntimeError(f"YouTube v={meta['v']}: {e}")
            notes.append(f"v={meta['v']} niedostępne, audio z wyszukiwania (sprawdź synchronizację)")
            src = None
    elif "a" in meta:
        yt(["-f", "ba", "-o", raw + ".%(ext)s"], f"https://www.youtube.com/watch?v={meta['a']}")
        src = next(os.path.join(work, f) for f in os.listdir(work) if f.startswith("_audio_src"))
    elif search_fallback:
        notes.append("brak linku w .txt, audio z wyszukiwania (sprawdź synchronizację)")
        src = None
    else:
        raise RuntimeError("brak linku YouTube w #VIDEO (v=/a=)")
    if src is None:
        q = f"ytsearch1:{h.get('ARTIST', '')} {h.get('TITLE', '')} audio"
        yt(["-f", "ba", "-o", raw + ".%(ext)s"], q)
        src = next(os.path.join(work, f) for f in os.listdir(work) if f.startswith("_audio_src"))
    to_mp3_normalized(src, os.path.join(work, f"{name}.mp3"))
    for f in os.listdir(work):
        if f.startswith("_audio_src"):
            os.remove(os.path.join(work, f))
    cover = None
    if fetch_cover(meta.get("co"), os.path.join(work, f"{name} [CO].jpg"), usdb_id):
        cover = f"{name} [CO].jpg"
    with open(os.path.join(work, f"{name}.txt"), "w", encoding="utf-8") as f:
        f.write(rewrite_txt(txt, name, year, has_video, cover))
    if os.path.exists(final):
        shutil.rmtree(final)
    shutil.move(work, final)
    notes.append("wideo" if has_video else "bez wideo")
    if not cover:
        notes.append("bez okładki")
    return ", ".join(notes)


def folder_complete(path):
    if not os.path.isdir(path):
        return False
    files = os.listdir(path)
    return any(f.endswith(".txt") for f in files) and any(f.endswith(".mp3") for f in files)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sku", nargs="*")
    ap.add_argument("--karty", help="lista numerów kart, np. 1,2,171")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="pobierz ponownie istniejące foldery")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--songs-dir", help="katalog piosenek gry (domyślnie SongDir1 z config.ini)")
    ap.add_argument("--szukaj-zamiennika", action="store_true",
                    help="gdy wideo z USDB jest niedostępne: audio z wyszukiwania YouTube")
    ap.add_argument("--import-json", help="import .txt z pliku JSON {nr: {id, txt}} do cache")
    a = ap.parse_args()

    if a.import_json:
        import_json(os.path.expanduser(a.import_json))
        if not a.sku:
            return
    if not a.sku:
        ap.error("podaj SKU")

    dest = hc.hitster_song_dir(a.songs_dir)
    if not os.path.isdir(os.path.dirname(dest)):
        sys.exit(f"Brak katalogu piosenek {os.path.dirname(dest)} (podłączony dysk?)")
    os.makedirs(dest, exist_ok=True)
    tmp_root = os.path.join(dest, ".pobieranie")

    karty = {int(x) for x in a.karty.split(",")} if a.karty else None
    jobs = collections.OrderedDict()       # folder -> dane
    for sku in a.sku:
        for r in hc.read_edition_csv(sku.lower()):
            if r["usdb_status"] not in hc.DO_POBRANIA or not r["folder"]:
                continue
            if karty and int(r["karta"]) not in karty:
                continue
            j = jobs.setdefault(r["folder"], {"usdb_id": r["usdb_id"], "years": set(), "cards": []})
            j["years"].add(r["rok"])
            j["cards"].append(f"{r['sku']}/{int(r['karta']):05d}")

    todo = [(f, j) for f, j in jobs.items() if a.force or not folder_complete(os.path.join(dest, f))]
    print(f"piosenek w tabelach: {len(jobs)}, już pobranych: {len(jobs) - len(todo)}, do pobrania: {len(todo)}")
    if a.limit:
        todo = todo[:a.limit]
    if a.dry_run:
        for f, j in todo:
            print(f"  {f}  (USDB {j['usdb_id']}, rok {'/'.join(sorted(j['years']))}, karty {', '.join(j['cards'])})")
        return

    usdb = Usdb()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rep_path = os.path.join(hc.ensure_dir(os.path.join(hc.CACHE_DIR, "raporty")), f"pobierz_{stamp}.csv")
    ok = err = 0
    with open(rep_path, "w", encoding="utf-8", newline="") as rf:
        w = csv.writer(rf)
        w.writerow(["folder", "usdb_id", "karty", "wynik", "opis"])
        for i, (folder, j) in enumerate(todo, 1):
            free = shutil.disk_usage(dest).free / 1e9
            if free < MIN_FREE_GB:
                print(f"STOP: mało miejsca na dysku z piosenkami ({free:.1f} GB)")
                break
            years = sorted(y for y in j["years"] if y)
            year = years[0] if years else ""
            note = f"; różne lata w edycjach: {'/'.join(years)}" if len(years) > 1 else ""
            try:
                txt = usdb.txt(j["usdb_id"])
                desc = download_song(txt, folder, year, os.path.join(tmp_root, folder),
                                     os.path.join(dest, folder), a.szukaj_zamiennika, j["usdb_id"])
                ok += 1
                w.writerow([folder, j["usdb_id"], " ".join(j["cards"]), "OK", desc + note])
                print(f"[{i}/{len(todo)}] OK    {folder} ({desc}{note})", flush=True)
            except Exception as e:
                err += 1
                w.writerow([folder, j["usdb_id"], " ".join(j["cards"]), "BŁĄD", str(e)])
                print(f"[{i}/{len(todo)}] BŁĄD  {folder}: {e}", flush=True)
                if "sesji USDB" in str(e) or "sesja wygasła" in str(e):
                    print("Przerywam: potrzebna sesja USDB (python3 usdb_login.py).")
                    break
            rf.flush()
    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"\nKONIEC: OK {ok}, błędy {err}. Raport: {rep_path}")


if __name__ == "__main__":
    main()
